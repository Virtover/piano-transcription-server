"""Planned restarts and shutdowns, announced to clients first.

    python scripts/server.py restart 5m                  # announce, build, wait, drain, restart
    python scripts/server.py stop 10m --back-in 2h       # announce, wait, drain, stop everything
    python scripts/server.py start                       # start again and wait until ready
    python scripts/server.py status
    python scripts/server.py cancel                      # also stops a waiting script

Runs on the host with only the Python standard library. Clients see the
notice in GET /api/server-info, in job status, and in the X-Server-Maintenance
headers. For a restart, the new images are built while the countdown runs and
the current version keeps serving; if the build fails, the restart is
cancelled. At the planned time, running jobs get up to --grace to finish.
Queued jobs survive both a restart and a shutdown.
"""

import argparse
import json
import shlex
import subprocess
import sys
import time

UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
DEFAULT_GRACE = "5m"
READY_TIMEOUT = 180
STATUS_INTERVAL = 10
DRAIN_INTERVAL = 5
# A shutdown stops every service, whichever profiles started it.
ALL_PROFILES = ["*"]
OUTSOURCE_NOTE = "--outsource is no longer needed: OUTSOURCE_PROVIDER in .env turns outsourcing on or off"


def parse_duration(text: str) -> float:
    text = text.strip().lower()
    try:
        value = float(text[:-1]) * UNITS[text[-1]]
    except (KeyError, ValueError, IndexError):
        raise argparse.ArgumentTypeError(f"{text!r} is not a duration such as 30s, 5m, or 1h") from None
    if value < 0:
        raise argparse.ArgumentTypeError("durations cannot be negative")
    return value


