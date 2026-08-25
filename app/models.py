"""SQLAlchemy models for wine-db."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return uuid.uuid4().hex


class Base(DeclarativeBase):
    pass


class WineType(str, enum.Enum):
    red = "red"
    white = "white"
    rose = "rose"
    sparkling = "sparkling"
    other = "other"


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    username: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Incremented on password change to invalidate existing sessions.
    token_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)
    # Only admins may create/remove users and manage database import/export.
    # Seeded from ADMIN_USERNAME/ADMIN_PASSWORD_HASH in the environment; never
    # self-promoted.
    is_admin: Mapped[bool] = mapped_column(default=False, nullable=False, index=True)

    ratings: Mapped[list["Rating"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    comments: Mapped[list["Comment"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class Wine(Base):
    """A wine in the shared central database. All users have equal access."""

    __tablename__ = "wines"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    maker: Mapped[str | None] = mapped_column(String(200), index=True)
    wine_type: Mapped[WineType] = mapped_column(
        Enum(WineType, name="wine_type"), default=WineType.other, nullable=False, index=True
    )
    country: Mapped[str | None] = mapped_column(String(100), index=True)
    region: Mapped[str | None] = mapped_column(String(150), index=True)
    vintage: Mapped[int | None] = mapped_column(Integer, index=True)
    grape: Mapped[str | None] = mapped_column(String(300))
    sugar_g_l: Mapped[float | None] = mapped_column(Float)
    alcohol_pct: Mapped[float | None] = mapped_column(Float)
    aromas: Mapped[str | None] = mapped_column(Text)
    barcode: Mapped[str | None] = mapped_column(String(64), index=True)

    # Gauge bars, 0-3 (0 = "no such taste"; nullable = not assessed)
    acidity: Mapped[int | None] = mapped_column(Integer)
    sweetness: Mapped[int | None] = mapped_column(Integer)
    body: Mapped[int | None] = mapped_column(Integer)
    mouthfeel: Mapped[int | None] = mapped_column(Integer)
    wood: Mapped[int | None] = mapped_column(Integer)

    photo_path: Mapped[str | None] = mapped_column(String(255))
    notes_source: Mapped[str | None] = mapped_column(String(255))
    # Raw text the vision model read from the BACK label when the wine was
    # added. Optional provenance - the structured facts it contained (grape,
    # region, country, alcohol, sugar) are merged into the fields above.
    back_label_text: Mapped[str | None] = mapped_column(Text)

    created_by: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    ratings: Mapped[list["Rating"]] = relationship(back_populates="wine", cascade="all, delete-orphan")
    comments: Mapped[list["Comment"]] = relationship(back_populates="wine", cascade="all, delete-orphan")
    favorite_items: Mapped[list["FavoriteItem"]] = relationship(
        back_populates="wine", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint("acidity IS NULL OR (acidity BETWEEN 0 AND 3)", name="ck_wine_acidity"),
        CheckConstraint("sweetness IS NULL OR (sweetness BETWEEN 0 AND 3)", name="ck_wine_sweetness"),
        CheckConstraint("body IS NULL OR (body BETWEEN 0 AND 3)", name="ck_wine_body"),
        CheckConstraint("mouthfeel IS NULL OR (mouthfeel BETWEEN 0 AND 3)", name="ck_wine_mouthfeel"),
        CheckConstraint("wood IS NULL OR (wood BETWEEN 0 AND 3)", name="ck_wine_wood"),
        CheckConstraint("sugar_g_l IS NULL OR (sugar_g_l BETWEEN 0 AND 500)", name="ck_wine_sugar"),
        CheckConstraint("alcohol_pct IS NULL OR (alcohol_pct BETWEEN 0 AND 100)", name="ck_wine_abv"),
        Index("ix_wines_name_lower", func.lower(name)),
    )


class Rating(Base):
    """Per-user star rating, 1-5. Never imported from the internet."""

    __tablename__ = "ratings"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    wine_id: Mapped[str] = mapped_column(ForeignKey("wines.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    stars: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    wine: Mapped[Wine] = relationship(back_populates="ratings")
    user: Mapped[User] = relationship(back_populates="ratings")

    __table_args__ = (
        UniqueConstraint("wine_id", "user_id", name="uq_rating_wine_user"),
        CheckConstraint("stars BETWEEN 1 AND 5", name="ck_rating_stars"),
    )


class Comment(Base):
    """Tasting comment. At least 1000 characters allowed (limit 4000)."""

    __tablename__ = "comments"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    wine_id: Mapped[str] = mapped_column(ForeignKey("wines.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    wine: Mapped[Wine] = relationship(back_populates="comments")
    user: Mapped[User] = relationship(back_populates="comments")


class FavoriteList(Base):
    """Shared favorites list - every user has access to all lists."""

    __tablename__ = "favorite_lists"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(String(300))
    created_by: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    items: Mapped[list["FavoriteItem"]] = relationship(
        back_populates="favorite_list", cascade="all, delete-orphan"
    )


class FavoriteItem(Base):
    __tablename__ = "favorite_items"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    list_id: Mapped[str] = mapped_column(
        ForeignKey("favorite_lists.id", ondelete="CASCADE"), nullable=False
    )
    wine_id: Mapped[str] = mapped_column(ForeignKey("wines.id", ondelete="CASCADE"), nullable=False)
    added_by: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    favorite_list: Mapped[FavoriteList] = relationship(back_populates="items")
    wine: Mapped[Wine] = relationship(back_populates="favorite_items")

    __table_args__ = (UniqueConstraint("list_id", "wine_id", name="uq_fav_list_wine"),)
