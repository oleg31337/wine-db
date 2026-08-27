"""Full database export/import as a ZIP archive.

Archive layout:
    manifest.json   - {"format": "wine-db-backup", "version": 1, "created_at": ...}
    data.json       - users (without password hashes unless include_users), wines,
                      ratings, comments, favorite lists/items
    photos/<file>   - normalized JPEG photos referenced by wines

Restore is defensive: the archive is treated as untrusted input. Entry names,
counts, and sizes are all bounded, and photo bytes are re-encoded before landing
on disk.
"""

from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException, status
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import (
    Comment,
    FavoriteItem,
    FavoriteList,
    Rating,
    User,
    Wine,
    WineType,
)
from app.services.images import normalize_image, store_photo

BACKUP_FORMAT = "wine-db-backup"
BACKUP_VERSION = 1

# Marks a restored account that has no usable credentials yet.
UNUSABLE_PASSWORD = "!restored-no-login"

MAX_ARCHIVE_ENTRIES = 20_000
MAX_UNCOMPRESSED_BYTES = 2 * 1024 * 1024 * 1024  # zip-bomb guard
MAX_JSON_BYTES = 64 * 1024 * 1024


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _parse_dt(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def export_backup(db: Session, uploads_dir: str) -> bytes:
    wines = list(db.scalars(select(Wine)))
    payload = {
        "format": BACKUP_FORMAT,
        "version": BACKUP_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        # Credentials are deliberately NOT exported: a backup file is a data
        # archive, not a copy of everyone's password. Accounts are matched back
        # by username on restore, and unknown authors are recreated as
        # login-disabled placeholders so their notes keep their attribution.
        "users": [
            {
                "id": u.id,
                "username": u.username,
                "display_name": u.display_name,
                "created_at": _iso(u.created_at),
                "is_active": u.is_active,
            }
            for u in db.scalars(select(User))
        ],
        "wines": [
            {
                "id": w.id,
                "name": w.name,
                "maker": w.maker,
                "wine_type": w.wine_type.value,
                "country": w.country,
                "region": w.region,
                "vintage": w.vintage,
                "grape": w.grape,
                "sugar_g_l": w.sugar_g_l,
                "alcohol_pct": w.alcohol_pct,
                "aromas": w.aromas,
                "acidity": w.acidity,
                "sweetness": w.sweetness,
                "body": w.body,
                "mouthfeel": w.mouthfeel,
                "wood": w.wood,
                "photo": w.photo_path,
                "created_by": w.created_by,
                "created_at": _iso(w.created_at),
                "updated_at": _iso(w.updated_at),
            }
            for w in wines
        ],
        "ratings": [
            {
                "id": r.id,
                "wine_id": r.wine_id,
                "user_id": r.user_id,
                "stars": r.stars,
                "created_at": _iso(r.created_at),
            }
            for r in db.scalars(select(Rating))
        ],
        "comments": [
            {
                "id": c.id,
                "wine_id": c.wine_id,
                "user_id": c.user_id,
                "body": c.body,
                "created_at": _iso(c.created_at),
                "updated_at": _iso(c.updated_at),
            }
            for c in db.scalars(select(Comment))
        ],
        "favorite_lists": [
            {
                "id": fl.id,
                "name": fl.name,
                "description": fl.description,
                "created_by": fl.created_by,
                "created_at": _iso(fl.created_at),
            }
            for fl in db.scalars(select(FavoriteList))
        ],
        "favorite_items": [
            {
                "id": fi.id,
                "list_id": fi.list_id,
                "wine_id": fi.wine_id,
                "added_by": fi.added_by,
                "created_at": _iso(fi.created_at),
            }
            for fi in db.scalars(select(FavoriteItem))
        ],
    }

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "manifest.json",
            json.dumps(
                {
                    "format": BACKUP_FORMAT,
                    "version": BACKUP_VERSION,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "counts": {k: len(v) for k, v in payload.items() if isinstance(v, list)},
                },
                indent=2,
            ),
        )
        zf.writestr("data.json", json.dumps(payload, indent=2))
        base = Path(uploads_dir)
        for wine in wines:
            if not wine.photo_path:
                continue
            src = base / wine.photo_path
            # Guard against a stored name escaping the uploads dir.
            if src.resolve().parent != base.resolve() or not src.is_file():
                continue
            zf.writestr(f"photos/{wine.photo_path}", src.read_bytes())
    return buf.getvalue()


def _safe_photo_name(name: str) -> str | None:
    """Only accept flat `photos/<file>` names - no traversal, no nesting."""
    if not name.startswith("photos/"):
        return None
    leaf = name[len("photos/") :]
    if not leaf or "/" in leaf or "\\" in leaf or leaf.startswith("."):
        return None
    if ".." in leaf or len(leaf) > 128:
        return None
    return leaf


