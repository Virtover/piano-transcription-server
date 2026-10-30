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
import importlib.util
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# How the commands tell you to run them again, for example to undo a change.
COMMAND = "python scripts/admin.py"
# What a user name keeps in the audit log; never an email address.
UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def load_script(name: str):
    spec = importlib.util.spec_from_file_location(f"{name}_script", Path(__file__).with_name(f"{name}.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


STACKS = load_script("stacks")


def actor() -> str:
    """Who is acting, for the audit log: the user name on this machine."""
    try:
        name = getpass.getuser()
    except Exception:
        name = ""
    return UNSAFE.sub("-", name).strip("-.")[:64] or "admin"


def main(arguments: list[str], stack=None, command: str = COMMAND) -> int:
    """Run app.admin in the API service. command is how its messages tell
    you to run it again."""
    stack = stack or STACKS.ComposeStack([], root=ROOT)
    terminal = sys.stdin.isatty() and sys.stdout.isatty()
    try:
        return stack.exec("api", "python", "-m", "app.admin", *arguments, capture=False, terminal=terminal,
                          env={"ADMIN_ACTOR": actor(), "ADMIN_COMMAND": command}).returncode
    except FileNotFoundError:
        print(f"{stack.cli} was not found: install it, or run this where it is installed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
