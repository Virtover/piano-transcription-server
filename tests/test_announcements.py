"""Announcements: what is accepted, which app sees which, the public
endpoint and how it keeps answering when the database cannot, and the
`announce` admin commands. The admin web page's part is in
test_admin_web.py."""

import builtins
import time

import pytest

from app import announcements
from app.admin import common
from app.admin.__main__ import main
from app.api.routes import announcements as announcement_routes
from app.billing import ADMIN_WEB_WRITES
from app.worker import tasks


def valid(**changes):
    data = {
        "id": "play-takedown",
        "severity": "critical",
        "texts": {"en": {"title": "Update the app another way", "body": "The app left Google Play.\nSee the link."}},
        "link_url": "https://example.com/update",
    }
    data.update(changes)
    return data


def admin(capsys, *arguments, answer=None, monkeypatch=None):
    if answer is not None:
        monkeypatch.setattr(common, "interactive", lambda: True)
        monkeypatch.setattr(builtins, "input", lambda question: answer)
    code = main(list(arguments))
    captured = capsys.readouterr()
    return code, captured.out + captured.err


def audit_rows(billing):
    return [dict(row) for row in billing.read("SELECT * FROM admin_actions ORDER BY id")]


def fetch(client, **parameters):
    announcement_routes.cache.clear()
    return client.get("/api/announcements", params=parameters)


# --- What is accepted ---------------------------------------------------------------

def test_a_valid_announcement_is_normalized():
    item = announcements.check(valid(texts={"en": {"title": "  Hi  ", "body": "Line one\r\nLine two"}}, platforms=["ios", "android", "ios"]))

    assert item["texts"]["en"] == {"title": "Hi", "body": "Line one\nLine two", "link_label": None}
    assert item["platforms"] == ["android", "ios"]
    assert item["starts_at"] == 0 and item["ends_at"] is None


@pytest.mark.parametrize("severity, dismissible", [("info", True), ("warning", True), ("critical", False)])
def test_only_critical_ones_cannot_be_dismissed_by_default(severity, dismissible):
    assert announcements.check(valid(severity=severity))["dismissible"] is dismissible


@pytest.mark.parametrize(
    "changes, problem",
    [
        ({"texts": {"pl": {"title": "Tytuł", "body": "Treść"}}}, "in English too"),
        ({"texts": {}}, "at least in English"),
        ({"link_url": "http://example.com/update"}, "https://"),
        ({"link_url": "javascript:alert(1)"}, "https://"),
        ({"link_url": "https://user:pass@example.com/"}, "user name or password"),
        ({"link_url": "https://example.com@evil.example/"}, "user name or password"),
        ({"link_url": "https://example.com:8443/"}, "standard https port"),
        ({"link_url": "https://localhost/"}, "name a host"),
        ({"link_url": "https://example.com/a b"}, "spaces"),
        ({"link_url": "https://" + "a" * 500 + ".com"}, "at most 500"),
        ({"texts": {"en": {"title": "Two\nlines", "body": "x"}}}, "control or invisible"),
        ({"texts": {"en": {"title": "Pay‮evil", "body": "x"}}}, "U+202E"),
        ({"texts": {"en": {"title": "Hi", "body": "zero​width"}}}, "U+200B"),
        ({"texts": {"en": {"title": "x" * 121, "body": "x"}}}, "at most 120"),
        ({"texts": {"en": {"title": "Hi", "body": "   "}}}, "must not be empty"),
        ({"texts": {"en": {"title": "Hi", "body": "x", "html": "<b>"}}}, "a title, a body"),
        ({"texts": {"en": {"title": "Hi", "body": "x"}, "<script>": {"title": "a", "body": "b"}}}, "language code"),
        ({"link_url": None, "texts": {"en": {"title": "Hi", "body": "x", "link_label": "Open"}}}, "needs a link"),
        ({"id": "Play Takedown"}, "lowercase"),
        ({"id": "-x"}, "lowercase"),
        ({"severity": "urgent"}, "severity"),
        ({"min_version": 5, "max_version": 4}, "above the highest"),
        ({"min_version": -1}, "whole number"),
        ({"max_version": True}, "whole number"),
        ({"starts_at": 100, "ends_at": 100}, "after the start"),
        ({"platforms": ["Android"]}, "platform name"),
        ({"dismissible": "no"}, "true or false"),
        ({"colour": "red"}, "Unknown fields: colour"),
    ],
)
def test_anything_else_is_refused(changes, problem):
    with pytest.raises(announcements.InvalidAnnouncement, match=problem.replace("+", r"\+")):
        announcements.check(valid(**changes))


