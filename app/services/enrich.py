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
    '"alcohol_pct": number|null, "aromas": str|null, "acidity": 0-3|null, '
    '"sweetness": 0-3|null, "body": 0-3|null, "mouthfeel": 0-3|null, "wood": 0-3|null, '
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
    # Structure gauges are a simple 0-3 scale (0 = "no such taste", null = unassessed).
    for gauge in ("acidity", "sweetness", "body", "mouthfeel", "wood"):
        _num(gauge, 0, 3, as_int=True)
    # When the model returns prose instead of a number (common from the web
    # summariser), map well-known descriptors onto the 0-3 scale. This lets the
    # structure parameters be filled from internet text, not just a numeric model.
    for gauge in ("acidity", "sweetness", "body", "mouthfeel", "wood"):
        if gauge not in out:
            prose_level = _gauge_word_level(raw.get(gauge))
            if prose_level is not None:
                out[gauge] = prose_level

    return {k: v for k, v in out.items() if k in ENRICHABLE_FIELDS}


# Descriptive words the (web) summariser returns for the structure gauges,
# mapped onto the 0-3 scale. 0 explicitly means "no such taste" (e.g. "dry",
# "unoaked"), so those words yield 0 - not "unknown".
_GAUGE_WORDS = {
    "acidity": [
        (3, ("high acidity", "very acidic", "bracing", "crisp", "racy", "zesty", "tart")),
        (2, ("good acidity", "lively acidity", "fresh", "bright", "vibrant")),
        (1, ("soft acidity", "gentle acidity", "low acid", "mild acid")),
        (0, ("no acidity", "flat", "flabby")),
    ],
    "sweetness": [
        (3, ("very sweet", "sweet", "dessert", "syrupy", "luscious")),
        (2, ("off-dry", "semi-sweet", "medium sweet")),
        (1, ("lightly sweet", "hint of sweetness", "touch of sweetness")),
        (0, ("dry", "bone dry", "brut", "extra brut", "unsweetened")),
    ],
    "body": [
        (3, ("full-bodied", "full body", "massive", "weighty", "opulent", "concentrated")),
        (2, ("medium-bodied", "medium body", "rounded", "rich")),
        (1, ("light-bodied", "light body", "lean", "delicate", "ethereal")),
        (0, ("watery", "thin", "no body")),
    ],
    "mouthfeel": [
        (3, ("tannic", "astringent", "grippy", "chewy", "structured")),
        (2, ("firm", "textured", "velvety")),
        (1, ("soft", "smooth", "silky")),
        (0, ("flabby", "flat mouthfeel")),
    ],
    "wood": [
        (3, ("heavily oaked", "heavy oak", "toasty oak", "pronounced vanilla", "new oak")),
        (2, ("oaked", "oak-aged", "barrel-aged", "vanilla", "spicy oak")),
        (1, ("lightly oaked", "subtle oak", "hint of oak", "touch of wood")),
        (0, ("unoaked", "un-oaked", "stainless steel", "no oak", "steel fermented")),
    ],
}


def _gauge_word_level(value) -> int | None:
    """Map a prose descriptor onto the 0-3 structure scale, else None.

    The numeric path (_num) is preferred; this only fires when the model sent
    words instead of a number. An exact substring match wins; otherwise None so
    we never guess.
    """
    if not isinstance(value, str):
        return None
    text = value.lower()
    for gauge, levels in _GAUGE_WORDS.items():
        for level, words in levels:
            for word in words:
                if word in text:
                    return level
    return None


