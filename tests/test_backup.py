"""Backup export / restore as a ZIP archive."""

from __future__ import annotations

import io
import json
import zipfile

from tests.conftest import create_wine, make_image


def export_zip(api):
    resp = api.get("/api/backup/export")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("application/zip")
    return zipfile.ZipFile(io.BytesIO(resp.content))


def import_zip(api, blob, mode="merge"):
    return api.post(
        "/api/backup/import",
        files={"file": ("backup.zip", blob, "application/zip")},
        data={"mode": mode},
    )


def seeded(api):
    wine = create_wine(api, name="Backup Bottle")
    api.put(f"/api/wines/{wine['id']}/photo", files={"file": ("l.jpg", make_image(), "image/jpeg")})
    api.put(f"/api/wines/{wine['id']}/rating", json={"stars": 5})
    api.post(f"/api/wines/{wine['id']}/comments", json={"body": "Excellent, would buy again."})
    fav = api.post("/api/favorites", json={"name": "Keepers"}).json()
    api.put(f"/api/favorites/{fav['id']}/wines/{wine['id']}")
    return wine, fav


def test_backup_requires_auth(client):
    assert client.get("/api/backup/export").status_code == 401


def test_export_contains_data_and_photos(api, admin):
    seeded(api)
    archive = export_zip(api)
    names = archive.namelist()
    assert "data.json" in names
    assert any(n.startswith("photos/") for n in names)

    data = json.loads(archive.read("data.json"))
    assert data["format"] == "wine-db-backup"
    assert data["version"] >= 1
    assert len(data["wines"]) == 1
    assert len(data["comments"]) == 1
    assert len(data["ratings"]) == 1
    assert len(data["favorite_lists"]) == 1
    assert data["wines"][0]["name"] == "Backup Bottle"


def test_export_never_leaks_password_hashes(api, admin):
    seeded(api)
    raw = export_zip(api).read("data.json").decode()
    assert "argon2" not in raw
    assert "password_hash" not in raw


def test_replace_restores_exactly_the_backup(api, admin):
    wine, _ = seeded(api)
    blob = api.get("/api/backup/export").content

    # Diverge: delete everything and add an unrelated wine.
    api.delete(f"/api/wines/{wine['id']}")
    create_wine(api, name="Should Disappear")

    resp = import_zip(api, blob, mode="replace")
    assert resp.status_code == 200, resp.text
    assert resp.json()["imported"]["wines"] == 1

    items = api.get("/api/wines").json()["items"]
    assert [i["name"] for i in items] == ["Backup Bottle"]

    restored = api.get(f"/api/wines/{items[0]['id']}").json()
    assert restored["comment_count"] == 1
    assert restored["my_rating"] == 5
    assert restored["photo_url"]
    assert api.get(restored["photo_url"]).status_code == 200
    assert [f["name"] for f in api.get("/api/favorites").json()] == ["Keepers"]


def test_merge_keeps_existing_rows(api, admin):
    seeded(api)
    blob = api.get("/api/backup/export").content
    create_wine(api, name="Kept Wine")

    assert import_zip(api, blob, mode="merge").status_code == 200
    names = {i["name"] for i in api.get("/api/wines").json()["items"]}
    assert "Kept Wine" in names
    assert "Backup Bottle" in names


def test_merge_is_idempotent_for_the_same_backup(api, admin):
    seeded(api)
    blob = api.get("/api/backup/export").content
    import_zip(api, blob, mode="merge")
    import_zip(api, blob, mode="merge")
    assert api.get("/api/wines").json()["total"] == 1
    assert api.get(f"/api/wines/{api.get('/api/wines').json()['items'][0]['id']}").json()["comment_count"] == 1


def test_roundtrip_preserves_every_field(api, admin):
    wine = create_wine(api)
    blob = api.get("/api/backup/export").content
    api.delete(f"/api/wines/{wine['id']}")
    import_zip(api, blob, mode="replace")

    restored = api.get("/api/wines").json()["items"][0]
    full = api.get(f"/api/wines/{restored['id']}").json()
    for key in ("name", "maker", "wine_type", "country", "region", "vintage", "grape",
                "alcohol_pct", "sugar_g_l", "aromas", "acidity", "sweetness", "body",
                "mouthfeel", "wood"):
        assert full[key] == wine[key], key


def test_comment_authorship_survives_restore(api, admin):
    wine = create_wine(api)
    from tests.conftest import register, login
    register(api, username="second")
    assert login(api, "second").status_code == 200
    api.post(f"/api/wines/{wine['id']}/comments", json={"body": "From the second user."})
    blob = api.get("/api/backup/export").content
    import_zip(api, blob, mode="replace")

    detail = api.get(f"/api/wines/{api.get('/api/wines').json()['items'][0]['id']}").json()
    assert detail["comments"][0]["username"] == "second"


def test_invalid_mode_rejected(api, admin):
    blob = api.get("/api/backup/export").content
    assert import_zip(api, blob, mode="obliterate").status_code == 422


def test_non_zip_upload_rejected(api, admin):
    assert import_zip(api, b"just some text").status_code == 400


def test_zip_without_data_json_rejected(api, admin):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("readme.txt", "nope")
    assert import_zip(api, buf.getvalue()).status_code == 400


def test_zip_with_bad_json_rejected(api, admin):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("data.json", "{not json")
    assert import_zip(api, buf.getvalue()).status_code == 400


def test_zip_with_wrong_format_marker_rejected(api, admin):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("data.json", json.dumps({"format": "something-else", "wines": []}))
    assert import_zip(api, buf.getvalue()).status_code == 400


def test_zip_slip_photo_paths_are_ignored(api, admin, settings):
    """A malicious archive must not write outside the uploads directory."""
    buf = io.BytesIO()
    payload = {
        "format": "wine-db-backup",
        "version": 1,
        "users": [],
        "wines": [
            {
                "id": "w1",
                "name": "Evil",
                "wine_type": "red",
                "photo_filename": "../../../../tmp/pwned.jpg",
            }
        ],
        "ratings": [],
        "comments": [],
        "favorite_lists": [],
        "favorite_entries": [],
    }
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("data.json", json.dumps(payload))
        zf.writestr("photos/../../../../tmp/pwned.jpg", make_image())

    resp = import_zip(api, buf.getvalue(), mode="replace")
    assert resp.status_code in (200, 400)
    import pathlib

    assert not pathlib.Path("/tmp/pwned.jpg").exists()
    if resp.status_code == 200:
        item = api.get("/api/wines").json()["items"][0]
        assert item["photo_url"] is None


def test_oversized_import_rejected(api, admin, settings):
    blob = b"PK\x03\x04" + b"0" * (settings.max_backup_bytes + 1024)
    assert import_zip(api, blob).status_code == 413


def test_zip_bomb_guard(api, admin, settings):
    """Highly compressible content beyond the declared limit is refused."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("data.json", json.dumps({"format": "wine-db-backup", "version": 1, "wines": []}))
        zf.writestr("photos/huge.jpg", b"0" * (settings.max_backup_bytes * 4))
    resp = import_zip(api, buf.getvalue(), mode="replace")
    assert resp.status_code in (200, 400, 413)
    # Whatever the outcome, nothing enormous may be written to disk.
    import pathlib

    total = sum(p.stat().st_size for p in pathlib.Path(settings.uploads_dir).glob("*"))
    assert total <= settings.max_backup_bytes