def test_too_many_languages_are_refused():
    codes = [f"{first}{second}" for first in "abcd" for second in "abcde"]
    texts = {"en": {"title": "a", "body": "b"}, **{code: {"title": "a", "body": "b"} for code in codes}}

    with pytest.raises(announcements.InvalidAnnouncement, match="At most 20 languages"):
        announcements.check(valid(texts=texts))


# --- Which app sees which ---------------------------------------------------------------

def item(**changes):
    return announcements.check(valid(**changes))


def test_versions_and_platforms_choose_the_apps():
    old_android = item(platforms=["android"], max_version=41)

    assert announcements.matches(old_android, "android", 41)
    assert not announcements.matches(old_android, "android", 42)
    assert not announcements.matches(old_android, "ios", 41)
    # An app that does not say its version may be a new one.
    assert not announcements.matches(old_android, "android", None)
    assert announcements.matches(item(), None, None)
    assert not announcements.matches(item(min_version=10), "android", 9)


def test_the_language_falls_back_to_its_base_then_english():
    texts = {"en": {"title": "Hi", "body": "x"}, "pt": {"title": "Olá", "body": "x"}, "pt-br": {"title": "Oi", "body": "x"}}
    announcement = item(texts=texts)

    assert announcements.for_app(announcement, "pt-BR")["title"] == "Oi"
    assert announcements.for_app(announcement, "pt_PT")["title"] == "Olá"
    assert announcements.for_app(announcement, "de")["title"] == "Hi"
    assert announcements.for_app(announcement, None)["language"] == "en"


def test_the_link_label_falls_back_to_english():
    texts = {"en": {"title": "Hi", "body": "x", "link_label": "How to update"}, "pl": {"title": "Cześć", "body": "x"}}

    shown = announcements.for_app(item(texts=texts), "pl")

    assert shown["link"] == {"url": "https://example.com/update", "label": "How to update"}


def test_only_active_ones_are_shown_most_severe_and_newest_first():
    now = 10_000
    items = [
        item(id="old-info", severity="info", starts_at=100),
        item(id="new-info", severity="info", starts_at=200),
        item(id="critical", severity="critical", starts_at=50),
        item(id="future", starts_at=now + 1),
        item(id="over", starts_at=1, ends_at=now),
    ]

    shown = announcements.visible(items, "android", 1, "en", now=now)

    assert [entry["id"] for entry in shown] == ["critical", "new-info", "old-info"]


def test_at_most_a_few_are_shown_at_once():
    items = [item(id=f"note-{index}", severity="info") for index in range(announcements.MAX_ACTIVE + 5)]

    assert len(announcements.visible(items, None, None, None)) == announcements.MAX_ACTIVE


# --- The cache ---------------------------------------------------------------------------

def test_the_cache_reads_the_database_at_most_every_few_seconds():
    reads = []
    clock = [0.0]
    cache = announcements.Cache(lambda: reads.append(1) or [], seconds=15, clock=lambda: clock[0])

    cache.get()
    cache.get()
    clock[0] = 16
    cache.get()

    assert len(reads) == 2


