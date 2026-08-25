"""Shared favorites lists. Every user can see and edit every list."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import FavoriteItem, FavoriteList, User, Wine
from app.schemas import (
    FavoriteListCreate,
    FavoriteListOut,
    FavoriteListUpdate,
    SearchResponse,
)
from app.routers.wines import _serialize_many
from app.security import current_user, require_csrf

router = APIRouter(prefix="/api/favorites", tags=["favorites"])


def _counts(db: Session) -> dict[str, int]:
    rows = db.execute(
        select(FavoriteItem.list_id, func.count(FavoriteItem.id)).group_by(FavoriteItem.list_id)
    ).all()
    return {lid: int(n) for lid, n in rows}


def _out(fav: FavoriteList, counts: dict[str, int]) -> FavoriteListOut:
    return FavoriteListOut(
        id=fav.id,
        name=fav.name,
        description=fav.description,
        wine_count=counts.get(fav.id, 0),
        created_at=fav.created_at,
    )


def _get_list(db: Session, list_id: str) -> FavoriteList:
    fav = db.scalar(select(FavoriteList).where(FavoriteList.id == list_id))
    if fav is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Favorites list not found")
    return fav


@router.get("", response_model=list[FavoriteListOut])
def list_lists(
    _user: User = Depends(current_user), db: Session = Depends(get_db)
) -> list[FavoriteListOut]:
    counts = _counts(db)
    rows = db.scalars(select(FavoriteList).order_by(FavoriteList.name))
    return [_out(f, counts) for f in rows]


@router.post("", response_model=FavoriteListOut, status_code=status.HTTP_201_CREATED)
def create_list(
    payload: FavoriteListCreate,
    user: User = Depends(current_user),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> FavoriteListOut:
    name = payload.name.strip()
    if db.scalar(select(FavoriteList).where(func.lower(FavoriteList.name) == name.lower())):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="A list with that name already exists"
        )
    fav = FavoriteList(name=name, description=payload.description, created_by=user.id)
    db.add(fav)
    db.flush()
    return _out(fav, {})


@router.patch("/{list_id}", response_model=FavoriteListOut)
def update_list(
    list_id: str,
    payload: FavoriteListUpdate,
    _user: User = Depends(current_user),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> FavoriteListOut:
    fav = _get_list(db, list_id)
    changes = payload.model_dump(exclude_unset=True)
    if "name" in changes and changes["name"]:
        new_name = changes["name"].strip()
        clash = db.scalar(
            select(FavoriteList).where(
                func.lower(FavoriteList.name) == new_name.lower(), FavoriteList.id != fav.id
            )
        )
        if clash:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="A list with that name already exists"
            )
        fav.name = new_name
    if "description" in changes:
        fav.description = changes["description"]
    db.flush()
    return _out(fav, _counts(db))


@router.delete("/{list_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_list(
    list_id: str,
    _user: User = Depends(current_user),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> Response:
    fav = _get_list(db, list_id)
    db.delete(fav)
    db.flush()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{list_id}/wines", response_model=SearchResponse)
def list_wines(
    list_id: str,
    limit: int = 50,
    offset: int = 0,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> SearchResponse:
    _get_list(db, list_id)
    limit = max(1, min(limit, 100))
    offset = max(0, min(offset, 100_000))
    stmt = (
        select(Wine)
        .join(FavoriteItem, FavoriteItem.wine_id == Wine.id)
        .where(FavoriteItem.list_id == list_id)
        .order_by(FavoriteItem.created_at.desc(), Wine.id)
    )
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    wines = list(db.scalars(stmt.limit(limit).offset(offset)))
    return SearchResponse(
        total=total, limit=limit, offset=offset, items=_serialize_many(db, user, wines)
    )


@router.put("/{list_id}/wines/{wine_id}", status_code=status.HTTP_204_NO_CONTENT)
def add_wine(
    list_id: str,
    wine_id: str,
    user: User = Depends(current_user),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> Response:
    _get_list(db, list_id)
    if not db.scalar(select(Wine.id).where(Wine.id == wine_id)):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Wine not found")
    exists = db.scalar(
        select(FavoriteItem).where(
            FavoriteItem.list_id == list_id, FavoriteItem.wine_id == wine_id
        )
    )
    if not exists:
        db.add(FavoriteItem(list_id=list_id, wine_id=wine_id, added_by=user.id))
        db.flush()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/{list_id}/wines/{wine_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_wine(
    list_id: str,
    wine_id: str,
    _user: User = Depends(current_user),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> Response:
    item = db.scalar(
        select(FavoriteItem).where(
            FavoriteItem.list_id == list_id, FavoriteItem.wine_id == wine_id
        )
    )
    if item:
        db.delete(item)
        db.flush()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
