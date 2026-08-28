"""Pydantic request/response schemas with strict validation."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models import WineType

COMMENT_MAX = 4000
COMMENT_MIN_ALLOWED = 1000  # requirement: at least 1000 characters must be accepted


def _reject_control_chars(v: str) -> str:
    """Refuse NUL and other C0 control characters.

    They have no place in a wine name and are a classic way to smuggle values
    past downstream consumers (log files, filenames, C-string boundaries).
    Tab, newline and carriage return stay allowed for free-text fields.
    """
    if any(ch in v for ch in "\x00\x0b\x0c") or any(
        ord(ch) < 32 and ch not in "\t\n\r" for ch in v
    ):
        raise ValueError("Value contains control characters")
    return v


def _clean(v: str | None, limit: int) -> str | None:
    if v is None:
        return None
    _reject_control_chars(v)
    v = v.strip()
    if not v:
        return None
    if len(v) > limit:
        raise ValueError(f"Value exceeds {limit} characters")
    return v


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=32)
    password: str = Field(min_length=1, max_length=128)


class RegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=12, max_length=128)
    display_name: str | None = Field(default=None, max_length=64)
    registration_code: str | None = Field(default=None, max_length=128)


class PasswordChangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=12, max_length=128)


class DisplayNameUpdate(BaseModel):
    """Self-service display-name change (any signed-in user)."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    display_name: str | None = Field(default=None, max_length=64)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    username: str
    display_name: str | None = None
    is_admin: bool = False
    created_at: datetime | None = None


class AdminPasswordResetRequest(BaseModel):
    """Admin reset of another account's password. No current-password check."""

    model_config = ConfigDict(extra="forbid")
    new_password: str = Field(min_length=12, max_length=128)


class UserCreateRequest(BaseModel):
    """Admin-created account. The admin supplies the initial password."""

    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=12, max_length=128)
    display_name: str | None = Field(default=None, max_length=64)


class WineBase(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=200)
    maker: str | None = Field(default=None, max_length=200)
    wine_type: WineType = WineType.other
    country: str | None = Field(default=None, max_length=100)
    region: str | None = Field(default=None, max_length=150)
    vintage: int | None = Field(default=None, ge=1800, le=2200)
    grape: str | None = Field(default=None, max_length=300)
    sugar_g_l: float | None = Field(default=None, ge=0, le=500)
    alcohol_pct: float | None = Field(default=None, ge=0, le=100)
    aromas: str | None = Field(default=None, max_length=2000)
    # Raw text read from the back label by the vision model. Optional; when
    # present it is merged into empty structured fields on creation (grape,
    # region, country, alcohol, sugar) so the back label enriches the card.
    back_label_text: str | None = Field(default=None, max_length=4000)

    acidity: int | None = Field(default=None, ge=0, le=3)
    sweetness: int | None = Field(default=None, ge=0, le=3)
    body: int | None = Field(default=None, ge=0, le=3)
    mouthfeel: int | None = Field(default=None, ge=0, le=3)
    wood: int | None = Field(default=None, ge=0, le=3)

    @field_validator(
        "name", "maker", "country", "region", "grape", "aromas", mode="after"
    )
    @classmethod
    def _no_control_chars(cls, v: str | None) -> str | None:
        return v if v is None else _reject_control_chars(v)

    @field_validator("maker", "country", "region", "grape", "aromas", mode="after")
    @classmethod
    def _empty_to_none(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return v.strip() or None


class WineCreate(WineBase):
    pass


class WineUpdate(BaseModel):
    """All fields editable; every field optional (PATCH semantics)."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str | None = Field(default=None, min_length=1, max_length=200)
    maker: str | None = Field(default=None, max_length=200)
    wine_type: WineType | None = None
    country: str | None = Field(default=None, max_length=100)
    region: str | None = Field(default=None, max_length=150)
    vintage: int | None = Field(default=None, ge=1800, le=2200)
    grape: str | None = Field(default=None, max_length=300)
    sugar_g_l: float | None = Field(default=None, ge=0, le=500)
    alcohol_pct: float | None = Field(default=None, ge=0, le=100)
    aromas: str | None = Field(default=None, max_length=2000)
    acidity: int | None = Field(default=None, ge=0, le=3)
    sweetness: int | None = Field(default=None, ge=0, le=3)
    body: int | None = Field(default=None, ge=0, le=3)
    mouthfeel: int | None = Field(default=None, ge=0, le=3)
    wood: int | None = Field(default=None, ge=0, le=3)


class RatingOut(BaseModel):
    user_id: str
    username: str
    stars: int


class CommentOut(BaseModel):
    id: str
    user_id: str
    username: str
    body: str
    created_at: datetime
    updated_at: datetime


class WineOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    maker: str | None
    wine_type: WineType
    country: str | None
    region: str | None
    vintage: int | None
    grape: str | None
    sugar_g_l: float | None
    alcohol_pct: float | None
    aromas: str | None
    acidity: int | None
    sweetness: int | None
    body: int | None
    mouthfeel: int | None
    wood: int | None
    photo_url: str | None = None
    created_at: datetime
    updated_at: datetime
    average_rating: float | None = None
    rating_count: int = 0
    my_rating: int | None = None
    comment_count: int = 0


class WineDetail(WineOut):
    ratings: list[RatingOut] = []
    comments: list[CommentOut] = []
    favorite_list_ids: list[str] = []


class RatingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stars: int = Field(ge=1, le=5)


class CommentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    body: str = Field(min_length=1, max_length=COMMENT_MAX)

    @field_validator("body", mode="after")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Comment cannot be empty")
        return v


class FavoriteListCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=300)


class FavoriteListUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str | None = Field(default=None, min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=300)


class FavoriteListOut(BaseModel):
    id: str
    name: str
    description: str | None = None
    wine_count: int = 0
    created_at: datetime


class SearchResponse(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[WineOut]


class EnrichRequest(BaseModel):
    """Enrichment lookup; only empty fields will be suggested by the server."""

    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, max_length=200)
    maker: str | None = Field(default=None, max_length=200)
    label_text: str | None = Field(default=None, max_length=4000)
    ask_back_label: bool = False


class EnrichResponse(BaseModel):
    suggestion: dict
    sources: list[str] = []
    confidence: str = "low"
    need_back_label: bool = False
    messages: list[str] = []
    # Raw text the vision model read from the label. Only populated when this
    # was a back-label scan (is_back_label=True), so the caller can persist it.
    back_label_text: str | None = None