def test_the_last_list_is_kept_while_the_database_cannot_be_read():
    row = announcements.to_row(item())
    clock = [0.0]
    failing = [False]

    def load():
        if failing[0]:
            raise ConnectionError("database down")
        return [{**row, "created_at": 1, "updated_at": 1, "created_by": "chris"}]

    cache = announcements.Cache(load, seconds=15, clock=lambda: clock[0])
    assert [entry["id"] for entry in cache.get()] == ["play-takedown"]
    failing[0] = True
    clock[0] = 100

    assert [entry["id"] for entry in cache.get()] == ["play-takedown"]


def test_with_nothing_read_yet_the_cache_says_it_is_unavailable():
    def load():
        raise ConnectionError("database down")

    with pytest.raises(announcements.Unavailable):
        announcements.Cache(load).get()


# --- The endpoint -------------------------------------------------------------------------

def publish(billing, actor="chris", **changes):
    announcement = item(**changes)
    billing.save_announcement(announcements.to_row(announcement), actor, "test", None, replace=False, max_stored=announcements.MAX_STORED)
    return announcement


def test_apps_get_what_is_meant_for_them(api_context):
    client, _, billing = api_context
    publish(billing, platforms=["android"], max_version=41)
    publish(billing, id="welcome", severity="info", texts={"en": {"title": "Hello", "body": "News"}, "pl": {"title": "Cześć", "body": "Nowości"}}, link_url=None)

    old = fetch(client, platform="android", app_version=41, lang="pl").json()["announcements"]
    new = fetch(client, platform="android", app_version=42, lang="pl").json()["announcements"]

    assert [entry["id"] for entry in old] == ["play-takedown", "welcome"]
    assert old[0] == {
        "id": "play-takedown", "severity": "critical", "language": "en", "title": "Update the app another way",
        "body": "The app left Google Play.\nSee the link.",
        "blocks": [{"type": "paragraph", "spans": [{"text": "The app left Google Play.\nSee the link.", "bold": False, "italic": False, "link": None}]}],
        "link": {"url": "https://example.com/update", "label": None},
        "dismissible": False, "starts_at": 0, "ends_at": None,
    }
    assert [(entry["id"], entry["title"]) for entry in new] == [("welcome", "Cześć")]


def test_nothing_announced_is_an_empty_list_and_never_cached_by_others(api_context):
    client, _, _ = api_context

    response = fetch(client)

    assert response.status_code == 200 and response.json() == {"announcements": []}
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("parameters", [{"platform": "Android!"}, {"app_version": -1}, {"app_version": "x"}, {"lang": "x" * 36}])
def test_odd_parameters_are_refused(api_context, parameters):
    client, _, _ = api_context

    assert fetch(client, **parameters).status_code == 422


def test_an_announcement_ends_on_time_even_from_the_cache(api_context, monkeypatch):
    client, _, billing = api_context
    publish(billing, starts_at=int(time.time()) - 10, ends_at=int(time.time()) + 60)
    assert len(fetch(client).json()["announcements"]) == 1

    later = time.time() + 120
    monkeypatch.setattr(announcements.time, "time", lambda: later)
    response = client.get("/api/announcements")

    assert response.json()["announcements"] == []


def test_the_endpoint_keeps_answering_while_the_database_is_down(api_context, monkeypatch):
    client, _, billing = api_context
    publish(billing)
    assert len(fetch(client).json()["announcements"]) == 1

    def down(*args, **kwargs):
        raise ConnectionError("database down")

    monkeypatch.setattr(billing, "announcements", down)
    monkeypatch.setattr(announcement_routes.cache, "_read_at", -1e9)
    response = client.get("/api/announcements")

    assert response.status_code == 200 and len(response.json()["announcements"]) == 1


def test_a_database_down_from_the_start_is_a_503_not_an_empty_list(api_context, monkeypatch):
    client, _, billing = api_context

    def down(*args, **kwargs):
        raise ConnectionError("database down")

    monkeypatch.setattr(billing, "announcements", down)
    response = fetch(client)

    assert response.status_code == 503 and response.headers["retry-after"] == "30"


