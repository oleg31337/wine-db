"""Wine CRUD, search, photos, ratings and comments."""

from __future__ import annotations

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse
from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.config import Settings, get_settings
from app.db import get_db
from app.models import Comment, FavoriteItem, Rating, User, Wine, WineType
from app.schemas import (
    CommentOut,
    CommentRequest,
    RatingOut,
    RatingRequest,
    SearchResponse,
    WineCreate,
    WineDetail,
    WineOut,
    WineUpdate,
)
from app.security import current_user, require_csrf
from app.services import images

router = APIRouter(prefix="/api/wines", tags=["wines"])

SORT_FIELDS = {
    "created": Wine.created_at,
    "updated": Wine.updated_at,
    "name": Wine.name,
    "vintage": Wine.vintage,
}


def _rating_stats(db: Session, wine_ids: list[str]) -> dict[str, tuple[float, int]]:
    if not wine_ids:
        return {}
    rows = db.execute(
        select(Rating.wine_id, func.avg(Rating.stars), func.count(Rating.id))
        .where(Rating.wine_id.in_(wine_ids))
        .group_by(Rating.wine_id)
    ).all()
    return {wid: (round(float(avg), 2), int(cnt)) for wid, avg, cnt in rows}


def _comment_counts(db: Session, wine_ids: list[str]) -> dict[str, int]:
    if not wine_ids:
        return {}
    rows = db.execute(
        select(Comment.wine_id, func.count(Comment.id))
        .where(Comment.wine_id.in_(wine_ids))
        .group_by(Comment.wine_id)
    ).all()
    return {wid: int(cnt) for wid, cnt in rows}


def _my_ratings(db: Session, user_id: str, wine_ids: list[str]) -> dict[str, int]:
    if not wine_ids:
        return {}
    rows = db.execute(
        select(Rating.wine_id, Rating.stars).where(
            Rating.user_id == user_id, Rating.wine_id.in_(wine_ids)
        )
    ).all()
    return {wid: int(stars) for wid, stars in rows}


def to_wine_out(
    wine: Wine,
    stats: dict[str, tuple[float, int]],
    mine: dict[str, int],
    comments: dict[str, int],
) -> WineOut:
    avg, count = stats.get(wine.id, (None, 0))
    return WineOut(
        id=wine.id,
        name=wine.name,
        maker=wine.maker,
        wine_type=wine.wine_type,
        country=wine.country,
        region=wine.region,
        vintage=wine.vintage,
        grape=wine.grape,
        sugar_g_l=wine.sugar_g_l,
        alcohol_pct=wine.alcohol_pct,
        aromas=wine.aromas,
        barcode=wine.barcode,
        acidity=wine.acidity,
        sweetness=wine.sweetness,
        body=wine.body,
        mouthfeel=wine.mouthfeel,
        wood=wine.wood,
        photo_url=f"/api/wines/{wine.id}/photo" if wine.photo_path else None,
        created_at=wine.created_at,
        updated_at=wine.updated_at,
        average_rating=avg,
        rating_count=count,
        my_rating=mine.get(wine.id),
        comment_count=comments.get(wine.id, 0),
    )


def _serialize_many(db: Session, user: User, wines: list[Wine]) -> list[WineOut]:
    ids = [w.id for w in wines]
    return [
        to_wine_out(w, _rating_stats(db, ids), _my_ratings(db, user.id, ids), _comment_counts(db, ids))
        for w in wines
    ]


