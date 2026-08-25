"""Safe image ingestion.

Never trust the uploaded bytes: we re-encode through Pillow, which strips EXIF
and guarantees the stored file really is an image (no polyglot/SVG/HTML payloads).
"""

from __future__ import annotations

import io
import os
import secrets
from pathlib import Path

from fastapi import HTTPException, UploadFile, status
from PIL import Image, UnidentifiedImageError

ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP", "HEIF", "HEIC", "MPO"}
# The vision model needs enough resolution to OCR a label, so we feed it this.
AI_DIMENSION = 1600
# The stored display copy only ever shows in a ~260px detail frame / 62px
# mini-card, so a much smaller size is plenty and saves disk + bandwidth.
STORE_DIMENSION = 800
MAX_PIXELS = 40_000_000

Image.MAX_IMAGE_PIXELS = MAX_PIXELS


def _open_validated(raw: bytes) -> Image.Image:
    try:
        probe = Image.open(io.BytesIO(raw))
        fmt = (probe.format or "").upper()
        if fmt not in ALLOWED_FORMATS:
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


def _to_jpeg(img: Image.Image, dimension: int) -> bytes:
    img = img.convert("RGB")
    img.thumbnail((dimension, dimension), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=85, optimize=True)
    return out.getvalue()


def normalize_image(raw: bytes, max_bytes: int) -> tuple[bytes, bytes]:
    """Validate + re-encode to clean JPEGs (metadata stripped).

    Returns ``(ai_bytes, store_bytes)``:
      * ``ai_bytes`` keeps resolution high enough for a vision model to read
        the label (AI_DIMENSION);
      * ``store_bytes`` is a smaller display copy (STORE_DIMENSION) used for the
        wine's stored photo - it is only ever shown in a small frame, so the
        extra pixels would be wasted disk and bandwidth.
    """
    if not raw:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Empty upload")
    if len(raw) > max_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Image exceeds {max_bytes // (1024 * 1024)} MB limit",
        )
    img = _open_validated(raw)
    return _to_jpeg(img, AI_DIMENSION), _to_jpeg(img, STORE_DIMENSION)


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