def test_a_row_changed_by_hand_into_something_invalid_is_not_shown(api_context, caplog):
    client, _, billing = api_context
    publish(billing)
    publish(billing, id="other", severity="info")
    with billing._connect() as connection:
        connection.execute("UPDATE announcements SET content = ? WHERE id = 'other'", ('{"severity": "info", "texts": {"en": {"title": "<b>x</b>", "body": "\\u202e"}}}',))

    shown = fetch(client).json()["announcements"]

    assert [entry["id"] for entry in shown] == ["play-takedown"]
    assert "'other' is not valid" in caplog.text


# --- The commands -------------------------------------------------------------------------

def test_publishing_asks_first_and_is_audited(api_context, capsys, monkeypatch):
    client, _, billing = api_context

    code, output = admin(capsys, "announce", "add", "play-takedown", "--title", "Update the app another way",
                         "--body", "The app left Google Play.\\nSee the link.", "--severity", "critical",
                         "--link", "https://example.com/update", "--link-label", "How to update",
                         "--translation", "pl", "Zaktualizuj aplikację inaczej", "Aplikacji nie ma w Google Play.", "Jak zaktualizować",
                         "--platform", "android", "--max-version", "41", "--reason", "store takedown",
                         answer="y", monkeypatch=monkeypatch)

    assert code == 0, output
    assert "cannot be dismissed" in output and "versions 41 and earlier" in output
    assert "announce end play-takedown" in output
    [shown] = fetch(client, platform="android", app_version=40, lang="pl").json()["announcements"]
    assert shown["title"] == "Zaktualizuj aplikację inaczej" and shown["link"]["label"] == "Jak zaktualizować"
    assert fetch(client, platform="android", app_version=40).json()["announcements"][0]["body"] == "The app left Google Play.\nSee the link."
    row = audit_rows(billing)[-1]
    assert (row["action"], row["target"], row["reason"]) == ("announce.add", "play-takedown", "store takedown")
    assert "critical" in row["detail"]


def test_saying_no_or_a_dry_run_changes_nothing(api_context, capsys, monkeypatch):
    _, _, billing = api_context
    arguments = ["announce", "add", "note", "--title", "Hi", "--body", "News"]

    assert admin(capsys, *arguments, answer="n", monkeypatch=monkeypatch)[0] == 0
    assert admin(capsys, *arguments, "--dry-run")[0] == 0

    assert billing.announcements(ended=True) == []


def test_an_id_in_use_needs_replace(api_context, capsys):
    client, _, billing = api_context
    arguments = ["announce", "add", "note", "--title", "Hi", "--yes", "--body"]
    assert admin(capsys, *arguments, "News")[0] == 0

    code, output = admin(capsys, *arguments, "Changed")
    assert code == 1 and "--replace" in output

    assert admin(capsys, *arguments, "Changed", "--replace")[0] == 0
    assert fetch(client).json()["announcements"][0]["body"] == "Changed"
    assert [row["action"] for row in audit_rows(billing)] == ["announce.add", "announce.replace"]


def test_invalid_input_is_refused_with_the_reason(api_context, capsys):
    _, _, billing = api_context

    code, output = admin(capsys, "announce", "add", "note", "--title", "Hi", "--body", "x", "--link", "http://example.com", "--yes")
    assert code == 1 and "https://" in output
    code, output = admin(capsys, "announce", "add", "note", "--title", "Hi", "--body", "x", "--reason", "for sam@example.com", "--yes")
    assert code == 1 and "email addresses" in output
    code, output = admin(capsys, "announce", "add", "note", "--title", "Hi", "--body", "x", "--translation", "pl", "Only a title", "--yes")
    assert code == 1 and "--translation takes" in output

    assert billing.announcements(ended=True) == []


