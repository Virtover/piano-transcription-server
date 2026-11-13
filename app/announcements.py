"""Announcements: messages from the administrator to the apps, such as how to
update the app if it is no longer in its store.

They are rows of the billing database (`announcements`), written with their
audit entry in one transaction, and kept in its backups. Only
`python -m app.admin announce` and the admin web page in read-write mode
write them; the API only reads them, for `GET /api/announcements`.

Everything an announcement holds is checked here, when it is written and
again when it is read, so a row changed by hand cannot reach an app unless
it is valid. Titles are plain text. A body may use a small formatting
language, parsed here into blocks (see parse_markup), so apps lay them out
without parsing anything: they never interpret text as HTML or Markdown.
Every link, in a body too, must be https.

The API keeps the last list it read for CACHE_SECONDS, so apps polling it do
not reach the database, and keeps answering from that list while the
database cannot be reached: an announcement that tells users how to keep
using the app must not disappear while the server has trouble.
"""

import json
import logging
import re
import threading
import time
import unicodedata
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

SEVERITIES = ("info", "warning", "critical")
# Higher is more severe; apps get the most severe first.
SEVERITY_ORDER = {severity: index for index, severity in enumerate(SEVERITIES)}
DEFAULT_LANGUAGE = "en"
ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
LANGUAGE_PATTERN = re.compile(r"[a-z]{2,3}(-[a-z0-9]{2,8})?")
PLATFORM_PATTERN = re.compile(r"[a-z][a-z0-9-]{0,19}")
TITLE_LENGTH = 120
BODY_LENGTH = 2000
LABEL_LENGTH = 60
URL_LENGTH = 500
MAX_LANGUAGES = 20
MAX_PLATFORMS = 10
# Announcements an app may be shown at once, and that may exist in all,
# ended ones included; ended ones can be replaced under the same ID.
MAX_ACTIVE = 10
MAX_STORED = 200
# App versions are whole numbers, such as Android's versionCode.
MAX_VERSION = 2**31 - 1
CACHE_SECONDS = 15.0
# Line breaks are kept in bodies; no other control or invisible formatting
# character is accepted, so a text cannot hide or reorder what users read.
ALLOWED_CONTROLS = {"\n"}


class InvalidAnnouncement(ValueError):
    """The message says what is wrong, for the administrator."""


# --- Formatting ---------------------------------------------------------------------
# A body's formatting, kept deliberately small, and parsed the same way by
# the admin web page's preview (admin.js, "Formatted text"; a test compares
# them):
#
#   Blank line           a new paragraph; a single line break is kept
#   # Title, ## Title    a heading, of level 1 or 2
#   - item, * item       a bulleted list; 1. item, 1) item: a numbered one
#   **bold**, *italic*   in any text; an unpaired marker is shown as typed
#   [text](https://...)  a link
#   \*                   a character shown as typed instead of formatting
#
# Apps get blocks: {"type": "paragraph" | "heading" | "list", ...} with
# spans {"text", "bold", "italic", "link"}; see docs/api.md.

MAX_LINKS = 10
MARKUP_ESCAPABLE = "\\*[]()#-_`~"
LINK_AT = re.compile(r"\[([^\[\]\n]+)\]\(([^()\s]+)\)")
HEADING_LINE = re.compile(r"(#{1,2}) (.*)")
BULLET_LINE = re.compile(r"([-*]) (.*)")
NUMBER_LINE = re.compile(r"([0-9]{1,3})[.)] (.*)")


