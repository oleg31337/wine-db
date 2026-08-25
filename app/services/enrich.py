"""Wine card enrichment.

Pipeline:
  1. Read the label photo with a vision model (Ollama or any OpenAI-compatible
     endpoint configured in the backend .env).
  2. Look the wine up on the internet (Open Food Facts barcode lookup, optional
     SearxNG text search).
  3. If the internet has nothing, fall back to the model's own knowledge and, when
     confidence is low, ask the user to scan the BACK label.

Hard rules:
  * ratings and comments are NEVER produced here - they are user-only data.
  * only fields the caller left empty are ever suggested (enforced in the router).
"""

from __future__ import annotations

import base64
import json
import logging
import re
from html import unescape

import httpx

from app.config import Settings

logger = logging.getLogger(__name__)

# Fields the enrichment layer is allowed to propose. Note the absence of
# rating/comment fields - those are user-only by requirement.
ENRICHABLE_FIELDS = (
    "name",
    "maker",
    "wine_type",
    "country",
    "region",
    "vintage",
    "grape",
    "sugar_g_l",
    "alcohol_pct",
    "aromas",
    "acidity",
    "sweetness",
    "body",
    "mouthfeel",
    "wood",
)

VALID_TYPES = {"red", "white", "rose", "sparkling", "other"}

# Models describe a wine in style words rather than our enum, so map the common
# ones back onto the five categories the app uses.
_TYPE_SYNONYMS = {
    "rosé": "rose",
    "rosado": "rose",
    "rosato": "rose",
    "blush": "rose",
    "pink": "rose",
    "red wine": "red",
    "rouge": "red",
    "tinto": "red",
    "rosso": "red",
    "white wine": "white",
    "blanc": "white",
    "blanco": "white",
    "bianco": "white",
    "weisswein": "white",
    "sparkling wine": "sparkling",
    "champagne": "sparkling",
    "prosecco": "sparkling",
    "cava": "sparkling",
    "cremant": "sparkling",
    "crémant": "sparkling",
    "spumante": "sparkling",
    "sekt": "sparkling",
    "pétillant": "sparkling",
    "petillant": "sparkling",
    "franciacorta": "sparkling",
    "fortified": "other",
    "port": "other",
    "sherry": "other",
    "madeira": "other",
    "dessert": "other",
    "orange": "other",
    "amber": "other",
    "skin contact": "other",
    "vermouth": "other",
}

# Grape/varietal -> wine type, used to infer the type when the model only gave
# a grape. Keyed by substring so "Sauvignon Blanc" -> white, "Pinot Noir" -> red.
_GRAPE_TYPE = {
    "sauvignon blanc": "white",
    "chardonnay": "white",
    "riesling": "white",
    "pinot grigio": "white",
    "pinot gris": "white",
    "grüner veltliner": "white",
    "gruener veltliner": "white",
    "gewürztraminer": "white",
    "gewurztraminer": "white",
    "semillon": "white",
    "viognier": "white",
    "chenin blanc": "white",
    "muscat": "white",
    "albariño": "white",
    "albarino": "white",
    "verdejo": "white",
    "fiano": "white",
    "pinot noir": "red",
    "cabernet sauvignon": "red",
    "merlot": "red",
    "syrah": "red",
    "shiraz": "red",
    "tempranillo": "red",
    "sangiovese": "red",
    "nebbiolo": "red",
    "grenache": "red",
    "garnacha": "red",
    "malbec": "red",
    "cabernet franc": "red",
    "zinfandel": "red",
    "mataro": "red",
    "mourvèdre": "red",
    "mourvedre": "red",
    "gamay": "red",
    "graciano": "red",
    "petit verdot": "red",
    "carmenere": "red",
    "montepulciano": "red",
    "aglianico": "red",
    "pinotage": "red",
    "prosecco": "sparkling",
    "champagne": "sparkling",
    "cava": "sparkling",
    "lambrusco": "sparkling",
    "sekt": "sparkling",
    "moscato": "sparkling",
}

