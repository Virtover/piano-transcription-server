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

A home cluster's hub (docs/cluster.md) has three commands of its own, run
here on the host, because they write files in secrets/:

    python scripts/admin.py nodes hub [--registry hub.example.ts.net]
    python scripts/admin.py nodes invite laptop [--tasks transcribe,arrange] [--again]
    python scripts/admin.py nodes revoke laptop

Runs on the host with only the Python standard library.
"""

import argparse
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
NODES = load_script("nodes")
# The commands that write the hub's files, and so run here.
HOST_NODE_COMMANDS = ("hub", "invite", "revoke")


def actor() -> str:
    """Who is acting, for the audit log: the user name on this machine."""
    try:
        name = getpass.getuser()
    except Exception:
        name = ""
    return UNSAFE.sub("-", name).strip("-.")[:64] or "admin"


def say(message: str) -> None:
    print(f"[nodes] {message}", flush=True)


def in_container(stack, *arguments: str) -> int:
    return stack.exec("api", "python", "-m", "app.admin", *arguments, capture=False, terminal=False,
                      env={"ADMIN_ACTOR": actor(), "ADMIN_COMMAND": COMMAND}).returncode


def node_command(arguments: list[str], stack) -> int:
    parser = argparse.ArgumentParser(prog=f"{COMMAND} nodes")
    commands = parser.add_subparsers(dest="action", required=True)
    hub = commands.add_parser("hub", help="make this machine a home cluster's hub")
    hub.add_argument("address", nargs="?", help="this machine's address; found from Tailscale when left out")
    hub.add_argument("--lan", action="store_true",
                     help="publish on this local network address instead of through Tailscale, as for a test")
    hub.add_argument("--registry", metavar="NAME", help="this machine's Tailscale name, for a registry of nodes' images")
    invite = commands.add_parser("invite", help="write an invite file for a new node")
    invite.add_argument("node", help="the device's name, such as laptop")
    # Not dispatch by default: it copies the RunPod key to the device, and the
    # hub already sends jobs to RunPod.
    invite.add_argument("--tasks", default="transcribe,arrange,check",
                        help="what it may run, from transcribe, arrange, dispatch (sending jobs to RunPod, with the "
                             "RunPod key), and check; or all. Default: transcribe,arrange,check")
    invite.add_argument("--again", action="store_true", help="a new invite for a node: new password and keys")
    revoke = commands.add_parser("revoke", help="cut a node off: its password and keys stop working at once")
    revoke.add_argument("node")
    args = parser.parse_args(arguments)
    root = stack.root
    try:
        if args.action == "hub":
            server = load_script("server")
            for message in NODES.setup_hub(root, server, args.address, args.registry, lan=args.lan):
                say(message)
            return 0
        if args.action == "invite":
            tasks = NODES.check_tasks(args.tasks)
            name = NODES.check_name(args.node)
            path, todo = NODES.invite(root, name, tasks, args.again)
            return finish_invite(stack, name, tasks, path, todo)
        return finish_revoke(stack, NODES.check_name(args.node), NODES.revoke(root, args.node))
    except NODES.ClusterError as error:
        say(str(error))
        return 1


def apply_users(stack, storage_changed: bool) -> bool:
    """Redis reads its users again; the bucket its keys, when they changed."""
    problem = stack.reload_redis_users()
    if problem:
        say(f"Redis did not read its users again ({problem}); is the hub running? Then: "
            f"{stack.restart_command('redis')}")
        return False
    if storage_changed and stack.restart_services(["storage"]) != 0:
        say(f"could not restart the bucket for its new keys: {stack.restart_command('storage')}")
        return False
    return True


def finish_invite(stack, name: str, tasks: list[str], path: Path, todo: dict) -> int:
    if not apply_users(stack, todo["storage_changed"]):
        return 1
    if in_container(stack, "nodes", "register", name, *tasks, "--reason", "invited") != 0:
        say("the node's password is ready, but the hub could not register it; is the API running?")
        return 1
    say(f"wrote {path.relative_to(stack.root)}: copy it to the device by a private way, such as `tailscale file cp`, "
        "never by email or chat. It holds the node's password and keys")
    say(f"then on the device, in its copy of this repository: python scripts/server.py join {path.name}")
    if todo["shared_keys"]:
        say(f"the invite holds the bucket's own keys ({', '.join(todo['shared_keys'])}), which every node shares: "
            "revoking a node cannot take them back; give your bucket provider's keys a limit to that bucket")
    return 0


def finish_revoke(stack, name: str, result: dict) -> int:
    ok = apply_users(stack, result["storage_changed"])
    try:
        # Its open connections, which a removed user keeps until they close.
        stack.redis("client", "kill", "user", f"node-{name}")
    except RuntimeError:
        pass
    code = in_container(stack, "nodes", "forget", name, "--reason", "revoked")
    say(f"{name} is cut off" + (f"; removed {', '.join(result['removed'])}" if result["removed"] else ""))
    say("its running jobs are queued again elsewhere within a minute")
    if result["cloud_keys"]:
        say("nodes may have had your STORAGE_ bucket's keys (JOB_STORAGE=s3, or RunPod): replace them at the "
            "provider if the device may be in the wrong hands")
    return 0 if ok and code == 0 else 1


def main(arguments: list[str], stack=None, command: str = COMMAND) -> int:
    """Run app.admin in the API service. command is how its messages tell
    you to run it again."""
    stack = stack or STACKS.ComposeStack([], root=ROOT)
    if arguments[:1] == ["nodes"] and arguments[1:2] and arguments[1] in HOST_NODE_COMMANDS:
        if not isinstance(stack, STACKS.ComposeStack):
            print("nodes hub, invite, and revoke are for a home cluster's Compose hub", file=sys.stderr)
            return 1
        return node_command(arguments[1:], stack)
    terminal = sys.stdin.isatty() and sys.stdout.isatty()
    try:
        return stack.exec("api", "python", "-m", "app.admin", *arguments, capture=False, terminal=terminal,
                          env={"ADMIN_ACTOR": actor(), "ADMIN_COMMAND": command}).returncode
    except FileNotFoundError:
        print(f"{stack.cli} was not found: install it, or run this where it is installed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