def _inline(text: str, links: bool = True) -> list[dict]:
    tokens: list[dict] = []
    buffer = ""
    index = 0
    while index < len(text):
        character = text[index]
        if character == "\\" and index + 1 < len(text) and text[index + 1] in MARKUP_ESCAPABLE:
            buffer += text[index + 1]
            index += 2
            continue
        if text.startswith("**", index) or character == "*":
            if buffer:
                tokens.append({"text": buffer})
                buffer = ""
            marker = "**" if text.startswith("**", index) else "*"
            tokens.append({"marker": marker})
            index += len(marker)
            continue
        if character == "[" and links:
            match = LINK_AT.match(text, index)
            if match:
                if buffer:
                    tokens.append({"text": buffer})
                    buffer = ""
                tokens.append({"label": match[1], "url": match[2]})
                index = match.end()
                continue
        buffer += character
        index += 1
    if buffer:
        tokens.append({"text": buffer})
    # An unpaired marker is text, as typed.
    for marker in ("**", "*"):
        found = [token for token in tokens if token.get("marker") == marker]
        if len(found) % 2:
            found[-1].pop("marker")
            found[-1]["text"] = marker
    spans: list[dict] = []

    def add(piece: str, bold: bool, italic: bool, link: str | None) -> None:
        if not piece:
            return
        if spans and (spans[-1]["bold"], spans[-1]["italic"], spans[-1]["link"]) == (bold, italic, link):
            spans[-1]["text"] += piece
        else:
            spans.append({"text": piece, "bold": bold, "italic": italic, "link": link})

    bold = italic = False
    for token in tokens:
        if token.get("marker") == "**":
            bold = not bold
        elif token.get("marker") == "*":
            italic = not italic
        elif "url" in token:
            for span in _inline(token["label"], links=False):
                add(span["text"], span["bold"] or bold, span["italic"] or italic, token["url"])
        else:
            add(token["text"], bold, italic, None)
    return spans


def parse_markup(text: str) -> list[dict]:
    """The blocks of a formatted body. Never fails: anything that is not
    formatting is text. Links are checked by check_markup."""
    blocks: list[dict] = []
    paragraph: list[str] = []
    current_list = None

    def end_paragraph() -> None:
        if paragraph:
            spans = _inline("\n".join(paragraph))
            if spans:
                blocks.append({"type": "paragraph", "spans": spans})
            paragraph.clear()

    for line in text.split("\n"):
        line = line.strip()
        if not line:
            end_paragraph()
            current_list = None
            continue
        heading = HEADING_LINE.fullmatch(line)
        if heading:
            end_paragraph()
            current_list = None
            spans = _inline(heading[2].strip())
            if spans:
                blocks.append({"type": "heading", "level": len(heading[1]), "spans": spans})
            continue
        number = NUMBER_LINE.fullmatch(line)
        item = number or BULLET_LINE.fullmatch(line)
        if item:
            end_paragraph()
            ordered = number is not None
            if current_list is None or current_list["ordered"] != ordered:
                current_list = {"type": "list", "ordered": ordered, "start": int(number[1]) if number else 1, "items": []}
                blocks.append(current_list)
            current_list["items"].append(_inline(item[2].strip()))
            continue
        current_list = None
        paragraph.append(line)
    end_paragraph()
    return blocks


def _spans_text(spans: list[dict]) -> str:
    parts = []
    label = ""
    for index, span in enumerate(spans):
        parts.append(span["text"])
        if not span["link"]:
            continue
        label += span["text"]
        following = spans[index + 1]["link"] if index + 1 < len(spans) else None
        if following != span["link"]:
            # After the whole text of the link, unless that text is the link.
            if label.strip() != span["link"]:
                parts.append(f" ({span['link']})")
            label = ""
    return "".join(parts)


def plain_text(blocks: list[dict]) -> str:
    """The body without formatting, for apps that show only text: links
    follow their text in brackets, list items get a bullet or number."""
    parts = []
    for block in blocks:
        if block["type"] == "list":
            parts.append("\n".join(
                f"{block['start'] + index}. {_spans_text(item)}" if block["ordered"] else f"• {_spans_text(item)}"
                for index, item in enumerate(block["items"])
            ))
        else:
            parts.append(_spans_text(block["spans"]))
    return "\n\n".join(parts)