def test_ending_keeps_it_listed_as_ended(api_context, capsys):
    client, _, billing = api_context
    admin(capsys, "announce", "add", "note", "--title", "Hi", "--body", "News", "--yes")

    code, output = admin(capsys, "announce", "end", "note", "--yes", "--reason", "over")

    assert code == 0 and "has ended" in output
    assert fetch(client).json()["announcements"] == []
    assert admin(capsys, "announce")[1].startswith("No announcements")
    assert "[ended]" in admin(capsys, "announce", "--all")[1]
    assert "already ended" in admin(capsys, "announce", "end", "note", "--yes")[1]
    assert audit_rows(billing)[-1]["action"] == "announce.end"
    assert admin(capsys, "announce", "end", "missing", "--yes")[0] == 1


def test_ending_a_scheduled_one_keeps_its_row_valid(api_context, capsys):
    _, _, billing = api_context
    admin(capsys, "announce", "add", "later", "--title", "Hi", "--body", "News", "--start", "1d", "--for", "1d", "--yes")

    admin(capsys, "announce", "end", "later", "--yes")

    [row] = billing.announcements(ended=True)
    assert announcements.from_row(row) is not None and row["ends_at"] <= time.time()


def test_the_count_kept_is_bounded(api_context, monkeypatch):
    _, _, billing = api_context
    publish(billing, id="one")

    with pytest.raises(ValueError, match="the most kept"):
        billing.save_announcement(announcements.to_row(item(id="two")), "chris", "x", None, replace=False, max_stored=1)


def test_the_list_as_json(api_context, capsys):
    import json

    admin(capsys, "announce", "add", "note", "--title", "Hi", "--body", "News", "--for", "2h", "--yes")

    [listed] = json.loads(admin(capsys, "announce", "--json")[1])

    assert listed["id"] == "note" and listed["state"] == "active" and listed["ends_at"] - listed["starts_at"] == 7200


def test_the_admin_web_page_may_add_and_end_but_never_delete():
    assert ADMIN_WEB_WRITES["read-write"]["announcements"].startswith("INSERT, UPDATE (")
    assert "DELETE" not in ADMIN_WEB_WRITES["read-write"]["announcements"]
    assert "announcements" not in ADMIN_WEB_WRITES["read-only"]


def test_the_job_api_s_redis_is_not_needed(api_context, monkeypatch):
    """Announcements live in the billing database: a Redis outage, or one
    emptied after a crash, does not take them away."""
    client, _, billing = api_context
    publish(billing)

    def down(*args, **kwargs):
        raise ConnectionError("redis down")

    monkeypatch.setattr(tasks.redis, "hgetall", down)
    response = fetch(client)

    assert response.status_code == 200 and len(response.json()["announcements"]) == 1


# --- Formatting -----------------------------------------------------------------------------

MARKUP_CASES = [
    "Plain text",
    "Line one\nLine two\n\nNew paragraph",
    "# Heading\n## Subheading\n### Not a heading",
    "**bold** and *italic* and ***both***",
    "An *unpaired star and **bold**",
    "**bold *with italic* inside**",
    r"\*not italic\* and \[not a link](x)",
    "See [the guide](https://example.com/a_b?c=1) now",
    "[**bold link**](https://example.com)",
    "[broken](https://example.com",
    "- one\n- **two**\n* three",
    "3. three\n4) four\n\n1. new list",
    "- bullet\n1. number\nafter",
    "-not a list\n1.not a list either",
    "  indented  \n\n\n  - spaced item  ",
    "Ünïcödé — ✓ **żółć**",
    "**",
    "a ** b",
]


def spans(text, bold=False, italic=False, link=None):
    return {"text": text, "bold": bold, "italic": italic, "link": link}


def test_paragraphs_line_breaks_and_headings():
    assert announcements.parse_markup("Line one\nLine two\n\nNext\n# Big\n## Small\n### Three") == [
        {"type": "paragraph", "spans": [spans("Line one\nLine two")]},
        {"type": "paragraph", "spans": [spans("Next")]},
        {"type": "heading", "level": 1, "spans": [spans("Big")]},
        {"type": "heading", "level": 2, "spans": [spans("Small")]},
        {"type": "paragraph", "spans": [spans("### Three")]},
    ]


