"""GET /api/announcements: what the administrator tells the apps, chosen for
the app that asks. See app.announcements and docs/api.md."""

from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from app import announcements
from app.api.routes import billing as billing_routes

router = APIRouter(tags=["announcements"])
# Reads through the billing routes' database, so tests can replace it.
cache = announcements.Cache(lambda: billing_routes.billing.announcements())
RETRY_SECONDS = 30


class AnnouncementLink(BaseModel):
    url: str
    # None when no label was given: the app shows its own, such as "Open".
    label: str | None = None


class Span(BaseModel):
    text: str
    bold: bool
    italic: bool
    # An https link, or None.
    link: str | None = None


class Paragraph(BaseModel):
    type: Literal["paragraph"]
    spans: list[Span]


class Heading(BaseModel):
    type: Literal["heading"]
    # 1 or 2.
    level: int
    spans: list[Span]


class BulletedOrNumbered(BaseModel):
    type: Literal["list"]
    ordered: bool
    # The first item's number, for numbered lists.
    start: int
    items: list[list[Span]]


Block = Annotated[Paragraph | Heading | BulletedOrNumbered, Field(discriminator="type")]


class AnnouncementResponse(BaseModel):
    id: str
    severity: Literal["info", "warning", "critical"]
    # The language of title, body, and the link's label.
    language: str
    title: str
    # The body without formatting; blocks has it formatted.
    body: str
    blocks: list[Block]
    link: AnnouncementLink | None = None
    dismissible: bool
    starts_at: int
    ends_at: int | None = None


class AnnouncementsResponse(BaseModel):
    announcements: list[AnnouncementResponse]


@router.get("/announcements", response_model=AnnouncementsResponse)
async def list_announcements(
    platform: str | None = Query(None, pattern=f"^{announcements.PLATFORM_PATTERN.pattern}$", description="android, ios, or web"),
    app_version: int | None = Query(None, ge=0, le=announcements.MAX_VERSION, description="the app's version code"),
    lang: str | None = Query(None, max_length=35, description="the user's language, such as pl or pt-BR"),
) -> AnnouncementsResponse:
    try:
        items = await run_in_threadpool(cache.get)
    except announcements.Unavailable:
        # Not an empty list: an app keeps showing what it has.
        raise HTTPException(
            status_code=503,
            detail="Announcements cannot be read at the moment",
            headers={"Retry-After": str(RETRY_SECONDS)},
        ) from None
    return AnnouncementsResponse(announcements=announcements.visible(items, platform, app_version, lang))