def check_markup(text: str, name: str) -> list[dict]:
    """The blocks of a body, or InvalidAnnouncement if one of its links is
    not acceptable or it shows nothing."""
    blocks = parse_markup(text)
    links = [
        span["link"]
        for block in blocks
        for spans in (block["items"] if block["type"] == "list" else [block["spans"]])
        for span in spans
        if span["link"]
    ]
    if len(set(links)) > MAX_LINKS:
        raise InvalidAnnouncement(f"{name} has more than {MAX_LINKS} links")
    for link in links:
        try:
            check_url(link)
        except InvalidAnnouncement as error:
            raise InvalidAnnouncement(f"{name}: {link[:60]}: {error}") from None
    if not any(span["text"].strip() for block in blocks for spans in (block["items"] if block["type"] == "list" else [block["spans"]]) for span in spans):
        raise InvalidAnnouncement(f"{name} shows no text")
    return blocks


# --- Checking ---------------------------------------------------------------------

def _text(value, name: str, length: int, multiline: bool = False) -> str:
    if not isinstance(value, str):
        raise InvalidAnnouncement(f"{name} must be text")
    value = unicodedata.normalize("NFC", value.replace("\r\n", "\n")).strip()
    if not value:
        raise InvalidAnnouncement(f"{name} must not be empty")
    if len(value) > length:
        raise InvalidAnnouncement(f"{name} must be at most {length} characters")
    for character in value:
        if character in ALLOWED_CONTROLS and multiline:
            continue
        if unicodedata.category(character) in ("Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"):
            raise InvalidAnnouncement(f"{name} contains a control or invisible character (U+{ord(character):04X})")
    return value


def check_id(value) -> str:
    if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
        raise InvalidAnnouncement("The ID must be 1 to 64 lowercase letters, digits, and hyphens, starting with a letter or digit")
    return value


# Letters that do not decompose into a base letter and a mark.
ID_LETTERS = str.maketrans({"ł": "l", "Ł": "l", "đ": "d", "Đ": "d", "ø": "o", "Ø": "o", "ß": "ss", "æ": "ae", "œ": "oe"})
# Room left for "-123", so a number can always be added.
ID_BASE_LENGTH = 56


def id_from_title(title: str) -> str:
    """An ID made from a title: "Zaktualizuj aplikację!" gives
    zaktualizuj-aplikacje. Not unique; see app.admin.announce.free_id."""
    text = unicodedata.normalize("NFKD", str(title).translate(ID_LETTERS))
    text = "".join(character for character in text if not unicodedata.combining(character)).lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")[:ID_BASE_LENGTH].strip("-")
    return text or "announcement"


def check_language(value) -> str:
    if not isinstance(value, str) or not LANGUAGE_PATTERN.fullmatch(value.lower()):
        raise InvalidAnnouncement(f"{value!r} is not a language code such as en, pl, or pt-br")
    return value.lower()


HOST_PATTERN = re.compile(r"(?=.{4,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?")


def check_url(value) -> str:
    """An https link with a host and nothing that could mislead: no user
    name or password, spaces, or control characters."""
    if not isinstance(value, str) or not value.strip():
        raise InvalidAnnouncement("The link must be text")
    value = value.strip()
    if len(value) > URL_LENGTH:
        raise InvalidAnnouncement(f"The link must be at most {URL_LENGTH} characters")
    if any(character.isspace() or unicodedata.category(character).startswith("C") for character in value):
        raise InvalidAnnouncement("The link must not contain spaces or control characters")
    # A host in another script can look like a known one (exаmple.com with a
    # Cyrillic а); its punycode form, xn--..., cannot.
    if not value.isascii():
        raise InvalidAnnouncement("The link must be ASCII: write an international host in its xn-- form, and encode other characters with %")
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError as error:
        raise InvalidAnnouncement(f"The link is not valid: {error}") from None
    if parts.scheme.lower() != "https":
        raise InvalidAnnouncement("The link must start with https://")
    # A backslash is a slash to browsers but not to every app's URL parser,
    # so the two could disagree about the host.
    if "\\" in value:
        raise InvalidAnnouncement("The link must not contain a backslash")
    # A plain domain name: no IP address, percent-encoding, underscore, or
    # trailing dot, each of which can hide where a link leads.
    if not parts.hostname or not HOST_PATTERN.fullmatch(parts.hostname):
        raise InvalidAnnouncement("The link must name a host such as example.com: no IP address or unusual characters")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise InvalidAnnouncement("The link must not contain a user name or password")
    if port is not None and port != 443:
        raise InvalidAnnouncement("The link must use the standard https port")
    return value


