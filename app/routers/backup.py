"""Backup export / restore as a ZIP file."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile, status
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_db
from app.models import User
from app.security import current_user, require_admin, require_csrf
from app.services.backup import export_backup, import_backup

router = APIRouter(prefix="/api/backup", tags=["backup"])

@router.get("/export")
def export_zip(
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Response:
    data = export_backup(db, settings.uploads_dir)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return Response(
        content=data,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="wine-db-backup-{stamp}.zip"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/import")
async def import_zip(
    file: UploadFile = File(...),
    mode: str = Form(default="replace"),
    _admin: User = Depends(require_admin),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    if mode not in ("replace", "merge"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="mode must be replace or merge"
        )

    chunks: list[bytes] = []
    total = 0
    while chunk := await file.read(256 * 1024):
        total += len(chunk)
        if total > settings.max_backup_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="Backup file too large"
            )
        chunks.append(chunk)
    raw = b"".join(chunks)
    if not raw:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Empty upload")

    stats = import_backup(db, settings.uploads_dir, raw, replace=(mode == "replace"))
    return {"status": "ok", "mode": mode, "imported": stats}
