"""`audit`: the log of admin actions, newest first."""

from app.admin.common import print_json, shorten, table, when
from app.worker import tasks


def entries(limit: int = 30) -> list[dict]:
    return [dict(row) for row in tasks.billing.read("SELECT * FROM admin_actions ORDER BY id DESC LIMIT ?", (limit,))]


def run(args) -> None:
    rows = entries(args.limit)
    if args.json:
        print_json(rows)
        return
    if not rows:
        print("No admin actions yet")
        return
    print(
        table(
            ["When", "By", "Action", "Target", "Detail", "Reason"],
            [
                [when(row["at"]), row["actor"], row["action"], row["target"], row["detail"], shorten(row["reason"], 60)]
                for row in rows
            ],
        )
    )
