"""Safe image ingestion.

Never trust the uploaded bytes: we re-encode through Pillow, which strips EXIF
and guarantees the stored file really is an image (no polyglot/SVG/HTML payloads).

Canonical storage contract
--------------------------
*Every* image that enters the system - whether uploaded from a phone, pasted,
or fetched from a web page - is normalised to a **single output format
(JPEG)** at a **single maximum resolution per role**:
    * ``AI_*``       -> the copy sent to the vision model (high enough to OCR
                       a label), capped at ``AI_DIMENSION``.
    * ``STORE_*``    -> the copy persisted in the database and shown in the
                       card frames, capped at ``STORE_DIMENSION``.
Both copies are always JPEG, so all images stored in the database share the
same format and the same maximum resolution regardless of the incoming source
format (JPEG/PNG/WebP/AVIF/GIF/BMP/TIFF/HEIC/ICO/MPO...).
"""

from __future__ import annotations

import io
import os
import secrets
from pathlib import Path

from fastapi import HTTPException, UploadFile, status
from PIL import Image, UnidentifiedImageError

# Output is always JPEG - universally supported by vision models (Ollama
# qwen2.5vl reads data:image/jpeg) and the smallest reasonable still-format.
STORE_FORMAT = "JPEG"
AI_FORMAT = "JPEG"
# The vision model needs enough resolution to OCR a label, so we feed it this.
AI_DIMENSION = 1600
# The stored display copy only ever shows in a ~260px detail frame / 62px
# mini-card, so a much smaller size is plenty and saves disk + bandwidth.
STORE_DIMENSION = 800
MAX_PIXELS = 40_000_000

Image.MAX_IMAGE_PIXELS = MAX_PIXELS

# Formats Pillow can *decode* on this build. Everything listed here is accepted
# on ingest and converted to the canonical JPEG above. (SVG/HTML are not in this
# list because Pillow cannot decode them - they fail the decode check and are
# rejected.) AVIF/WebP/etc. are increasingly common on the web, so they must be
# accepted even though they are never stored as-is.
DECODABLE_FORMATS = {
    "JPEG", "PNG", "WEBP", "GIF", "BMP", "TIFF", "AVIF",
    "HEIF", "HEIC", "ICO", "MPO",
}


def _open_validated(raw: bytes) -> Image.Image:
    try:
        probe = Image.open(io.BytesIO(raw))
        fmt = (probe.format or "").upper()
        if fmt not in DECODABLE_FORMATS:
            # If Pillow recognised it but we don't list it, still reject rather
            # than guess; everything we intend to accept is in the set above.
            raise HTTPException(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                detail=f"Unsupported image format: {fmt or 'unknown'}",
            )
        probe.verify()  # structural check; invalidates the handle
    except HTTPException:
        raise
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="File is not a valid image"
        ) from exc
    return Image.open(io.BytesIO(raw))


def _encode(img: Image.Image, dimension: int, fmt: str) -> bytes:
    """Re-encode to the canonical format at <= ``dimension`` px (long edge).

    ``convert("RGB")`` flattens alpha/transparency (JPEG has none) and removes
    any embedded colour profile / EXIF, which is exactly what we want for a
    clean, model-friendly, storage-friendly copy.
    """
    img = img.convert("RGB")
    img.thumbnail((dimension, dimension), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, format=fmt, quality=85, optimize=True)
    return out.getvalue()


def normalize_image(raw: bytes, max_bytes: int) -> tuple[bytes, bytes]:
    """Validate + re-encode to clean JPEGs (metadata stripped).

    Returns ``(ai_bytes, store_bytes)``:
      * ``ai_bytes`` keeps resolution high enough for a vision model to read
        the label (AI_DIMENSION);
      * ``store_bytes`` is a smaller display copy (STORE_DIMENSION) used for the
        wine's stored photo - it is only ever shown in a small frame, so the
        extra pixels would be wasted disk and bandwidth.

    Both copies are always JPEG, regardless of the incoming format, so every
    stored image in the database shares one format and one maximum resolution.
    """
    if not raw:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Empty upload")
    if len(raw) > max_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Image exceeds {max_bytes // (1024 * 1024)} MB limit",
        )
    img = _open_validated(raw)
    return (
        _encode(img, AI_DIMENSION, AI_FORMAT),
        _encode(img, STORE_DIMENSION, STORE_FORMAT),
    )


# Single entry point for ANY ingestion source (upload, paste, web fetch). The
# name documents the invariant: whatever arrives is normalised to the canonical
# JPEG contract before it touches the database or the vision model.
normalize_ingested_image = normalize_image


async def read_upload(upload: UploadFile, max_bytes: int) -> bytes:
    """Stream the upload with a hard byte cap (never buffer unbounded input)."""
    chunks: list[bytes] = []
    total = 0
    while chunk := await upload.read(64 * 1024):
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"Image exceeds {max_bytes // (1024 * 1024)} MB limit",
            )
        chunks.append(chunk)
    return b"".join(chunks)


def store_photo(uploads_dir: str, data: bytes) -> str:
    """Persist normalized JPEG bytes; returns the opaque relative filename."""
    directory = Path(uploads_dir)
    directory.mkdir(parents=True, exist_ok=True)
    filename = f"{secrets.token_hex(16)}.jpg"
    target = directory / filename
    tmp = directory / f".{filename}.tmp"
    tmp.write_bytes(data)
    os.replace(tmp, target)
    return filename


def resolve_photo(uploads_dir: str, filename: str) -> Path:
    """Resolve a stored photo path, refusing traversal attempts."""
    base = Path(uploads_dir).resolve()
    candidate = (base / filename).resolve()
    if candidate.parent != base or not candidate.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Photo not found")
    return candidate


def delete_photo(uploads_dir: str, filename: str | None) -> None:
    if not filename:
        return
    try:
        resolve_photo(uploads_dir, filename).unlink(missing_ok=True)
    except HTTPException:
        pass