def test_bold_italic_links_and_escapes():
    [block] = announcements.parse_markup(r"**B** *I* ***BI*** [go **now**](https://example.com) \*x\* *open")

    assert block["spans"] == [
        spans("B", bold=True), spans(" "), spans("I", italic=True), spans(" "), spans("BI", bold=True, italic=True), spans(" "),
        spans("go ", link="https://example.com"), spans("now", bold=True, link="https://example.com"), spans(" *x* *open"),
    ]


def test_lists_numbered_from_their_first_item():
    blocks = announcements.parse_markup("- a\n* b\n3. c\n4) d\nafter")

    assert blocks[0] == {"type": "list", "ordered": False, "start": 1, "items": [[spans("a")], [spans("b")]]}
    assert blocks[1] == {"type": "list", "ordered": True, "start": 3, "items": [[spans("c")], [spans("d")]]}
    assert blocks[2] == {"type": "paragraph", "spans": [spans("after")]}


def test_plain_text_for_apps_without_formatting():
    blocks = announcements.parse_markup("# Moved\nRead [the guide](https://example.com/x) or https://example.com/y.\n\n- one\n1. first")

    assert announcements.plain_text(blocks) == (
        "Moved\n\nRead the guide (https://example.com/x) or https://example.com/y.\n\n• one\n\n1. first"
    )


@pytest.mark.parametrize(
    "body, problem",
    [
        ("[click](javascript:alert)", "https://"),
        ("[click](http://example.com)", "https://"),
        ("[click](https://user@example.com)", "user name"),
        ("[click](https://exаmple.com)", "ASCII"),
        (" ".join(f"[{index}](https://example.com/{index})" for index in range(11)), "more than 10 links"),
        ("** **", "shows no text"),
    ],
)
def test_links_in_a_body_are_checked_like_the_link_button(body, problem):
    with pytest.raises(announcements.InvalidAnnouncement, match=problem):
        announcements.check(valid(texts={"en": {"title": "Hi", "body": body}}))


def test_what_is_not_a_link_stays_text():
    [block] = announcements.parse_markup("[click](javascript:alert(1)) and **")

    assert block["spans"] == [spans("[click](javascript:alert(1)) and **")]


def test_a_look_alike_host_is_refused():
    with pytest.raises(announcements.InvalidAnnouncement, match="ASCII"):
        announcements.check(valid(link_url="https://gооgle.com/"))
    assert announcements.check(valid(link_url="https://xn--ggle-55da.com/"))["link_url"] == "https://xn--ggle-55da.com/"


def test_apps_get_blocks_and_plain_text(api_context):
    client, _, billing = api_context
    publish(billing, texts={"en": {"title": "Hi", "body": "**Update** via [the guide](https://example.com/u)"}})

    [shown] = fetch(client).json()["announcements"]

    assert shown["body"] == "Update via the guide (https://example.com/u)"
    assert shown["blocks"] == [{"type": "paragraph", "spans": [
        spans("Update", bold=True), spans(" via "), spans("the guide", link="https://example.com/u"),
    ]}]


def test_the_page_preview_parses_exactly_like_the_server(tmp_path):
    """The admin page's preview runs the same parser, in JavaScript."""
    import json
    import shutil
    import subprocess
    from pathlib import Path

    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is not installed")
    script = (Path(__file__).resolve().parent.parent / "app" / "admin_web" / "static" / "admin.js").read_text(encoding="utf-8")
    start = script.index("// --- Formatted text")
    end = script.index("// --- End of formatted text")
    harness = tmp_path / "harness.js"
    cases = tmp_path / "cases.json"
    cases.write_text(json.dumps(MARKUP_CASES), encoding="utf-8")
    harness.write_text(
        script[start:end]
        + "\nconst fs = require('fs');\n"
        + "const cases = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));\n"
        + "process.stdout.write(JSON.stringify(cases.map(parseMarkup)));\n",
        encoding="utf-8",
    )

    result = subprocess.run([node, str(harness), str(cases)], capture_output=True, text=True, encoding="utf-8", timeout=30, check=True)

    assert json.loads(result.stdout) == [announcements.parse_markup(case) for case in MARKUP_CASES]