def _open_archive(raw: bytes) -> zipfile.ZipFile:
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Not a valid ZIP archive"
        ) from exc
    infos = zf.infolist()
    if len(infos) > MAX_ARCHIVE_ENTRIES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Archive has too many entries"
        )
    if sum(i.file_size for i in infos) > MAX_UNCOMPRESSED_BYTES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Archive is too large when expanded"
        )
    return zf


def import_backup(db: Session, uploads_dir: str, raw: bytes, replace: bool = True) -> dict:
    """Restore a backup. With replace=True the current data is wiped first."""
    zf = _open_archive(raw)
    names = set(zf.namelist())
    if "data.json" not in names:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Archive is missing data.json"
        )

    if "manifest.json" in names:
        try:
            manifest = json.loads(zf.read("manifest.json"))
            if manifest.get("format") != BACKUP_FORMAT:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Archive is not a wine-db backup",
                )
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="Corrupt manifest.json"
            ) from exc

    info = zf.getinfo("data.json")
    if info.file_size > MAX_JSON_BYTES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="data.json too large")
    try:
        data = json.loads(zf.read("data.json"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Corrupt data.json"
        ) from exc
    if not isinstance(data, dict):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Corrupt data.json")
    declared = data.get("format")
    if declared is not None and declared != BACKUP_FORMAT:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Archive is not a wine-db backup"
        )
    if declared is None and "manifest.json" not in names:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Archive is not a wine-db backup"
        )
    version = data.get("version", BACKUP_VERSION)
    if isinstance(version, int) and version > BACKUP_VERSION:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Backup was made by a newer version (format {version})",
        )

    if replace:
        for model in (FavoriteItem, FavoriteList, Comment, Rating, Wine):
            db.execute(delete(model))
        db.flush()

    stats = {"users": 0, "wines": 0, "ratings": 0, "comments": 0, "lists": 0, "list_items": 0}

    # --- users: merged by username, never overwriting an existing account ---
    existing_users = {u.username: u for u in db.scalars(select(User))}
    id_map: dict[str, str] = {}
    for row in data.get("users") or []:
        if not isinstance(row, dict):
            continue
        username = str(row.get("username") or "").strip()
        pw_hash = row.get("password_hash")
        if not username:
            continue
        found = existing_users.get(username)
        if found:
            id_map[str(row.get("id"))] = found.id
            continue
        # A backup carries no credentials. Recreate the account so its ratings
        # and comments keep their author, but with an unusable password hash so
        # nobody can log in as them until they are given a real password.
        user = User(
            username=username[:32],
            password_hash=(
                pw_hash[:255]
                if isinstance(pw_hash, str) and pw_hash.startswith("$argon2")
                else UNUSABLE_PASSWORD
            ),
            display_name=(row.get("display_name") or None),
            created_at=_parse_dt(row.get("created_at")),
            is_active=bool(row.get("is_active", True)),
        )
        db.add(user)
        db.flush()
        existing_users[username] = user
        id_map[str(row.get("id"))] = user.id
        stats["users"] += 1

    def _user(ref) -> str | None:
        return id_map.get(str(ref))

    def _gauge(value) -> int | None:
        return value if isinstance(value, int) and 0 <= value <= 5 else None

    # --- wines ---
    def _wine_key(name: str, row: dict) -> tuple:
        """Identity used to avoid duplicating a wine when merging."""
        return (
            "fields",
            name.casefold(),
            str(row.get("maker") or "").strip().casefold(),
            row.get("vintage"),
        )

    existing_keys: dict[tuple, str] = {}
    if not replace:
        for w in db.scalars(select(Wine)):
            existing_keys[_wine_key(w.name, {"maker": w.maker, "vintage": w.vintage})] = w.id

    wine_map: dict[str, str] = {}
    for row in data.get("wines") or []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        key = _wine_key(name, row)
        if key in existing_keys:
            # Already present (merge of a backup we already hold) - reuse it so
            # ratings and comments attach to the existing card instead of a copy.
            wine_map[str(row.get("id"))] = existing_keys[key]
            continue
        try:
            wtype = WineType(str(row.get("wine_type") or "other"))
        except ValueError:
            wtype = WineType.other

        photo_name = None
        stored = row.get("photo")
        if isinstance(stored, str) and stored:
            entry = f"photos/{stored}"
            if entry in names and _safe_photo_name(entry):
                try:
                    _ai_bytes, photo_data = normalize_image(zf.read(entry), 32 * 1024 * 1024)
                    photo_name = store_photo(uploads_dir, photo_data)
                except (HTTPException, OSError):
                    photo_name = None

        wine = Wine(
            name=name[:200],
            maker=(row.get("maker") or None),
            wine_type=wtype,
            country=(row.get("country") or None),
            region=(row.get("region") or None),
            vintage=row.get("vintage") if isinstance(row.get("vintage"), int) else None,
            grape=(row.get("grape") or None),
            sugar_g_l=row.get("sugar_g_l") if isinstance(row.get("sugar_g_l"), (int, float)) else None,
            alcohol_pct=row.get("alcohol_pct")
            if isinstance(row.get("alcohol_pct"), (int, float))
            else None,
            aromas=(row.get("aromas") or None),
            acidity=_gauge(row.get("acidity")),
            sweetness=_gauge(row.get("sweetness")),
            body=_gauge(row.get("body")),
            mouthfeel=_gauge(row.get("mouthfeel")),
            wood=_gauge(row.get("wood")),
            photo_path=photo_name,
            created_by=_user(row.get("created_by")),
            created_at=_parse_dt(row.get("created_at")),
            updated_at=_parse_dt(row.get("updated_at")),
        )
        db.add(wine)
        db.flush()
        wine_map[str(row.get("id"))] = wine.id
        existing_keys[key] = wine.id
        stats["wines"] += 1

    # --- ratings (user data, preserved verbatim) ---
    seen_ratings: set[tuple[str, str]] = set()
    if not replace:
        seen_ratings.update(
            (r.wine_id, r.user_id) for r in db.scalars(select(Rating))
        )
    for row in data.get("ratings") or []:
        if not isinstance(row, dict):
            continue
        wine_id = wine_map.get(str(row.get("wine_id")))
        user_id = _user(row.get("user_id"))
        stars = row.get("stars")
        if not (wine_id and user_id and isinstance(stars, int) and 1 <= stars <= 5):
            continue
        if (wine_id, user_id) in seen_ratings:
            continue
        seen_ratings.add((wine_id, user_id))
        db.add(
            Rating(
                wine_id=wine_id,
                user_id=user_id,
                stars=stars,
                created_at=_parse_dt(row.get("created_at")),
            )
        )
        stats["ratings"] += 1

    # --- comments ---
    seen_comments: set[tuple[str, str, str]] = set()
    if not replace:
        seen_comments.update(
            (c.wine_id, c.user_id, c.body) for c in db.scalars(select(Comment))
        )
    for row in data.get("comments") or []:
        if not isinstance(row, dict):
            continue
        wine_id = wine_map.get(str(row.get("wine_id")))
        user_id = _user(row.get("user_id"))
        body = row.get("body")
        if not (wine_id and user_id and isinstance(body, str) and body.strip()):
            continue
        fingerprint = (wine_id, user_id, body[:4000])
        if fingerprint in seen_comments:
            continue
        seen_comments.add(fingerprint)
        db.add(
            Comment(
                wine_id=wine_id,
                user_id=user_id,
                body=body[:4000],
                created_at=_parse_dt(row.get("created_at")),
                updated_at=_parse_dt(row.get("updated_at")),
            )
        )
        stats["comments"] += 1

    # --- favorite lists ---
    list_map: dict[str, str] = {}
    taken = {fl.name for fl in db.scalars(select(FavoriteList))}
    for row in data.get("favorite_lists") or []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()[:80]
        if not name or name in taken:
            if name in taken:
                existing = db.scalar(select(FavoriteList).where(FavoriteList.name == name))
                if existing:
                    list_map[str(row.get("id"))] = existing.id
            continue
        fav = FavoriteList(
            name=name,
            description=(row.get("description") or None),
            created_by=_user(row.get("created_by")),
            created_at=_parse_dt(row.get("created_at")),
        )
        db.add(fav)
        db.flush()
        taken.add(name)
        list_map[str(row.get("id"))] = fav.id
        stats["lists"] += 1

    seen_items: set[tuple[str, str]] = set()
    if not replace:
        seen_items.update(
            (fi.list_id, fi.wine_id) for fi in db.scalars(select(FavoriteItem))
        )
    for row in data.get("favorite_items") or []:
        if not isinstance(row, dict):
            continue
        list_id = list_map.get(str(row.get("list_id")))
        wine_id = wine_map.get(str(row.get("wine_id")))
        if not (list_id and wine_id) or (list_id, wine_id) in seen_items:
            continue
        seen_items.add((list_id, wine_id))
        db.add(
            FavoriteItem(
                list_id=list_id,
                wine_id=wine_id,
                added_by=_user(row.get("added_by")),
                created_at=_parse_dt(row.get("created_at")),
            )
        )
        stats["list_items"] += 1

    db.flush()
    return stats
