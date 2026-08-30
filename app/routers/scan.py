"""Scanning & enrichment.

`POST /api/scan/label`  - photo of the (front or back) label -> suggested fields
`POST /api/scan/lookup` - typed name -> suggested fields

The response only ever contains SUGGESTIONS. Nothing is written to the database
here, and fields the caller already filled in are stripped from the suggestion so
user-entered values can never be overwritten. Ratings and comments are never
suggested.
"""

from __future__ import annotations

import asyncio
import httpx
import logging
import re
import unicodedata
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_db
from app.models import User, Wine
from app.schemas import EnrichRequest, EnrichResponse, WineOut
from app.security import current_user, require_csrf
from app.services import images
from app.services.enrich import (
    ENRICHABLE_FIELDS,
    AIClient,
    EnrichmentError,
    _coerce,
    search_web,
    summarize_search,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/scan", tags=["scan"])

BACK_LABEL_HINT = (
    "Not much could be read from this label. Try scanning the BACK label - it usually "
    "lists the region, grape variety, alcohol and sugar content."
)


def _filter_empty_only(suggestion: dict, already_filled: set[str]) -> dict:
    """Only propose values for fields the user left empty."""
    return {
        k: v
        for k, v in suggestion.items()
        if k in ENRICHABLE_FIELDS and k not in already_filled and v not in (None, "")
    }


def _filled_fields(form_values: dict) -> set[str]:
    return {
        k
        for k, v in form_values.items()
        if v is not None and str(v).strip() != "" and k in ENRICHABLE_FIELDS
    }


def _serialize_existing(db: Session, user_id: str, wines: list[Wine]) -> list[WineOut]:
    """Render a small card preview for the duplicate-check banner.

    We deliberately do NOT pull in comments/favorite-list ids (that's the full
    WineDetail path). The banner only needs the identifying fields plus the
    rating summary so the user can recognise the wine and open its real card.
    """
    from app.routers.wines import _comment_counts, _my_ratings, _rating_stats

    ids = [w.id for w in wines]
    stats = _rating_stats(db, ids)
    mine = _my_ratings(db, user_id, ids)
    comments = _comment_counts(db, ids)
    out = []
    for w in wines:
        avg, count = stats.get(w.id, (None, 0))
        out.append(
            WineOut(
                id=w.id,
                name=w.name,
                maker=w.maker,
                wine_type=w.wine_type,
                country=w.country,
                region=w.region,
                vintage=w.vintage,
                grape=w.grape,
                sugar_g_l=w.sugar_g_l,
                alcohol_pct=w.alcohol_pct,
                aromas=w.aromas,
                acidity=w.acidity,
                sweetness=w.sweetness,
                body=w.body,
                mouthfeel=w.mouthfeel,
                wood=w.wood,
                photo_url=f"/api/wines/{w.id}/photo" if w.photo_path else None,
                created_at=w.created_at,
                updated_at=w.updated_at,
                average_rating=avg,
                rating_count=count,
                my_rating=mine.get(w.id),
                comment_count=comments.get(w.id, 0),
            )
        )
    return out


# Words that carry almost no identifying power on a wine label (the word
# "chateau"/"domaine"/"estate" appears on thousands of different wines) or are
# noise. We still keep them for context but require a *strong* token to match.
_WEAK_TOKENS = {
    "chateau", "château", "domaine", "estate", "winery", "the", "de", "du",
    "des", "la", "le", "les", "of", "and", "reserve", "reserva", "cuvée",
    "cuvee", "grand", "vineyard", "family", "maison", "cantina", "weingut",
    "bodega", "cellars", "cellar", "vignobles", "vignerons", "clos", "castello",
    "tenuta", "abbey", "house",
}


def _fold(text: str) -> str:
    """Lowercase + strip accents so "Château" and "Chateau" compare equal."""
    if not text:
        return ""
    normalized = unicodedata.normalize("NFKD", str(text))
    return "".join(c for c in normalized if not unicodedata.combining(c)).lower()


def _significant_tokens(text: str) -> set[str]:
    """Accent-folded, de-pluralised tokens of a name, minus weak/short words."""
    folded = _fold(text)
    words = re.split(r"[^a-z0-9]+", folded)
    out = set()
    for w in words:
        if not w or len(w) < 3:
            continue
        if w in _WEAK_TOKENS:
            continue
        # crude singular form so "commanderie"/"commanderies" align
        if w.endswith("s") and len(w) > 4:
            w = w[:-1]
        out.add(w)
    return out


def _lev(a: str, b: str) -> int:
    """Classic Levenshtein edit distance (cheap; tokens are short)."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _tokens_match(stored_tok: str, vision_tok: str) -> bool:
    """True when two significant tokens are the same OR close enough that a label
    misread (one dropped/accented letter) would explain the difference."""
    if stored_tok == vision_tok:
        return True
    n = max(len(stored_tok), len(vision_tok))
    if n < 4:
        return False
    # allow 1 edit for short words, 2 for longer ones
    return _lev(stored_tok, vision_tok) <= (1 if n <= 6 else 2)


def _name_similarity(a: str, b: str) -> float:
    """Token-overlap ratio in [0, 1] between two wine names.

    Uses the *smaller* set of significant tokens as the denominator so a truncated
    label reading ("commanderie bardélet") still scores 1.0 against the full
    stored name ("château la commanderie du bardélet"), while an unrelated name
    scores ~0. Token comparison is edit-distance tolerant so a single-name label
    read with a typo (e.g. vision "Roussane" vs stored "Roussanne") still matches.
    """
    ta, tb = _significant_tokens(a), _significant_tokens(b)
    if not ta or not tb:
        return 0.0
    matched = 0
    for vtok in ta:
        if any(_tokens_match(stok, vtok) for stok in tb):
            matched += 1
    denom = min(len(ta), len(tb))
    return matched / denom


def _lookup_existing(
    db: Session, *, name: str | None, maker: str | None, limit: int = 5
) -> list[Wine]:
    """Find wines that look like the one being scanned.

    Matching is deliberately LOOSE: the vision model frequently drops accents,
    truncates long names, or returns only part of the label. We accent-fold both
    sides and compare on *significant* token overlap, ignoring the vintage/year
    entirely (a 2018 and a 2020 of the same wine are the same entry).

    A "significant token" is any lowercased, accent-stripped word of >= 3 chars
    that isn't a generic wine-word (château, domaine, de, la, ...). The score is
    the fraction of the *shorter* name's significant tokens that also appear in
    the other name, so a partial label reading still matches the full stored name.
    """
    if not name or not name.strip():
        # Without a name we can only fall back to an exact maker match.
        if maker and maker.strip():
            m = f"%{maker.strip().lower()}%"
            return list(
                db.scalars(
                    select(Wine)
                    .where(func.lower(func.coalesce(Wine.maker, "")).like(m, escape="\\"))
                    .order_by(Wine.created_at.desc())
                    .limit(limit)
                )
            )
        return []

    candidates = db.scalars(
        select(Wine).where(Wine.name.is_not(None)).order_by(Wine.created_at.desc()).limit(200)
    ).all()

    scored = []
    for w in candidates:
        score = _name_similarity(w.name, name)
        if maker and maker.strip():
            if _fold(maker.strip()) in _fold(w.maker or ""):
                score = max(score, 0.9)
        if score >= 0.5:
            scored.append((score, w))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [w for _, w in scored[:limit]]


# Large, well-known wine catalogues we scope the web search to. Each is queried
# in parallel (a site-scoped search per site) so the enrichment gathers facts
# from several specialised databases at once instead of a single generic web
# search. Add a domain here to include another catalogue - no other wiring needed.
_WEB_SOURCES = ("cellartracker.com", "vivino.com", "saq.com")


async def _enrich_from_context(
    settings: Settings,
    *,
    hints: dict,
    label_text: str | None,
    sources: list[str],
    messages: list[str],
) -> tuple[dict, str]:
    """Internet first, then the model's own knowledge."""
    suggestion: dict = {}
    confidence = "low"

    query_bits = [
        str(hints.get("name") or ""),
        str(hints.get("maker") or ""),
        str(hints.get("vintage") or ""),
        "wine",
    ]
    query = " ".join(b for b in query_bits if b).strip()
    web_context, web_sources = await search_web(settings, query) if query else ("", [])

    # Large wine catalogues are queried with site-scoped searches, all fanned out
    # in parallel via asyncio.gather so they run simultaneously. CellarTracker,
    # Vivino and SAQ are large wine databases; their product pages are considered
    # when the engine supports site-scoping. No Wikipedia fallback applies to these
    # (a bare "site:..." would pollute the fallback) and none has a public API, so
    # each simply contributes nothing when the primary engine is unavailable.
    # The list of sites lives in _WEB_SOURCES.
    if query:
        site_tasks = [search_web(settings, f"{query} site:{site}") for site in _WEB_SOURCES]
        for ctx, src in await asyncio.gather(*site_tasks):
            if ctx:
                web_context = (web_context + "\n" + ctx).strip()
                web_sources = web_sources + src
    if web_context:
        sources.extend(web_sources)
        confidence = "medium"
        messages.append("Found web results for this wine.")

    ai = AIClient(settings)
    if ai.available:
        # Preferred path: summarise the raw internet results with a fast local
        # model and fold the structured facts into the suggestion.
        web_used = False
        if web_context:
            try:
                summary_fields = _coerce(await summarize_search(ai, web_context))
                for key, value in summary_fields.items():
                    suggestion.setdefault(key, value)
                if summary_fields:
                    web_used = True
                    messages.append("Filled details from a web search (check them).")
            except (httpx.HTTPError, EnrichmentError, ValueError) as exc:
                logger.warning("search summariser failed: %s", exc)
                messages.append("The summariser model could not be reached.")

        # Fallback / completion: when the web had nothing usable, ask the model
        # for its best-effort knowledge (passing the raw context so it can still
        # salvage any real wine data the summariser skipped). Never invents
        # ratings/comments.
        if not suggestion:
            context_lines = [f"{k}: {v}" for k, v in hints.items() if v]
            if web_context:
                context_lines.append(f"web search results:\n{web_context}")
            if label_text:
                context_lines.append(f"label text: {label_text[:2000]}")
            if context_lines:
                try:
                    ai_fields = _coerce(await ai.text_lookup("\n".join(context_lines)))
                    for key, value in ai_fields.items():
                        suggestion.setdefault(key, value)
                    if ai_fields:
                        messages.append(
                            "No reliable web match - filled best-effort details from the model. "
                            "Verify them."
                        )
                        confidence = "medium"
                except (httpx.HTTPError, EnrichmentError, ValueError) as exc:
                    logger.warning("AI text lookup failed: %s", exc)
                    messages.append("The AI model could not be reached for extra details.")
        elif not web_used:
            # Web context existed but the summariser declined it (e.g. only food
            # products came back). Say so instead of claiming success.
            messages.append("Web results were not about this wine - review them before saving.")

    return suggestion, confidence


@router.post("/label", response_model=EnrichResponse)
async def scan_label(
    file: UploadFile = File(...),
    name: str | None = Form(default=None, max_length=200),
    maker: str | None = Form(default=None, max_length=200),
    country: str | None = Form(default=None, max_length=100),
    region: str | None = Form(default=None, max_length=150),
    grape: str | None = Form(default=None, max_length=300),
    vintage: str | None = Form(default=None, max_length=8),
    is_back_label: bool = Form(default=False),
    _user: User = Depends(current_user),
    _: None = Depends(require_csrf),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> EnrichResponse:
    raw = await images.read_upload(file, settings.max_upload_bytes)
    ai_bytes, _store_bytes = images.normalize_ingested_image(raw, settings.max_upload_bytes)

    filled_form = {
        "name": name,
        "maker": maker,
        "country": country,
        "region": region,
        "grape": grape,
        "vintage": vintage,
    }
    already = _filled_fields(filled_form)

    sources: list[str] = []
    messages: list[str] = []
    ai = AIClient(settings)
    label_fields: dict = {}
    label_text = None
    legible = False

    existing_matches: list[WineOut] = []
    if ai.available:
        try:
            raw_vision = await ai.vision_label(ai_bytes)
            label_fields = _coerce(raw_vision)
            label_text = str(raw_vision.get("raw_text") or "")[:4000] or None
            legible = bool(raw_vision.get("legible")) or bool(label_fields) or bool(label_text)
            if legible:
                sources.append(f"vision:{settings.vision_model}")
                messages.append("Read the label with the vision model.")
        except httpx.HTTPStatusError as exc:
            logger.warning("vision model HTTP error: %s", exc)
            messages.append(
                "The vision model rejected the request - check VISION_MODEL supports images."
            )
        except (httpx.HTTPError, EnrichmentError, ValueError) as exc:
            logger.warning("vision model failed: %s", exc)
            messages.append("The vision model could not be reached.")
    else:
        messages.append("No AI endpoint is configured on the server.")

    hints = {
        "name": name or label_fields.get("name"),
        "maker": maker or label_fields.get("maker"),
        "country": country or label_fields.get("country"),
        "region": region or label_fields.get("region"),
        "grape": grape or label_fields.get("grape"),
        "vintage": vintage or label_fields.get("vintage"),
    }

    # After the front label is read, check whether this wine is already in the
    # collection. Match on name (+ maker when known); never on the vintage/year.
    # Skip the check for back-label scans - the front label already did it, and a
    # back label may not name the wine at all.
    if not is_back_label and (hints["name"] or hints["maker"]):
        matches = _lookup_existing(db, name=hints["name"], maker=hints["maker"])
        if matches:
            existing_matches = _serialize_existing(db, _user.id, matches)
            messages.append(
                "This wine may already be in your collection - check the match below "
                "before saving a duplicate."
            )

    net_suggestion, confidence = await _enrich_from_context(
        settings,
        hints=hints,
        label_text=label_text,
        sources=sources,
        messages=messages,
    )

    # Label evidence wins over inference; both are only offered for empty fields.
    merged = {**net_suggestion, **label_fields}
    suggestion = _filter_empty_only(merged, already)

    strong = {"name", "maker", "country", "region", "grape", "vintage"}
    if label_fields and len(set(label_fields) & strong) >= 3:
        confidence = "high"
    elif label_fields:
        confidence = "medium" if confidence != "high" else confidence

    need_back = (not is_back_label) and (
        not legible or len({*suggestion, *already} & strong) < 3 or confidence == "low"
    )
    if need_back:
        messages.append(BACK_LABEL_HINT)

    return EnrichResponse(
        suggestion=suggestion,
        sources=sources,
        confidence=confidence,
        need_back_label=need_back,
        messages=messages,
        back_label_text=label_text if is_back_label else None,
        existing_matches=existing_matches,
    )


@router.post("/lookup", response_model=EnrichResponse)
async def scan_lookup(
    payload: EnrichRequest,
    _user: User = Depends(current_user),
    _: None = Depends(require_csrf),
    settings: Settings = Depends(get_settings),
) -> EnrichResponse:
    """Typed-name or label-text lookup, no photo needed."""
    if not any([payload.name, payload.maker, payload.label_text]):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Provide a name or label text",
        )

    already = _filled_fields({"name": payload.name, "maker": payload.maker})
    sources: list[str] = []
    messages: list[str] = []
    suggestion, confidence = await _enrich_from_context(
        settings,
        hints={"name": payload.name, "maker": payload.maker},
        label_text=payload.label_text,
        sources=sources,
        messages=messages,
    )
    filtered = _filter_empty_only(suggestion, already)
    need_back = not filtered or confidence == "low"
    if need_back and payload.ask_back_label:
        messages.append(BACK_LABEL_HINT)
    return EnrichResponse(
        suggestion=filtered,
        sources=sources,
        confidence=confidence,
        need_back_label=need_back,
        messages=messages,
    )


@router.get("/status")
def scan_status(
    _user: User = Depends(current_user), settings: Settings = Depends(get_settings)
) -> dict:
    """What the frontend may offer - never exposes URLs, keys or other config."""
    ai = AIClient(settings)
    return {
        "ai_available": ai.available,
        "web_search_enabled": settings.web_search_enabled,
    }
