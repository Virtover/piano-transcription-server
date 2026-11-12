"""`announce`: messages to the apps, shown by `GET /api/announcements`; see
app.announcements. Adding or replacing one asks first, since every user
sees it within a minute. Ending one keeps it, ended, so the list shows what
was said, and its ID can be used again with --replace."""

import time

from app import announcements
from app.admin.common import (
    AdminError,
    check_reason,
    finish,
    parse_duration,
    print_json,
    proceed,
    say,
    span,
    when,
)
from app.worker import tasks


# --- Data ---------------------------------------------------------------------

def listing(ended: bool = False, now: float | None = None) -> list[dict]:
    """The announcements, newest first, each with its state: scheduled,
    active, or ended. Without ended, those that have ended are left out."""
    now = time.time() if now is None else now
    rows = tasks.billing.announcements(ended=ended, now=now)
    found = []
    for row in rows:
        item = announcements.from_row(row)
        if item is None:
            found.append({"id": row["id"], "state": "invalid", "starts_at": row["starts_at"], "ends_at": row["ends_at"]})
        else:
            found.append({**item, "state": announcements.state(item, now)})
    return found


def find(announcement_id: str) -> dict | None:
    rows = tasks.billing.read("SELECT * FROM announcements WHERE id = ?", (announcement_id,))
    return announcements.from_row(rows[0]) if rows else None


def build(
    announcement_id: str,
    texts: dict,
    severity: str = "info",
    link_url: str | None = None,
    platforms: list[str] | None = None,
    min_version: int | None = None,
    max_version: int | None = None,
    starts_in: float | None = None,
    lasts: float | None = None,
    dismissible: bool | None = None,
    now: float | None = None,
) -> dict:
    """A checked announcement, or AdminError. starts_in and lasts are
    seconds: when it starts, from now, and how long it lasts from then."""
    now = int(time.time() if now is None else now)
    starts_at = now + int(starts_in or 0)
    try:
        return announcements.check({
            "id": announcement_id,
            "severity": severity,
            "texts": texts,
            "link_url": link_url,
            "platforms": platforms or [],
            "min_version": min_version,
            "max_version": max_version,
            "starts_at": starts_at,
            "ends_at": starts_at + int(lasts) if lasts else None,
            "dismissible": dismissible,
        })
    except announcements.InvalidAnnouncement as error:
        raise AdminError(str(error)) from None


def audience(item: dict) -> str:
    """Who sees it, in words."""
    parts = [", ".join(item["platforms"]) if item["platforms"] else "every platform"]
    low, high = item["min_version"], item["max_version"]
    if low is not None and high is not None:
        parts.append(f"versions {low} to {high}")
    elif low is not None:
        parts.append(f"versions {low} and later")
    elif high is not None:
        parts.append(f"versions {high} and earlier")
    else:
        parts.append("every version")
    return ", ".join(parts)


def window(item: dict, now: float | None = None) -> str:
    now = time.time() if now is None else now
    start = "now" if item["starts_at"] <= now else when(item["starts_at"])
    end = when(item["ends_at"]) if item["ends_at"] else "until ended"
    return f"from {start}, {'to ' + end if item['ends_at'] else end}"


def add_plan(item: dict, replace: bool) -> dict:
    """What adding item would do. Refuses, as adding would, if the ID is
    taken and replace is not given."""
    existing = find(item["id"])
    if existing is not None and not replace:
        raise AdminError(f"An announcement {item['id']} exists; add --replace to overwrite it, or choose another ID")
    english = item["texts"][announcements.DEFAULT_LANGUAGE]
    return {
        "id": item["id"],
        "replaces": existing is not None,
        # So a change meanwhile needs a new review on the page.
        "replaced": existing,
        "severity": item["severity"],
        "title": english["title"],
        "body": english["body"],
        # As apps lay it out, for the page to show what will be published.
        "blocks": announcements.parse_markup(english["body"]),
        "link": item["link_url"],
        "languages": sorted(item["texts"]),
        "audience": audience(item),
        "dismissible": item["dismissible"],
        "starts_at": item["starts_at"],
        "ends_at": item["ends_at"],
    }


def add(item: dict, replace: bool, actor: str, reason: str | None = None) -> dict:
    """Publish item, or replace the one with its ID when replace is given.
    The audit entry is written in the same transaction."""
    reason = check_reason(reason)
    try:
        # Again, as it is written: it may have waited in the page's confirmation.
        item = announcements.check(item)
    except announcements.InvalidAnnouncement as error:
        raise AdminError(str(error)) from None
    english = item["texts"][announcements.DEFAULT_LANGUAGE]
    detail = f"{item['severity']}, {audience(item)}: {english['title'][:80]}"
    try:
        replaced = tasks.billing.save_announcement(
            announcements.to_row(item), actor, detail, reason, replace, announcements.MAX_STORED
        )
    except ValueError as error:
        raise AdminError(str(error)) from None
    return {"id": item["id"], "replaced": replaced, "starts_at": item["starts_at"], "ends_at": item["ends_at"]}


