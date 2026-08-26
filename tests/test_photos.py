"""Photo upload: validation, normalisation, traversal safety."""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image

from tests.conftest import create_wine, make_image


def upload(api, wine_id, data, filename="label.jpg", content_type="image/jpeg"):
    return api.put(
        f"/api/wines/{wine_id}/photo",
        files={"file": (filename, data, content_type)},
    )


def test_upload_and_fetch_photo(api, user):
    wine = create_wine(api)
    resp = upload(api, wine["id"], make_image())
    assert resp.status_code == 200
    assert resp.json()["photo_url"] == f"/api/wines/{wine['id']}/photo"

    photo = api.get(f"/api/wines/{wine['id']}/photo")
    assert photo.status_code == 200
    assert photo.headers["content-type"] == "image/jpeg"
    assert Image.open(io.BytesIO(photo.content)).format == "JPEG"


def test_photo_requires_authentication(api, client, user):
    wine = create_wine(api)
    upload(api, wine["id"], make_image())
    client.cookies.clear()
    assert client.get(f"/api/wines/{wine['id']}/photo").status_code == 401


def test_png_is_accepted_and_converted_to_jpeg(api, user):
    wine = create_wine(api)
    assert upload(api, wine["id"], make_image(fmt="PNG"), "l.png", "image/png").status_code == 200
    photo = api.get(f"/api/wines/{wine['id']}/photo")
    assert Image.open(io.BytesIO(photo.content)).format == "JPEG"


def test_oversized_dimensions_are_downscaled(api, user, settings):
    wine = create_wine(api)
    assert upload(api, wine["id"], make_image(size=(3000, 4000))).status_code == 200
    photo = api.get(f"/api/wines/{wine['id']}/photo")
    img = Image.open(io.BytesIO(photo.content))
    assert max(img.size) <= 800  # stored display copy, not the 1600px AI copy


def test_normalize_image_returns_ai_and_store_copies():
    """The model keeps a high-res copy; storage gets a smaller display copy."""
    from app.services.images import AI_DIMENSION, STORE_DIMENSION, normalize_image

    raw = make_image(size=(4000, 3000))
    ai_bytes, store_bytes = normalize_image(raw, 8 * 1024 * 1024)

    ai = Image.open(io.BytesIO(ai_bytes))
    store = Image.open(io.BytesIO(store_bytes))
    # Both are valid JPEGs, store is the smaller of the two, and store respects
    # the display cap (it only ever shows in a small frame).
    assert ai.format == "JPEG" and store.format == "JPEG"
    assert max(store.size) <= STORE_DIMENSION
    assert max(ai.size) <= AI_DIMENSION
    assert max(store.size) <= max(ai.size)
    assert len(store_bytes) <= len(ai_bytes)


def test_stored_photo_is_display_sized_not_ai_sized(api, user, settings):
    """What gets persisted is the ~800px display copy, not the 1600px AI copy."""
    from app.services.images import STORE_DIMENSION

    wine = create_wine(api)
    upload(api, wine["id"], make_image(size=(4000, 3000)))
    photo = api.get(f"/api/wines/{wine['id']}/photo")
    assert max(Image.open(io.BytesIO(photo.content)).size) <= STORE_DIMENSION + 1


def test_exif_metadata_is_stripped(api, user, settings):
    wine = create_wine(api)
    buf = io.BytesIO()
    img = Image.new("RGB", (400, 500), (10, 20, 30))
    exif = img.getexif()
    exif[271] = "SecretCameraMake"
    img.save(buf, format="JPEG", exif=exif)
    assert upload(api, wine["id"], buf.getvalue()).status_code == 200

    stored = api.get(f"/api/wines/{wine['id']}/photo").content
    assert b"SecretCameraMake" not in stored
    assert not Image.open(io.BytesIO(stored)).getexif().get(271)


def test_non_image_payload_rejected(api, user):
    wine = create_wine(api)
    resp = upload(api, wine["id"], b"#!/bin/sh\nrm -rf /\n", "evil.jpg")
    assert resp.status_code == 400


def test_html_disguised_as_image_rejected(api, user):
    wine = create_wine(api)
    payload = b"<html><script>alert(1)</script></html>"
    assert upload(api, wine["id"], payload, "x.png", "image/png").status_code == 400


def test_svg_is_rejected(api, user):
    wine = create_wine(api)
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    assert upload(api, wine["id"], svg, "x.svg", "image/svg+xml").status_code in (400, 415)


def test_empty_upload_rejected(api, user):
    wine = create_wine(api)
    assert upload(api, wine["id"], b"").status_code == 400


def test_upload_larger_than_limit_rejected(api, user, settings):
    wine = create_wine(api)
    blob = b"\xff\xd8\xff" + b"0" * (settings.max_upload_bytes + 4096)
    assert upload(api, wine["id"], blob).status_code == 413