_VISION_PROMPT = (
    "You are reading a photo of a wine bottle label. Transcribe every legible piece "
    "of text exactly as printed, then summarise what the label tells us. "
    "Respond ONLY with compact JSON using these keys: "
    '{"raw_text": str, "name": str|null, "maker": str|null, "wine_type": '
    '"red"|"white"|"rose"|"sparkling"|"other"|null, "country": str|null, '
    '"region": str|null, "vintage": int|null, "grape": str|null, '
    '"alcohol_pct": number|null, "sugar_g_l": number|null, "legible": true|false}. '
    "Use null when the label does not show the information. Never invent text."
)

_TEXT_PROMPT = (
    "You are a sommelier reference database. Given the information below about one "
    "wine, return your best factual estimate of its attributes. "
    "Respond ONLY with compact JSON using these keys: "
    '{"name": str|null, "maker": str|null, "wine_type": "red"|"white"|"rose"|'
    '"sparkling"|"other"|null, "country": str|null, "region": str|null, '
    '"vintage": int|null, "grape": str|null, "sugar_g_l": number|null, '
    '"alcohol_pct": number|null, "aromas": str|null, "acidity": 0-5|null, '
    '"sweetness": 0-5|null, "body": 0-5|null, "mouthfeel": 0-5|null, "wood": 0-5|null, '
    '"confidence": "high"|"medium"|"low"}. '
    "Never provide a rating or a tasting comment. Use null when unsure."
)


class EnrichmentError(RuntimeError):
    pass


def _extract_json(text: str) -> dict:
    """Models like to wrap JSON in prose or code fences - dig it out."""
    if not text:
        return {}
    text = text.strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        pass
    depth = 0
    start = -1
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    parsed = json.loads(text[start : i + 1])
                    return parsed if isinstance(parsed, dict) else {}
                except json.JSONDecodeError:
                    start = -1
    return {}


def _coerce(raw: dict) -> dict:
    """Normalise/clamp model output into our field domain. Drop anything odd."""
    out: dict = {}
    if not isinstance(raw, dict):
        return out

    def _text(key: str, limit: int) -> None:
        val = raw.get(key)
        if isinstance(val, (list, tuple)):  # e.g. aromas returned as a list
            val = "; ".join(str(x) for x in val if str(x).strip())[:limit]
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            val = str(val)
        if isinstance(val, str):
            val = val.strip()
            if val and val.lower() not in {"null", "none", "n/a", "unknown", "-"}:
                out[key] = val[:limit]

    _text("name", 200)
    _text("maker", 200)
    _text("country", 100)
    _text("region", 150)
    _text("grape", 300)
    _text("aromas", 2000)

    wt = raw.get("wine_type")
    if isinstance(wt, str):
        wt = wt.strip().lower()
        wt = _TYPE_SYNONYMS.get(wt, wt)
        if wt in VALID_TYPES:
            out["wine_type"] = wt
    # Infer the type from the grape/varietal when the model gave neither.
    if "wine_type" not in out:
        g = (out.get("grape") or "").lower()
        for grape, ttype in _GRAPE_TYPE.items():
            if grape in g:
                out["wine_type"] = ttype
                break

    def _num(key: str, lo: float, hi: float, as_int: bool = False) -> None:
        val = raw.get(key)
        if isinstance(val, str):
            m = re.search(r"-?\d+(?:[.,]\d+)?", val)
            val = m.group(0).replace(",", ".") if m else None
        if val is None or isinstance(val, bool):
            return
        try:
            num = float(val)
        except (TypeError, ValueError):
            return
        if not (lo <= num <= hi):
            return
        out[key] = int(round(num)) if as_int else round(num, 2)

    _num("vintage", 1800, 2200, as_int=True)
    _num("sugar_g_l", 0, 500)
    _num("alcohol_pct", 0, 100)
    for gauge in ("acidity", "sweetness", "body", "mouthfeel", "wood"):
        _num(gauge, 0, 5, as_int=True)

    return {k: v for k, v in out.items() if k in ENRICHABLE_FIELDS}