def end_plan(announcement_id: str) -> dict:
    item = find(announcement_id) if _valid_id(announcement_id) else None
    if item is None:
        raise AdminError(f"No announcement {announcement_id}")
    return {
        "id": item["id"],
        "state": announcements.state(item, time.time()),
        "title": item["texts"][announcements.DEFAULT_LANGUAGE]["title"],
        "severity": item["severity"],
    }


def end(announcement_id: str, actor: str, reason: str | None = None) -> dict:
    reason = check_reason(reason)
    if not _valid_id(announcement_id) or find(announcement_id) is None:
        raise AdminError(f"No announcement {announcement_id}")
    return {"id": announcement_id, "changed": tasks.billing.end_announcement(announcement_id, actor, reason)}


def _valid_id(value: str) -> bool:
    try:
        announcements.check_id(value)
        return True
    except announcements.InvalidAnnouncement:
        return False


# --- Command line -------------------------------------------------------------

def _line_breaks(text: str | None) -> str | None:
    """A shell argument cannot easily hold a line break: \\n stands for one."""
    return text.replace("\\n", "\n") if text is not None else None


def texts_from(args) -> dict:
    texts = {announcements.DEFAULT_LANGUAGE: {
        "title": args.title, "body": _line_breaks(args.body), "link_label": args.link_label,
    }}
    for translation in args.translation or []:
        if len(translation) not in (3, 4):
            raise AdminError("--translation takes a language, a title, a body, and optionally a link label")
        language = translation[0].lower()
        if language in texts:
            raise AdminError(f"The language {language} is given twice")
        texts[language] = {
            "title": translation[1],
            "body": _line_breaks(translation[2]),
            "link_label": translation[3] if len(translation) == 4 else None,
        }
    return texts


def describe(item: dict, now: float | None = None) -> list[str]:
    english = item["texts"][announcements.DEFAULT_LANGUAGE]
    lines = [
        f"{item['id']} ({item['severity']}{'' if item['dismissible'] else ', cannot be dismissed'})",
        f"  For:       {audience(item)}",
        f"  When:      {window(item, now)}",
        f"  Languages: {', '.join(sorted(item['texts']))}",
        f"  Title:     {english['title']}",
    ]
    lines += [f"  {'Body:' if index == 0 else '':<10} {line}" for index, line in enumerate(english["body"].split("\n"))]
    if item["link_url"]:
        lines.append(f"  Link:      {item['link_url']}{' (' + english['link_label'] + ')' if english['link_label'] else ''}")
    return lines


def run_list(args) -> None:
    found = listing(ended=args.all)
    if args.json:
        print_json(found)
        return
    if not found:
        print("No announcements" + ("" if args.all else "; --all also lists ended ones"))
        return
    now = time.time()
    for item in found:
        if item["state"] == "invalid":
            print(f"{item['id']} [invalid: not shown; see the API's log, and replace or end it]")
            continue
        lines = describe(item, now)
        lines[0] = f"{lines[0]} [{item['state']}]"
        print("\n".join(lines))
        print()


def run_add(args) -> None:
    starts_in = parse_duration(args.start, "--start") if args.start else None
    lasts = parse_duration(args.for_, "--for")
    item = build(
        args.id,
        texts_from(args),
        args.severity,
        args.link,
        args.platform,
        args.min_version,
        args.max_version,
        starts_in,
        lasts,
        args.dismissible,
    )
    plan = add_plan(item, args.replace)
    if not args.json:
        print(("Replaces the announcement with this ID. " if plan["replaces"] else "") + "Apps will show, within a minute:")
        print("\n".join(describe(item)))
    if not proceed(args, plan, question="Publish it?", would="It will be published"):
        return
    result = add(item, args.replace, args.by, args.reason)
    lasting = f" for {span(lasts)}" if lasts else " until ended"
    finish(args, result, f"Published {item['id']}{lasting}", ["announce", "end", item["id"]])


def run_end(args) -> None:
    plan = end_plan(args.id)
    if plan["state"] == "ended":
        say(args, f"{plan['id']} has already ended")
        if args.json:
            print_json({"dry_run": args.dry_run, "result": {"id": plan["id"], "changed": False}})
        return
    say(args, f"{plan['id']} ({plan['severity']}, {plan['state']}): {plan['title']}")
    if not proceed(args, plan, question="End it now?", would="It will no longer be shown"):
        return
    result = end(args.id, args.by, args.reason)
    finish(args, result, f"{args.id} has ended; apps stop showing it within a minute")
