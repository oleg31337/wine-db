"""Scanning & enrichment.

`POST /api/scan/label`  - photo of the (front or back) label -> suggested fields
`POST /api/scan/lookup` - barcode / typed name -> suggested fields

The response only ever contains SUGGESTIONS. Nothing is written to the database
here, and fields the caller already filled in are stripped from the suggestion so
user-entered values can never be overwritten. Ratings and comments are never
suggested.
"""

from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status

from app.config import Settings, get_settings
from app.models import User
from app.schemas import EnrichRequest, EnrichResponse
from app.security import current_user, require_csrf
from app.services import images
from app.services.enrich import (
    ENRICHABLE_FIELDS,
    AIClient,
    EnrichmentError,
    _coerce,
    lookup_barcode,
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


async def _enrich_from_context(
    settings: Settings,
    *,
    barcode: str | None,
    hints: dict,
    label_text: str | None,
    sources: list[str],
    messages: list[str],
) -> tuple[dict, str]:
    """Internet first, then the model's own knowledge."""
    suggestion: dict = {}
    confidence = "low"

    if barcode:
        fields, src = await lookup_barcode(settings, barcode)
        if fields:
            suggestion.update(fields)
            sources.extend(src)
            confidence = "medium"
            messages.append("Matched the barcode in the Open Food Facts database.")

    query_bits = [
        str(hints.get("name") or ""),
        str(hints.get("maker") or ""),
        str(hints.get("vintage") or ""),
        "wine",
    ]
    query = " ".join(b for b in query_bits if b).strip()
    web_context, web_sources = await search_web(settings, query) if query else ("", [])
    # SAQ (saq.com) is a large wine catalogue; try a site-scoped search
    # alongside the general one so its product pages are also considered when
    # the engine supports it. No Wikipedia fallback here (a bare "site:saq.com"
    # would pollute the fallback), and SAQ has no public API, so this simply
    # contributes nothing when the primary engine is unavailable.
    if query:
        saq_context, saq_sources = await search_web(settings, query + " site:saq.com")
        if saq_context:
            web_context = (web_context + "\n" + saq_context).strip()
            web_sources = web_sources + saq_sources
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
            if barcode:
                context_lines.append(f"barcode: {barcode}")
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
    barcode: str | None = Form(default=None, max_length=64),
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
) -> EnrichResponse:
    raw = await images.read_upload(file, settings.max_upload_bytes)
    ai_bytes, _store_bytes = images.normalize_ingested_image(raw, settings.max_upload_bytes)

    if barcode and not barcode.isdigit():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Barcode must be digits only"
        )

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

    net_suggestion, confidence = await _enrich_from_context(
        settings,
        barcode=barcode,
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
    )


@router.post("/lookup", response_model=EnrichResponse)
async def scan_lookup(
    payload: EnrichRequest,
    _user: User = Depends(current_user),
    _: None = Depends(require_csrf),
    settings: Settings = Depends(get_settings),
) -> EnrichResponse:
    """Barcode-only or typed-name lookup, no photo needed."""
    if payload.barcode and not payload.barcode.isdigit():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Barcode must be digits only"
        )
    if not any([payload.barcode, payload.name, payload.maker, payload.label_text]):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Provide a barcode, a name, or label text",
        )

    already = _filled_fields({"name": payload.name, "maker": payload.maker})
    sources: list[str] = []
    messages: list[str] = []
    suggestion, confidence = await _enrich_from_context(
        settings,
        barcode=payload.barcode,
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
        "barcode_lookup": settings.web_search_enabled,
    }