def remaining_text(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    minutes, seconds = divmod(seconds, 60)
    return f"{minutes} m {seconds} s" if minutes else f"{seconds} s"


class Stack:
    """The Docker Compose stack, reached through the docker CLI."""

    def __init__(self, profiles: list[str], run=subprocess.run, popen=subprocess.Popen):
        self.profiles = profiles
        self._run = run
        self._popen = popen

    def compose(self, profiles: list[str] | None = None) -> list[str]:
        selected = self.profiles if profiles is None else profiles
        return ["docker", "compose"] + [part for name in selected for part in ("--profile", name)]

    def maintenance(self, *arguments: str) -> str:
        result = self._run(
            self.compose() + ["exec", "-T", "api", "python", "-m", "app.maintenance", *arguments],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError((result.stderr or result.stdout).strip() or "the api service is not running")
        return result.stdout.strip()

    def ready(self) -> bool:
        probe = "import urllib.request; urllib.request.urlopen('http://localhost:8000/ready', timeout=5)"
        result = self._run(self.compose() + ["exec", "-T", "api", "python", "-c", probe], capture_output=True, text=True)
        return result.returncode == 0

    def start_build(self):
        return self._popen(self.compose() + ["build"])

    def up(self, command: str | None = None, build: bool = True) -> int:
        if command:
            return self._run(shlex.split(command, posix=(sys.platform != "win32"))).returncode
        return self._run(self.compose() + ["up", "-d"] + (["--build"] if build else [])).returncode

    def stop(self, down: bool) -> int:
        return self._run(self.compose(ALL_PROFILES) + (["down"] if down else ["stop"])).returncode


class Clock:
    def now(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


def say(message: str) -> None:
    print(f"[server] {message}", flush=True)


def announce(stack: Stack, action: str, args) -> dict:
    command = ["schedule", action, f"{args.delay:g}s", "--grace", f"{args.grace:g}s"]
    if args.reason:
        command += ["--reason", args.reason]
    if getattr(args, "back_in", None):
        command += ["--back-in", f"{args.back_in:g}s"]
    notice = json.loads(stack.maintenance(*command))
    say(f"announced to clients: {notice['message']}")
    return notice


def wait_until(stack: Stack, clock: Clock, deadline: float, build=None) -> str:
    """Wait for the planned time and the build, if any. Returns "due",
    "build-failed", or "cancelled"."""
    next_report = clock.now()
    next_status = clock.now() + STATUS_INTERVAL
    while True:
        now = clock.now()
        building = build is not None and build.poll() is None
        if build is not None and not building and build.returncode != 0:
            return "build-failed"
        if now >= deadline and not building:
            return "due"
        if now >= next_status:
            next_status = now + STATUS_INTERVAL
            try:
                if json.loads(stack.maintenance("status") or "null") is None:
                    return "cancelled"
            except RuntimeError:
                # The API is briefly unreachable; the plan goes ahead.
                pass
        if now >= next_report:
            left = deadline - now
            state = ", building" if building else ""
            say(f"{remaining_text(left)} left{state}" if left > 0 else "waiting for the build to finish")
            next_report = now + (60 if left > 120 else 10)
        clock.sleep(1)


def drain(stack: Stack, clock: Clock, grace: float) -> None:
    deadline = clock.now() + grace
    while True:
        running = int(stack.maintenance("active-jobs") or 0)
        if running == 0:
            say("no running jobs")
            return
        if clock.now() >= deadline:
            say(f"grace period over: {running} running job(s) will be interrupted and refunded")
            return
        say(f"waiting for {running} running job(s), up to {remaining_text(deadline - clock.now())}")
        clock.sleep(DRAIN_INTERVAL)


def wait_ready(stack: Stack, clock: Clock) -> bool:
    deadline = clock.now() + READY_TIMEOUT
    while not stack.ready():
        if clock.now() >= deadline:
            return False
        clock.sleep(2)
    return True


def countdown(stack: Stack, clock: Clock, action: str, args, build_images: bool):
    """Announce, then wait for the planned time. Returns the exit code if the
    plan ended early, or None when it is due."""
    try:
        notice = announce(stack, action, args)
    except RuntimeError as error:
        say(f"could not announce the {action}: {error}")
        return 1
    deadline = clock.now() + notice["seconds_remaining"]
    build = None
    if build_images:
        say("building the new images while the current version keeps serving")
        build = stack.start_build()
    try:
        outcome = wait_until(stack, clock, deadline, build)
    except KeyboardInterrupt:
        if build is not None and build.poll() is None:
            build.terminate()
        stack.maintenance("cancel")
        say(f"interrupted: {action} cancelled, notice removed")
        return 130
    if outcome == "build-failed":
        stack.maintenance("cancel")
        say("the build failed: restart cancelled, the current version keeps running")
        return 1
    if outcome == "cancelled":
        if build is not None and build.poll() is None:
            build.terminate()
        say(f"the {action} was cancelled")
        return 0
    return None


def restart(args, stack: Stack, clock: Clock) -> int:
    if args.command:
        say("custom --command: nothing is built before the countdown ends")
    code = countdown(stack, clock, "restart", args, build_images=not (args.command or args.no_build))
    if code is not None:
        return code
    drain(stack, clock, args.grace)
    say("restarting")
    code = stack.up(args.command)
    if code != 0:
        say(f"the restart command failed with exit code {code}")
        try:
            stack.maintenance("cancel")
        except RuntimeError:
            say("could not remove the notice; it expires on its own")
        return code
    if not wait_ready(stack, clock):
        say(f"the API is not ready after {READY_TIMEOUT} s; check `docker compose logs api`. "
            "The notice expires on its own.")
        return 1
    stack.maintenance("cancel")
    say("done: the API is ready and the notice is removed")
    return 0


def stop(args, stack: Stack, clock: Clock) -> int:
    code = countdown(stack, clock, "shutdown", args, build_images=False)
    if code is not None:
        return code
    drain(stack, clock, args.grace)
    say("stopping every service" + (" and removing the containers" if args.down else ""))
    code = stack.stop(args.down)
    if code != 0:
        say(f"docker compose failed with exit code {code}")
        return code
    # The notice stays in Redis; the API removes it when it starts again.
    say("stopped. Start again with: python scripts/server.py start")
    return 0


def start(args, stack: Stack, clock: Clock) -> int:
    say("starting")
    code = stack.up(build=False)
    if code != 0:
        say(f"docker compose failed with exit code {code}")
        return code
    if not wait_ready(stack, clock):
        say(f"the API is not ready after {READY_TIMEOUT} s; check `docker compose logs api`")
        return 1
    say("done: the API is ready")
    return 0


def parse_arguments(arguments: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python scripts/server.py",
        description="Planned restarts and shutdowns, announced to clients first.",
    )
    commands = parser.add_subparsers(dest="action", required=True)

    def profiles(subparser):
        subparser.add_argument("--profile", action="append", default=[], help="Compose profile; repeatable")
        # Accepted so older commands keep working; the remote worker always runs now.
        subparser.add_argument("--outsource", action="store_true", help=argparse.SUPPRESS)

    def timing(subparser, noun):
        subparser.add_argument("delay", type=parse_duration, help=f"time until the {noun}, for example 5m or 30s")
        subparser.add_argument("--grace", type=parse_duration, default=parse_duration(DEFAULT_GRACE),
                               help=f"how long running jobs may take to finish (default {DEFAULT_GRACE})")
        subparser.add_argument("--reason", help="optional text shown to clients")

    restart_parser = commands.add_parser("restart", help="restart, for example to update")
    timing(restart_parser, "restart")
    profiles(restart_parser)
    restart_parser.add_argument("--command", help='restart command instead of "docker compose up -d --build"')
    restart_parser.add_argument("--no-build", action="store_true", help="do not build before the countdown ends")

    stop_parser = commands.add_parser("stop", help="shut the server down")
    timing(stop_parser, "shutdown")
    stop_parser.add_argument("--back-in", type=parse_duration, help="when the server is expected back, shown to clients")
    stop_parser.add_argument("--down", action="store_true", help="also remove the containers; volumes and data are kept")

    start_parser = commands.add_parser("start", help="start the server again")
    profiles(start_parser)

    commands.add_parser("status", help="print the pending restart or shutdown")
    commands.add_parser("cancel", help="cancel the pending restart or shutdown")

    args = parser.parse_args(arguments)
    args.profile = list(getattr(args, "profile", []))
    return args


def main(arguments: list[str], stack: Stack | None = None, clock: Clock | None = None) -> int:
    args = parse_arguments(arguments)
    if getattr(args, "outsource", False):
        say(OUTSOURCE_NOTE)
    stack = stack or Stack(args.profile)
    clock = clock or Clock()
    try:
        if args.action == "status":
            notice = json.loads(stack.maintenance("status") or "null")
            say(f"{notice['message']} (at {notice['at']})" if notice else "nothing is scheduled")
            return 0
        if args.action == "cancel":
            say(stack.maintenance("cancel"))
            return 0
    except RuntimeError as error:
        say(str(error))
        return 1
    return {"restart": restart, "stop": stop, "start": start}[args.action](args, stack, clock)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