def _version(value, name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_VERSION:
        raise InvalidAnnouncement(f"{name} must be a whole number from 0 to {MAX_VERSION}")
    return value


def _time(value, name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value < 2**40:
        raise InvalidAnnouncement(f"{name} must be a Unix time in seconds")
    return int(value)


def check(data: dict) -> dict:
    """The announcement in data, normalized, or InvalidAnnouncement. Unknown
    fields are refused, so a misspelt one is not silently ignored."""
    if not isinstance(data, dict):
        raise InvalidAnnouncement("An announcement must be an object")
    known = {"id", "severity", "texts", "link_url", "dismissible", "platforms", "min_version", "max_version", "starts_at", "ends_at"}
    unknown = sorted(set(data) - known)
    if unknown:
        raise InvalidAnnouncement(f"Unknown fields: {', '.join(unknown)}")
    severity = data.get("severity", "info")
    if severity not in SEVERITIES:
        raise InvalidAnnouncement(f"The severity must be one of {', '.join(SEVERITIES)}")
    texts = data.get("texts")
    if not isinstance(texts, dict) or not texts:
        raise InvalidAnnouncement("Give a title and a body, at least in English")
    if len(texts) > MAX_LANGUAGES:
        raise InvalidAnnouncement(f"At most {MAX_LANGUAGES} languages")
    link_url = check_url(data["link_url"]) if data.get("link_url") is not None else None
    checked_texts = {}
    for language, text in texts.items():
        language = check_language(language)
        if language in checked_texts:
            raise InvalidAnnouncement(f"The language {language} is given twice")
        if not isinstance(text, dict) or set(text) - {"title", "body", "link_label"}:
            raise InvalidAnnouncement(f"The {language} text must have a title, a body, and optionally a link_label")
        label = text.get("link_label")
        if label is not None and link_url is None:
            raise InvalidAnnouncement("A link label needs a link")
        checked_texts[language] = {
            "title": _text(text.get("title"), f"The {language} title", TITLE_LENGTH),
            "body": _text(text.get("body"), f"The {language} body", BODY_LENGTH, multiline=True),
            "link_label": _text(label, f"The {language} link label", LABEL_LENGTH) if label is not None else None,
        }
        check_markup(checked_texts[language]["body"], f"The {language} body")
    if DEFAULT_LANGUAGE not in checked_texts:
        raise InvalidAnnouncement("Give the text in English too: apps in other languages fall back to it")
    platforms = data.get("platforms") or []
    if not isinstance(platforms, list) or len(platforms) > MAX_PLATFORMS:
        raise InvalidAnnouncement(f"The platforms must be a list of at most {MAX_PLATFORMS}")
    for platform in platforms:
        if not isinstance(platform, str) or not PLATFORM_PATTERN.fullmatch(platform):
            raise InvalidAnnouncement(f"{platform!r} is not a platform name such as android or ios")
    min_version = _version(data.get("min_version"), "The lowest app version")
    max_version = _version(data.get("max_version"), "The highest app version")
    if min_version is not None and max_version is not None and min_version > max_version:
        raise InvalidAnnouncement("The lowest app version is above the highest")
    starts_at = _time(data.get("starts_at"), "The start") or 0
    ends_at = _time(data.get("ends_at"), "The end")
    if ends_at is not None and ends_at <= starts_at:
        raise InvalidAnnouncement("The end must be after the start")
    dismissible = data.get("dismissible")
    if dismissible is None:
        dismissible = severity != "critical"
    if not isinstance(dismissible, bool):
        raise InvalidAnnouncement("dismissible must be true or false")
    return {
        "id": check_id(data.get("id")),
        "severity": severity,
        "texts": dict(sorted(checked_texts.items())),
        "link_url": link_url,
        "dismissible": dismissible,
        "platforms": sorted(set(platforms)),
        "min_version": min_version,
        "max_version": max_version,
        "starts_at": starts_at,
        "ends_at": ends_at,
    }


# --- Rows -------------------------------------------------------------------------------

def to_row(announcement: dict) -> dict:
    """The database columns of a checked announcement. What apps are shown is
    kept as one JSON value; the times, for queries, also as columns."""
    content = {key: value for key, value in announcement.items() if key not in ("id", "starts_at", "ends_at")}
    return {
        "id": announcement["id"],
        "content": json.dumps(content, sort_keys=True, ensure_ascii=False),
        "starts_at": announcement["starts_at"],
        "ends_at": announcement["ends_at"],
    }


def from_row(row, quiet: bool = False) -> dict | None:
    """The announcement of a database row with its record fields, or None,
    logged unless quiet, if the row is not valid. Never raises: one bad row
    must not hide the others."""
    try:
        row = dict(row)
        content = json.loads(row["content"])
        if not isinstance(content, dict):
            raise InvalidAnnouncement("the content is not an object")
        announcement = check({**content, "id": row["id"], "starts_at": row["starts_at"], "ends_at": row["ends_at"]})
    except Exception as error:
        if not quiet:
            logger.error("announcement %r is not valid and is not shown: %s", row.get("id") if isinstance(row, dict) else None, error)
        return None
    for field in ("created_at", "updated_at", "created_by"):
        announcement[field] = row.get(field)
    # Parsed once here, when the list is read, not on every request.
    announcement[PARSED] = {language: parse_markup(text["body"]) for language, text in announcement["texts"].items()}
    return announcement


# Where from_row keeps each language's parsed body; not shown to anyone.
PARSED = "_parsed"


def is_active(announcement: dict, now: float) -> bool:
    return announcement["starts_at"] <= now and (announcement["ends_at"] is None or now < announcement["ends_at"])


def state(announcement: dict, now: float) -> str:
    if announcement["ends_at"] is not None and announcement["ends_at"] <= now:
        return "ended"
    return "active" if announcement["starts_at"] <= now else "scheduled"


# --- What an app is shown ---------------------------------------------------------------

def matches(announcement: dict, platform: str | None, app_version: int | None) -> bool:
    """Whether an app on platform at app_version is meant. An app that does
    not say its platform or version gets only announcements for every
    platform or version: a takedown notice for old versions is not shown to
    a client that may be new."""
    if announcement["platforms"] and platform not in announcement["platforms"]:
        return False
    if announcement["min_version"] is not None and (app_version is None or app_version < announcement["min_version"]):
        return False
    if announcement["max_version"] is not None and (app_version is None or app_version > announcement["max_version"]):
        return False
    return True


def pick_language(texts: dict, wanted: str | None) -> str:
    """wanted, such as pt-br, else its base language, pt, else English."""
    if wanted:
        wanted = wanted.strip().lower().replace("_", "-")
        if wanted in texts:
            return wanted
        base = wanted.split("-")[0]
        if base in texts:
            return base
    return DEFAULT_LANGUAGE


def for_app(announcement: dict, language: str | None) -> dict:
    chosen = pick_language(announcement["texts"], language)
    text = announcement["texts"][chosen]
    parsed = announcement.get(PARSED) or {}
    blocks = parsed[chosen] if chosen in parsed else parse_markup(text["body"])
    link = None
    if announcement["link_url"]:
        label = text["link_label"] or announcement["texts"][DEFAULT_LANGUAGE]["link_label"]
        link = {"url": announcement["link_url"], "label": label}
    return {
        "id": announcement["id"],
        "severity": announcement["severity"],
        "language": chosen,
        "title": text["title"],
        "body": plain_text(blocks),
        "blocks": blocks,
        "link": link,
        "dismissible": announcement["dismissible"],
        "starts_at": announcement["starts_at"],
        "ends_at": announcement["ends_at"],
    }


def visible(
    announcements: list[dict],
    platform: str | None,
    app_version: int | None,
    language: str | None,
    now: float | None = None,
) -> list[dict]:
    """What an app is shown: active and meant for it, most severe first,
    then the newest, at most MAX_ACTIVE."""
    now = time.time() if now is None else now
    shown = [item for item in announcements if is_active(item, now) and matches(item, platform, app_version)]
    shown.sort(key=lambda item: (-SEVERITY_ORDER[item["severity"]], -item["starts_at"], item["id"]))
    return [for_app(item, language) for item in shown[:MAX_ACTIVE]]


class Unavailable(Exception):
    """The announcements cannot be read, and none were read before."""


class Cache:
    """The announcements as last read, for every request of one process.
    load reads all that have not ended. A failed read keeps the last list:
    times are checked on every request, so one that ends meanwhile still
    ends on time.

    One request at a time reads the database. While it does, the others get
    the last list at once instead of waiting, so a slow database never ties
    up the API's threads; only the very first read is waited for. After a
    failed read, the database is tried again only after `seconds`."""

    def __init__(self, load, seconds: float = CACHE_SECONDS, clock=time.monotonic):
        self._load = load
        self._seconds = seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._items: list[dict] | None = None
        self._read_at = 0.0
        # The newest time an announcement was written: the clock cannot
        # really be earlier, so `now` never is; see now().
        self._newest = 0
        self._clock_warned = False
        self._failed_at: float | None = None
        self._error = ""
        # Invalid rows already logged, so each is logged once, not on every read.
        self._reported: set[tuple] = set()

    def _fresh(self) -> bool:
        return self._items is not None and self._clock() - self._read_at < self._seconds

    def _failed_recently(self) -> bool:
        return self._failed_at is not None and self._clock() - self._failed_at < self._seconds

    def get(self) -> list[dict]:
        if self._fresh():
            return self._items
        if not self._lock.acquire(blocking=self._items is None):
            # Another request is reading; the last list will do meanwhile.
            return self._items
        try:
            if self._fresh():
                return self._items
            if self._failed_recently():
                if self._items is None:
                    raise Unavailable(self._error)
                return self._items
            try:
                rows = self._load()
            except Exception as error:
                if self._failed_at is None:
                    logger.warning("cannot read the announcements (%s)%s", error,
                                   "; answering with the last ones read" if self._items is not None else "")
                self._failed_at = self._clock()
                self._error = str(error)
                if self._items is None:
                    raise Unavailable(self._error) from error
                return self._items
            if self._failed_at is not None:
                logger.info("announcements can be read again")
            self._failed_at = None
            items = []
            for row in rows:
                key = (dict(row).get("id"), dict(row).get("content"))
                item = from_row(row, quiet=key in self._reported)
                if item is None:
                    self._reported.add(key)
                else:
                    items.append(item)
            self._items = items
            self._newest = max([self._newest] + [int(item.get("updated_at") or 0) for item in items])
            self._read_at = self._clock()
            return items
        finally:
            self._lock.release()

    def now(self, wall: float | None = None) -> float:
        """The time to choose announcements by. After a power cut, a server
        whose clock did not sync can start years in the past, which would
        hide every announcement as not started yet; the time an
        announcement was last written is a floor for it."""
        wall = time.time() if wall is None else wall
        if wall >= self._newest:
            return wall
        if not self._clock_warned:
            logger.error("the system clock is behind the newest announcement's change; is the clock synchronized?")
            self._clock_warned = True
        return float(self._newest)

    def clear(self) -> None:
        with self._lock:
            self._items = None
            self._read_at = 0.0
            self._failed_at = None
