"""The administration commands, run from the host:

    python scripts/admin.py status
    python scripts/admin.py jobs --failed --since 1d
    python scripts/admin.py user user@example.com credit 30 --reason "refund for a failed job"
    python scripts/admin.py --help

The same as `docker compose exec api python -m app.admin ...`, with two
differences. The audit log names you by your user name on this machine,
unless --by says otherwise. And without a terminal, as in a script or a
pipe, the container gets none either, so a command that would ask a
question refuses instead of waiting. See docs/administration.md.

Runs on the host with only the Python standard library.
"""

import getpass
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# How the commands tell you to run them again, for example to undo a change.
COMMAND = "python scripts/admin.py"
# What a user name keeps in the audit log; never an email address.
UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def actor() -> str:
    """Who is acting, for the audit log: the user name on this machine."""
    try:
        name = getpass.getuser()
    except Exception:
        name = ""
    return UNSAFE.sub("-", name).strip("-.")[:64] or "admin"


def command(arguments: list[str], terminal: bool, name: str) -> list[str]:
    no_terminal = [] if terminal else ["-T"]
    return [
        "docker", "compose", "exec", *no_terminal,
        "-e", f"ADMIN_ACTOR={name}",
        "-e", f"ADMIN_COMMAND={COMMAND}",
        "api", "python", "-m", "app.admin", *arguments,
    ]


def main(arguments: list[str], run=subprocess.run) -> int:
    terminal = sys.stdin.isatty() and sys.stdout.isatty()
    try:
        return run(command(arguments, terminal, actor()), cwd=ROOT).returncode
    except FileNotFoundError:
        print("docker was not found: install Docker, or run this where it is installed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