class AIClient:
    """Talks to a vision provider and a summary provider, each of which can be
    either Ollama or any OpenAI-compatible endpoint configured separately."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def vision_available(self) -> bool:
        return bool(self.settings.vision_base_url)

    @property
    def summary_available(self) -> bool:
        return bool(self.settings.summary_base_url or self.settings.vision_base_url)

    @property
    def available(self) -> bool:
        # Vision is the primary stage; keep the old name meaning "can read labels".
        return self.vision_available

    @staticmethod
    def _openai_url(base_url: str) -> str:
        """Normalise any OpenAI-compatible base to .../v1/chat/completions.

        Accepts a bare Ollama host (http://host:11434), a /v1 root
        (https://api.openai.com/v1) or a full /v1/chat/completions URL.
        """
        u = base_url.rstrip("/")
        if u.endswith("/chat/completions"):
            return u
        if u.endswith("/v1"):
            u = u[: -len("/v1")].rstrip("/")
        return u + "/v1/chat/completions"

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=self.settings.ai_timeout_seconds,
            follow_redirects=False,  # no redirect-based SSRF pivots
        )

    async def vision_label(self, image_bytes: bytes) -> dict:
        b64 = base64.b64encode(image_bytes).decode()
        s = self.settings
        if not s.vision_base_url:
            raise EnrichmentError("No vision endpoint configured")
        payload = {
            "model": s.vision_model,
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
        return _extract_json(await self._openai_chat(payload, s.vision_base_url, s.vision_api_key))

    async def text_lookup(self, context: str) -> dict:
        s = self.settings
        model = s.text_model or s.vision_model
        prompt = f"{_TEXT_PROMPT}\n\nInformation about the wine:\n{context}"
        if not s.vision_base_url:
            raise EnrichmentError("No vision endpoint configured")
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
            "stream": False,
        }
        return _extract_json(await self._openai_chat(payload, s.vision_base_url, s.vision_api_key))

    async def _openai_chat(self, payload: dict, base_url: str, api_key: str | None) -> str:
        url = self._openai_url(base_url)
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
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


async def search_web(
    settings: Settings, query: str
) -> tuple[str, list[str]]:
    """Internet lookup for wine details -> (raw_context_text, sources).

    Uses a real search engine (DuckDuckGo HTML by default, or a self-hosted
    SearxNG if configured). DuckDuckGo's HTML endpoint is increasingly gated by
    an anti-bot challenge (HTTP 202 with no results), so when the primary engine
    yields nothing we fall back to Wikipedia's keyless API - it reliably returns
    real encyclopedic wine facts. The structured extraction is done afterwards
    by the local summariser model, so the context is left as raw evidence.

    Site-scoped queries (e.g. "... site:saq.com") skip the Wikipedia fallback -
    a literal "site:saq.com" would otherwise pollute the fallback with irrelevant
    hits, and SAQ has no public API, so those queries simply contribute nothing
    when the primary engine is unavailable.
    """
    if not (settings.web_search_enabled and query.strip()):
        return "", []

    if settings.searxng_base_url:
        primary_ctx, primary_src = await _searxng_search(settings, query)
    else:
        primary_ctx, primary_src = await _duckduckgo_search(settings, query)

    # DuckDuckGo returns a 202 anti-bot page with no anchors -> empty context.
    # Fall back to Wikipedia so the enrichment stage still gets real data. Skip
    # the fallback for site-scoped queries (e.g. "site:saq.com") so we don't
    # search Wikipedia for the literal "site:saq.com".
    if "site:" not in query and not primary_ctx.strip():
        wiki_ctx, wiki_src = await _wikipedia_search(settings, query)
        if wiki_ctx.strip():
            return wiki_ctx, wiki_src
    return primary_ctx, primary_src


async def _wikipedia_search(settings: Settings, query: str) -> tuple[str, list[str]]:
    """Keyless Wikipedia fallback: search, then pull the lead extract of the
    top hit. Returns (context_text, sources) with no API key required."""
    q = query.strip()
    if not q:
        return "", []
    try:
        async with httpx.AsyncClient(
            timeout=settings.web_search_timeout_seconds, follow_redirects=True
        ) as client:
            # 1) Find the best-matching article title.
            search_resp = await client.get(
                "https://en.wikipedia.org/w/api.php",
                params={
                    "action": "query",
                    "list": "search",
                    "srsearch": q,
                    "srlimit": 3,
                    "format": "json",
                },
                headers={
                    "User-Agent": "wine-db/1.0 (self-hosted wine catalogue; +https://github.com/NousResearch/hermes-agent)"
                },
            )
            search_resp.raise_for_status()
            hits = (search_resp.json().get("query") or {}).get("search") or []
            if not hits:
                return "", []
            title = hits[0]["title"]

            # 2) Pull the lead extract (plaintext) of that article.
            ext_resp = await client.get(
                "https://en.wikipedia.org/w/api.php",
                params={
                    "action": "query",
                    "prop": "extracts",
                    "exintro": "1",
                    "explaintext": "1",
                    "titles": title,
                    "redirects": "1",
                    "format": "json",
                },
                headers={
                    "User-Agent": "wine-db/1.0 (self-hosted wine catalogue; +https://github.com/NousResearch/hermes-agent)"
                },
            )
            ext_resp.raise_for_status()
            pages = (ext_resp.json().get("query") or {}).get("pages") or {}
            extract = ""
            for page in pages.values():
                extract = (page.get("extract") or "").strip()
                if extract:
                    break
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        logger.info("Wikipedia search failed: %s", exc)
        return "", []

    if not extract:
        return "", []
    source = "https://en.wikipedia.org/wiki/" + title.replace(" ", "_")
    return f"- {title}: {extract[:1200]}", [source]


_SUMMARY_PROMPT = (
    "You are extracting structured wine facts from search-engine results. "
    "Return ONLY compact JSON with these keys: "
    '{"name": str|null, "maker": str|null, "wine_type": "red"|"white"|"rose"|'
    '"sparkling"|"other"|null, "country": str|null, "region": str|null, '
    '"vintage": int|null, "grape": str|null, "sugar_g_l": number|null, '
    '"alcohol_pct": number|null, "aromas": str|null, '
    '"acidity": 0-3|null, "sweetness": 0-3|null, "body": 0-3|null, '
    '"mouthfeel": 0-3|null, "wood": 0-3|null}. '
    "Score the structure gauges on a 0-3 scale where 0 means 'no such taste' "
    "(e.g. a 'dry, unoaked' wine is sweetness 0 and wood 0). "
    "Use only facts present in the text. Use null when the text does not say. "
    "Never provide a rating or tasting comment."
)


async def summarize_search(client: "AIClient", context: str) -> dict:
    """Run raw search context through the summary provider's model.

    Falls back to the vision provider when no summary-specific provider is
    configured (the common case: one OpenAI-compatible endpoint serves both).
    """
    s = client.settings
    prompt = f"{_SUMMARY_PROMPT}\n\nSearch results:\n{context}"
    if s.summary_base_url:
        payload = {
            "model": s.summary_model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
            "stream": False,
        }
        return _extract_json(await client._openai_chat(payload, s.summary_base_url, s.summary_api_key))
    if s.vision_base_url:
        payload = {
            "model": s.summary_model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
            "stream": False,
        }
        return _extract_json(await client._openai_chat(payload, s.vision_base_url, s.vision_api_key))
    raise EnrichmentError("No AI endpoint configured")