# --- Keeping the API responsive ---------------------------------------------------------------

def test_while_one_request_reads_the_others_get_the_last_list_at_once():
    import threading

    row = {**announcements.to_row(item()), "created_at": 1, "updated_at": 1, "created_by": "chris"}
    reading = threading.Event()
    release = threading.Event()
    calls = [0]

    def load():
        calls[0] += 1
        if calls[0] == 2:
            reading.set()
            release.wait(5)
        return [row]

    clock = [0.0]
    cache = announcements.Cache(load, seconds=15, clock=lambda: clock[0])
    cache.get()
    clock[0] = 100
    slow = threading.Thread(target=cache.get)
    slow.start()
    assert reading.wait(5)

    started = time.monotonic()
    assert [entry["id"] for entry in cache.get()] == ["play-takedown"]
    assert time.monotonic() - started < 1

    release.set()
    slow.join(5)


def test_a_database_down_from_the_start_is_not_asked_on_every_request():
    calls = [0]
    clock = [0.0]

    def load():
        calls[0] += 1
        raise ConnectionError("database down")

    cache = announcements.Cache(load, seconds=15, clock=lambda: clock[0])
    for _ in range(5):
        with pytest.raises(announcements.Unavailable):
            cache.get()
    assert calls[0] == 1

    clock[0] = 16
    with pytest.raises(announcements.Unavailable):
        cache.get()
    assert calls[0] == 2


def test_an_invalid_row_is_logged_once_not_on_every_read(caplog):
    good = {**announcements.to_row(item()), "created_at": 1, "updated_at": 1, "created_by": "chris"}
    bad = {**good, "id": "bad", "content": "not json"}
    clock = [0.0]
    cache = announcements.Cache(lambda: [good, bad, {"id": "worse"}], seconds=1, clock=lambda: clock[0])

    for step in range(3):
        clock[0] = step * 10
        assert [entry["id"] for entry in cache.get()] == ["play-takedown"]

    assert caplog.text.count("'bad' is not valid") == 1
    assert caplog.text.count("'worse' is not valid") == 1


# --- IDs made from titles --------------------------------------------------------------------

@pytest.mark.parametrize(
    "title, expected",
    [
        ("Zaktualizuj aplikację!", "zaktualizuj-aplikacje"),
        ("Łódź — żółć", "lodz-zolc"),
        ("  Update the app (v2)  ", "update-the-app-v2"),
        ("!!!", "announcement"),
        ("x" * 80, "x" * 56),
    ],
)
def test_an_id_is_made_from_the_title(title, expected):
    made = announcements.id_from_title(title)

    assert made == expected
    assert announcements.check_id(made) == made


def test_a_title_used_again_gets_a_numbered_id(api_context, capsys):
    client, _, billing = api_context

    for _ in range(3):
        assert admin(capsys, "announce", "add", "--title", "Update the app", "--body", "News", "--yes")[0] == 0
    admin(capsys, "announce", "end", "update-the-app-2", "--yes")
    assert admin(capsys, "announce", "add", "--title", "Update the app", "--body", "News", "--yes")[0] == 0

    ids = sorted(row["id"] for row in billing.announcements(ended=True))
    assert ids == ["update-the-app", "update-the-app-2", "update-the-app-3", "update-the-app-4"]


def test_replace_needs_an_id(api_context, capsys):
    code, output = admin(capsys, "announce", "add", "--title", "Hi", "--body", "News", "--replace", "--yes")

    assert code == 1 and "--replace needs the ID" in output