class AIClient:
    """Talks to Ollama natively, or to any OpenAI-compatible endpoint."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def available(self) -> bool:
        return bool(self.settings.ollama_base_url or self.settings.openai_base_url)

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=self.settings.ai_timeout_seconds,
            follow_redirects=False,  # no redirect-based SSRF pivots
        )

    async def vision_label(self, image_bytes: bytes) -> dict:
        b64 = base64.b64encode(image_bytes).decode()
        if self.settings.openai_base_url:
            payload = {
                "model": self.settings.openai_model or self.settings.vision_model,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": _VISION_PROMPT},
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                            },
                        ],
                    }
                ],
                "temperature": 0,
                "stream": False,
            }
            return _extract_json(await self._openai_chat(payload))
        if not self.settings.ollama_base_url:
            raise EnrichmentError("No AI endpoint configured")
        payload = {
            "model": self.settings.vision_model,
            "messages": [{"role": "user", "content": _VISION_PROMPT, "images": [b64]}],
            "stream": False,
            "options": {"temperature": 0},
        }
        return _extract_json(await self._ollama_chat(payload))

    async def text_lookup(self, context: str) -> dict:
        model = (
            self.settings.openai_model
            if self.settings.openai_base_url
            else (self.settings.text_model or self.settings.vision_model)
        )
        prompt = f"{_TEXT_PROMPT}\n\nInformation about the wine:\n{context}"
        if self.settings.openai_base_url:
            payload = {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1,
                "stream": False,
            }
            return _extract_json(await self._openai_chat(payload))
        if not self.settings.ollama_base_url:
            raise EnrichmentError("No AI endpoint configured")
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "options": {"temperature": 0.1},
        }
        return _extract_json(await self._ollama_chat(payload))

    async def _ollama_chat(self, payload: dict) -> str:
        url = f"{self.settings.ollama_base_url.rstrip('/')}/api/chat"
        async with self._client() as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
        return (data.get("message") or {}).get("content", "") or data.get("response", "")

    async def _openai_chat(self, payload: dict) -> str:
        url = f"{self.settings.openai_base_url.rstrip('/')}/chat/completions"
        headers = {}
        if self.settings.openai_api_key:
            headers["Authorization"] = f"Bearer {self.settings.openai_api_key}"
        async with self._client() as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        choices = data.get("choices") or []
        if not choices:
            return ""
        return (choices[0].get("message") or {}).get("content", "") or ""


async def lookup_barcode(settings: Settings, barcode: str) -> tuple[dict, list[str]]:
    """Open Food Facts barcode lookup (free, no key). Returns (fields, sources)."""
    if not settings.web_search_enabled or not barcode.isdigit():
        return {}, []
    url = f"{settings.openfoodfacts_base_url.rstrip('/')}/api/v2/product/{barcode}.json"
    try:
        async with httpx.AsyncClient(
            timeout=settings.web_search_timeout_seconds, follow_redirects=False
        ) as client:
            resp = await client.get(url, headers={"User-Agent": "wine-db/1.0 (self-hosted)"})
            if resp.status_code != 200:
                return {}, []
            data = resp.json()
    except (httpx.HTTPError, json.JSONDecodeError, ValueError) as exc:
        logger.info("barcode lookup failed: %s", exc)
        return {}, []

    if data.get("status") != 1:
        return {}, []
    product = data.get("product") or {}
    raw = {
        "name": product.get("product_name") or product.get("generic_name"),
        "maker": product.get("brands"),
        "country": (product.get("countries") or "").split(",")[0] or None,
        "alcohol_pct": (product.get("nutriments") or {}).get("alcohol_value"),
    }
    fields = _coerce({k: v for k, v in raw.items() if v})
    return fields, [f"openfoodfacts:{barcode}"] if fields else []


async def _duckduckgo_search(settings: Settings, query: str) -> tuple[str, list[str]]:
    """Real web search via DuckDuckGo's HTML endpoint (no API key).

    Returns (context_text, sources). This is the primary internet source - it
    returns general web results about the wine, not grocery barcodes.
    """
    q = query.strip()
    if not q:
        return "", []
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
        )
    }
    lines: list[str] = []
    sources: list[str] = []
    try:
        async with httpx.AsyncClient(
            timeout=settings.web_search_timeout_seconds, follow_redirects=True
        ) as client:
            resp = await client.post(
                "https://html.duckduckgo.com/html/",
                data={"q": q},
                headers=headers,
            )
            resp.raise_for_status()
            html = resp.text
    except (httpx.HTTPError, ValueError) as exc:
        logger.info("DuckDuckGo search failed: %s", exc)
        return "", []

    # Snippets and titles are in adjacent anchors; parse both.
    titles = re.findall(r'class="result__a"[^>]*>(.*?)</a>', html, re.S)
    snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', html, re.S)
    links = re.findall(r'class="result__a"[^>]*href="([^"]+)"', html)
    clean = lambda x: unescape(re.sub(r"<[^>]+>", "", x)).strip()  # noqa: E731

    for i, (title, snippet) in enumerate(zip(titles[:6], snippets[:6])):
        t, s = clean(title), clean(snippet)
        if s:
            lines.append(f"- {t}: {s[:400]}")
            if i < len(links):
                href = links[i]
                # Skip DuckDuckGo ad-redirect and tracking links.
                if href and "duckduckgo.com/y.js" not in href:
                    sources.append(href[:300])
    return "\n".join(lines), sources


async def _searxng_search(settings: Settings, query: str) -> tuple[str, list[str]]:
    url = f"{(settings.searxng_base_url or '').rstrip('/')}/search"
    lines: list[str] = []
    sources: list[str] = []
    try:
        async with httpx.AsyncClient(
            timeout=settings.web_search_timeout_seconds, follow_redirects=False
        ) as client:
            resp = await client.get(
                url,
                params={"q": query, "format": "json", "language": "en"},
                headers={"User-Agent": "wine-db/1.0 (self-hosted)"},
            )
            resp.raise_for_status()
            data = resp.json()
        for item in (data.get("results") or [])[:6]:
            title = str(item.get("title", ""))[:200]
            content = str(item.get("content", ""))[:400]
            link = str(item.get("url", ""))[:300]
            if title or content:
                lines.append(f"- {title}: {content}")
            if link:
                sources.append(link)
    except (httpx.HTTPError, json.JSONDecodeError, ValueError) as exc:
        logger.info("SearxNG search failed: %s", exc)
    return "\n".join(lines), sources


async def search_web(settings: Settings, query: str) -> tuple[str, list[str]]:
    """Internet lookup for wine details -> (raw_context_text, sources).

    Uses a real search engine (DuckDuckGo HTML by default, or a self-hosted
    SearxNG if configured). The structured extraction is done afterwards by the
    local summariser model, so the context is left as raw evidence.
    """
    if not (settings.web_search_enabled and query.strip()):
        return "", []
    if settings.searxng_base_url:
        return await _searxng_search(settings, query)
    return await _duckduckgo_search(settings, query)


_SUMMARY_PROMPT = (
    "You are extracting structured wine facts from search-engine results. "
    "Return ONLY compact JSON with these keys: "
    '{"name": str|null, "maker": str|null, "wine_type": "red"|"white"|"rose"|'
    '"sparkling"|"other"|null, "country": str|null, "region": str|null, '
    '"vintage": int|null, "grape": str|null, "sugar_g_l": number|null, '
    '"alcohol_pct": number|null, "aromas": str|null}. '
    "Use only facts present in the text. Use null when the text does not say. "
    "Never provide a rating or tasting comment."
)


async def summarize_search(client: "AIClient", context: str) -> dict:
    """Run raw search context through the local summariser model."""
    prompt = f"{_SUMMARY_PROMPT}\n\nSearch results:\n{context}"
    if client.settings.openai_base_url:
        payload = {
            "model": client.settings.openai_model or client.settings.summary_model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
            "stream": False,
        }
        return _extract_json(await client._openai_chat(payload))
    if not client.settings.ollama_base_url:
        raise EnrichmentError("No AI endpoint configured")
    payload = {
        "model": client.settings.summary_model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "options": {"temperature": 0.1},
    }
    return _extract_json(await client._ollama_chat(payload))