def test_stored_filename_is_opaque_and_inside_uploads(api, user, settings):
    wine = create_wine(api)
    upload(api, wine["id"], make_image())
    files = list((Path(settings.uploads_dir)).glob("*.jpg"))
    assert len(files) == 1
    assert files[0].name != "label.jpg"
    assert files[0].parent == Path(settings.uploads_dir)


def test_replacing_photo_removes_the_old_file(api, user, settings):
    wine = create_wine(api)
    upload(api, wine["id"], make_image(color=(1, 2, 3)))
    upload(api, wine["id"], make_image(color=(9, 9, 9)))
    assert len(list(Path(settings.uploads_dir).glob("*.jpg"))) == 1


def test_deleting_photo_clears_it(api, user, settings):
    wine = create_wine(api)
    upload(api, wine["id"], make_image())
    assert api.delete(f"/api/wines/{wine['id']}/photo").status_code == 204
    assert api.get(f"/api/wines/{wine['id']}").json()["photo_url"] is None
    assert api.get(f"/api/wines/{wine['id']}/photo").status_code == 404
    assert list(Path(settings.uploads_dir).glob("*.jpg")) == []


def test_deleting_wine_removes_its_photo_file(api, user, settings):
    wine = create_wine(api)
    upload(api, wine["id"], make_image())
    assert api.delete(f"/api/wines/{wine['id']}").status_code == 204
    assert list(Path(settings.uploads_dir).glob("*.jpg")) == []


def test_photo_traversal_is_blocked(settings):
    """A tampered DB value must never read outside the uploads directory."""
    from fastapi import HTTPException

    from app.services.images import resolve_photo

    secret = Path(settings.data_dir) / "secret.txt"
    secret.write_text("classified")
    for evil in ("../secret.txt", "../../etc/passwd", "/etc/passwd", "sub/../../secret.txt"):
        try:
            resolve_photo(settings.uploads_dir, evil)
        except HTTPException as exc:
            assert exc.status_code == 404
        else:
            raise AssertionError(f"traversal not blocked for {evil}")


# ----------------------------------------------------- format-agnostic ingest
def test_all_incoming_formats_normalise_to_jpeg():
    """Web-sourced images arrive in many formats (AVIF/WebP/GIF/BMP/TIFF/ICO);
    every one must be accepted and converted to the canonical JPEG."""
    from app.services.images import normalize_ingested_image

    for fmt in ("PNG", "WEBP", "GIF", "BMP", "TIFF", "ICO", "AVIF"):
        raw = make_image(size=(1200, 900), fmt=fmt)
        ai_bytes, store_bytes = normalize_ingested_image(raw, 8 * 1024 * 1024)
        ai = Image.open(io.BytesIO(ai_bytes))
        store = Image.open(io.BytesIO(store_bytes))
        assert ai.format == "JPEG", f"{fmt} AI copy not JPEG: {ai.format}"
        assert store.format == "JPEG", f"{fmt} store copy not JPEG: {store.format}"


def test_png_with_alpha_flattens_to_rgb_jpeg():
    """Transparency in a PNG source must be flattened (JPEG has no alpha)."""
    from app.services.images import normalize_ingested_image

    buf = io.BytesIO()
    Image.new("RGBA", (500, 400), (10, 20, 30, 128)).save(buf, format="PNG")
    ai_bytes, store_bytes = normalize_ingested_image(buf.getvalue(), 8 * 1024 * 1024)
    assert Image.open(io.BytesIO(store_bytes)).mode == "RGB"


def test_stored_database_images_share_format_and_resolution():
    """The canonical contract: all images persisted in the DB are JPEG and
    capped at the same maximum resolution, regardless of source format."""
    from app.services.images import (
        AI_DIMENSION,
        STORE_DIMENSION,
        normalize_ingested_image,
    )

    for fmt in ("JPEG", "PNG", "WEBP", "AVIF", "GIF", "BMP", "TIFF"):
        raw = make_image(size=(2000, 1500), fmt=fmt)
        _ai_bytes, store_bytes = normalize_ingested_image(raw, 20 * 1024 * 1024)
        store = Image.open(io.BytesIO(store_bytes))
        # Uniform: every stored image is JPEG and at most STORE_DIMENSION.
        assert store.format == "JPEG"
        assert max(store.size) <= STORE_DIMENSION
        # The AI copy (sent to the vision model) is also JPEG and uniform.
        ai = Image.open(io.BytesIO(_ai_bytes))
        assert ai.format == "JPEG"
        assert max(ai.size) <= AI_DIMENSION


def test_webp_image_uploaded_via_api_becomes_jpeg(api, user):
    """End-to-end: a WebP upload through the API is stored as JPEG."""
    wine = create_wine(api)
    resp = upload(api, wine["id"], make_image(fmt="WEBP"), "l.webp", "image/webp")
    assert resp.status_code == 200
    photo = api.get(f"/api/wines/{wine['id']}/photo")
    assert photo.headers["content-type"] == "image/jpeg"
    assert Image.open(io.BytesIO(photo.content)).format == "JPEG"