def _like_escape(value: str) -> str:
    """Escape LIKE wildcards so a user's % or _ is matched literally."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _get_wine(db: Session, wine_id: str) -> Wine:
    wine = db.scalar(select(Wine).where(Wine.id == wine_id))
    if wine is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Wine not found")
    return wine


@router.get("", response_model=SearchResponse)
def search_wines(
    q: str | None = Query(default=None, max_length=200, description="Name / maker / grape"),
    wine_type: list[WineType] | None = Query(default=None),
    country: str | None = Query(default=None, max_length=100),
    region: str | None = Query(default=None, max_length=150),
    min_rating: float | None = Query(default=None, ge=0, le=5),
    max_rating: float | None = Query(default=None, ge=0, le=5),
    rating_scope: str = Query(default="average", pattern="^(average|mine)$"),
    favorite_list_id: str | None = Query(default=None, max_length=32),
    unrated_by_me: bool = Query(default=False),
    sort: str = Query(default="created", pattern="^(created|updated|name|vintage)$"),
    order: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=24, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=100_000),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> SearchResponse:
    stmt: Select = select(Wine)

    if q:
        needle = f"%{_like_escape(q.strip().lower())}%"
        stmt = stmt.where(
            or_(
                func.lower(Wine.name).like(needle, escape="\\"),
                func.lower(func.coalesce(Wine.maker, "")).like(needle, escape="\\"),
                func.lower(func.coalesce(Wine.grape, "")).like(needle, escape="\\"),
                func.lower(func.coalesce(Wine.country, "")).like(needle, escape="\\"),
                func.lower(func.coalesce(Wine.region, "")).like(needle, escape="\\"),
            )
        )
    if wine_type:
        stmt = stmt.where(Wine.wine_type.in_(wine_type))
    if country:
        stmt = stmt.where(func.lower(func.coalesce(Wine.country, "")) == country.strip().lower())
    if region:
        stmt = stmt.where(
            func.lower(func.coalesce(Wine.region, "")).like(
                f"%{_like_escape(region.strip().lower())}%", escape="\\"
            )
        )
    if favorite_list_id:
        stmt = stmt.where(
            Wine.id.in_(select(FavoriteItem.wine_id).where(FavoriteItem.list_id == favorite_list_id))
        )

    if min_rating is not None or max_rating is not None:
        if rating_scope == "mine":
            sub = select(Rating.wine_id).where(Rating.user_id == user.id)
            if min_rating is not None:
                sub = sub.where(Rating.stars >= min_rating)
            if max_rating is not None:
                sub = sub.where(Rating.stars <= max_rating)
        else:
            sub = select(Rating.wine_id).group_by(Rating.wine_id)
            if min_rating is not None:
                sub = sub.having(func.avg(Rating.stars) >= min_rating)
            if max_rating is not None:
                sub = sub.having(func.avg(Rating.stars) <= max_rating)
        stmt = stmt.where(Wine.id.in_(sub))

    if unrated_by_me:
        stmt = stmt.where(
            Wine.id.notin_(select(Rating.wine_id).where(Rating.user_id == user.id))
        )

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0

    column = SORT_FIELDS[sort]
    stmt = stmt.order_by(column.asc() if order == "asc" else column.desc(), Wine.id.asc())
    wines = list(db.scalars(stmt.limit(limit).offset(offset)))

    return SearchResponse(
        total=total, limit=limit, offset=offset, items=_serialize_many(db, user, wines)
    )


@router.get("/facets")
def facets(user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """Countries/regions/types actually present - drives the search filters."""
    countries = db.execute(
        select(Wine.country, func.count(Wine.id))
        .where(Wine.country.is_not(None))
        .group_by(Wine.country)
        .order_by(Wine.country)
    ).all()
    regions = db.execute(
        select(Wine.country, Wine.region, func.count(Wine.id))
        .where(Wine.region.is_not(None))
        .group_by(Wine.country, Wine.region)
        .order_by(Wine.region)
    ).all()
    types = db.execute(select(Wine.wine_type, func.count(Wine.id)).group_by(Wine.wine_type)).all()
    return {
        "countries": [{"name": c, "count": int(n)} for c, n in countries],
        "regions": [{"country": c, "name": r, "count": int(n)} for c, r, n in regions],
        "types": [{"name": t.value, "count": int(n)} for t, n in types],
        "total": db.scalar(select(func.count()).select_from(Wine)) or 0,
    }


@router.post("", response_model=WineDetail, status_code=status.HTTP_201_CREATED)
def create_wine(
    payload: WineCreate,
    user: User = Depends(current_user),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> WineDetail:
    wine = Wine(**payload.model_dump(), created_by=user.id)
    db.add(wine)
    db.flush()
    return _detail(db, user, wine)


@router.get("/{wine_id}", response_model=WineDetail)
def get_wine(
    wine_id: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> WineDetail:
    wine = db.scalar(
        select(Wine)
        .options(selectinload(Wine.ratings).selectinload(Rating.user))
        .options(selectinload(Wine.comments).selectinload(Comment.user))
        .where(Wine.id == wine_id)
    )
    if wine is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Wine not found")
    return _detail(db, user, wine)


def _detail(db: Session, user: User, wine: Wine) -> WineDetail:
    ids = [wine.id]
    base = to_wine_out(
        wine, _rating_stats(db, ids), _my_ratings(db, user.id, ids), _comment_counts(db, ids)
    )
    ratings = [
        RatingOut(user_id=r.user_id, username=r.user.username if r.user else "?", stars=r.stars)
        for r in sorted(wine.ratings, key=lambda r: r.created_at or r.id)
    ]
    comments = [
        CommentOut(
            id=c.id,
            user_id=c.user_id,
            username=c.user.username if c.user else "?",
            body=c.body,
            created_at=c.created_at,
            updated_at=c.updated_at,
        )
        for c in sorted(wine.comments, key=lambda c: c.created_at or c.id, reverse=True)
    ]
    fav_ids = [
        row[0]
        for row in db.execute(
            select(FavoriteItem.list_id).where(FavoriteItem.wine_id == wine.id)
        ).all()
    ]
    return WineDetail(
        **base.model_dump(), ratings=ratings, comments=comments, favorite_list_ids=fav_ids
    )


@router.patch("/{wine_id}", response_model=WineDetail)
def update_wine(
    wine_id: str,
    payload: WineUpdate,
    user: User = Depends(current_user),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> WineDetail:
    """Every field is editable by any user - all users have equal access."""
    wine = _get_wine(db, wine_id)
    changes = payload.model_dump(exclude_unset=True)
    for field, value in changes.items():
        setattr(wine, field, value)
    db.flush()
    return _detail(db, user, wine)


@router.delete("/{wine_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_wine(
    wine_id: str,
    _user: User = Depends(current_user),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Response:
    wine = _get_wine(db, wine_id)
    photo = wine.photo_path
    db.delete(wine)
    db.flush()
    images.delete_photo(settings.uploads_dir, photo)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put("/{wine_id}/photo", response_model=WineOut)
async def upload_photo(
    wine_id: str,
    file: UploadFile = File(...),
    user: User = Depends(current_user),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> WineOut:
    raw = await images.read_upload(file, settings.max_upload_bytes)
    _ai_bytes, normalized = images.normalize_image(raw, settings.max_upload_bytes)
    wine = _get_wine(db, wine_id)
    old = wine.photo_path
    wine.photo_path = images.store_photo(settings.uploads_dir, normalized)
    db.flush()
    if old and old != wine.photo_path:
        images.delete_photo(settings.uploads_dir, old)
    ids = [wine.id]
    return to_wine_out(
        wine, _rating_stats(db, ids), _my_ratings(db, user.id, ids), _comment_counts(db, ids)
    )


@router.get("/{wine_id}/photo")
def get_photo(
    wine_id: str,
    _user: User = Depends(current_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> FileResponse:
    wine = _get_wine(db, wine_id)
    if not wine.photo_path:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No photo")
    path = images.resolve_photo(settings.uploads_dir, wine.photo_path)
    return FileResponse(
        path,
        media_type="image/jpeg",
        headers={"Cache-Control": "private, max-age=86400", "X-Content-Type-Options": "nosniff"},
    )


@router.delete("/{wine_id}/photo", status_code=status.HTTP_204_NO_CONTENT)
def delete_wine_photo(
    wine_id: str,
    _user: User = Depends(current_user),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Response:
    wine = _get_wine(db, wine_id)
    old, wine.photo_path = wine.photo_path, None
    db.flush()
    images.delete_photo(settings.uploads_dir, old)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------
# Ratings & comments - user-generated only, never from an internet lookup.
# --------------------------------------------------------------------------


@router.put("/{wine_id}/rating", response_model=WineOut)
def set_rating(
    wine_id: str,
    payload: RatingRequest,
    user: User = Depends(current_user),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> WineOut:
    wine = _get_wine(db, wine_id)
    rating = db.scalar(select(Rating).where(Rating.wine_id == wine.id, Rating.user_id == user.id))
    if rating is None:
        db.add(Rating(wine_id=wine.id, user_id=user.id, stars=payload.stars))
    else:
        rating.stars = payload.stars
    db.flush()
    ids = [wine.id]
    return to_wine_out(
        wine, _rating_stats(db, ids), _my_ratings(db, user.id, ids), _comment_counts(db, ids)
    )


@router.delete("/{wine_id}/rating", status_code=status.HTTP_204_NO_CONTENT)
def clear_rating(
    wine_id: str,
    user: User = Depends(current_user),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> Response:
    rating = db.scalar(select(Rating).where(Rating.wine_id == wine_id, Rating.user_id == user.id))
    if rating:
        db.delete(rating)
        db.flush()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{wine_id}/comments", response_model=CommentOut, status_code=status.HTTP_201_CREATED)
def add_comment(
    wine_id: str,
    payload: CommentRequest,
    user: User = Depends(current_user),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> CommentOut:
    wine = _get_wine(db, wine_id)
    comment = Comment(wine_id=wine.id, user_id=user.id, body=payload.body)
    db.add(comment)
    db.flush()
    return CommentOut(
        id=comment.id,
        user_id=user.id,
        username=user.username,
        body=comment.body,
        created_at=comment.created_at,
        updated_at=comment.updated_at,
    )


@router.patch("/{wine_id}/comments/{comment_id}", response_model=CommentOut)
def edit_comment(
    wine_id: str,
    comment_id: str,
    payload: CommentRequest,
    user: User = Depends(current_user),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> CommentOut:
    comment = db.scalar(select(Comment).where(Comment.id == comment_id, Comment.wine_id == wine_id))
    if comment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Comment not found")
    # A comment is personal authored content: only its author may rewrite it.
    if comment.user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="You can only edit your own comment"
        )
    comment.body = payload.body
    db.flush()
    return CommentOut(
        id=comment.id,
        user_id=comment.user_id,
        username=user.username,
        body=comment.body,
        created_at=comment.created_at,
        updated_at=comment.updated_at,
    )


@router.delete("/{wine_id}/comments/{comment_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_comment(
    wine_id: str,
    comment_id: str,
    user: User = Depends(current_user),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> Response:
    comment = db.scalar(select(Comment).where(Comment.id == comment_id, Comment.wine_id == wine_id))
    if comment is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    if comment.user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="You can only delete your own comment"
        )
    db.delete(comment)
    db.flush()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
