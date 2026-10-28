"""Planned restarts and shutdowns, announced to clients first.

    python scripts/server.py restart 5m                  # announce, build, wait, drain, restart
    python scripts/server.py stop 10m --back-in 2h       # announce, wait, drain, stop everything
    python scripts/server.py start                       # build, prepare, start, wait until ready
    python scripts/server.py start --legacy-gpu          # worker for older GPUs such as the GTX 1050 Ti
    python scripts/server.py status
    python scripts/server.py cancel                      # also stops a waiting script
    python scripts/server.py restore [latest|NAME|N]     # billing database from a backup; --from local, --fallback

Runs on the host with only the Python standard library. On a new
installation, start creates .env from .env.example, a random PostgreSQL
password in secrets/postgres_password, and random Redis passwords in
secrets/redis; passwords are never printed. start, restart, and restore also
create a missing one, for example after an update. Clients see the
notice in GET /api/server-info, in job status, and in the X-Server-Maintenance
headers. For a restart, the new images are built while the countdown runs and
the current version keeps serving; if the build fails, the restart is
cancelled. --legacy-gpu on start, restart, and restore builds the worker for
older NVIDIA GPUs, such as the GTX 10 series, for that run;
WORKER_GPU_BUILD=legacy in .env makes it permanent. At the planned time, running jobs get up to --grace to finish.
Queued jobs survive both a restart and a shutdown.

restore takes an off-site backup by default, or one from billing-data/backups
with --from local; --fallback tries the other place when the first has no
such backup or cannot be reached. For an off-site backup it asks for the
private key without showing it, or reads it from standard input when it is
piped, and fetches and checks the backup first. Then it stops the API and
the workers, takes a safety backup of the current database, restores in one
transaction, and starts again.
"""

import argparse
import getpass
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
DEFAULT_GRACE = "5m"
READY_TIMEOUT = 180
AOF_TIMEOUT = 60
STATUS_INTERVAL = 10
DRAIN_INTERVAL = 5
# A shutdown stops every service, whichever profiles started it.
ALL_PROFILES = ["*"]
# docker-compose.yml passes it to Dockerfile.worker as GPU_BUILD.
GPU_BUILD_VARIABLE = "WORKER_GPU_BUILD"
OUTSOURCE_NOTE = "--outsource is no longer needed: OUTSOURCE_PROVIDER in .env turns outsourcing on or off"
# Everything that uses the billing database; PostgreSQL keeps running.
BILLING_SERVICES = ["api", "worker", "remote-worker", "cleanup"]
# The last line of `python -m app.backups fetch`.
FETCHED = re.compile(r"saved and checked: (/billing/\S+)")
# app.backups.BACKUP_FILE_PATTERN: the automatic backups in billing-data/backups.
LOCAL_BACKUP = re.compile(r"^billing-(\d+)\.(?:sqlite3|dump)$")
# The Redis users with a password in secrets/redis; see redis/users.acl.
REDIS_USERS = ["operator", "app", "admin-read-only", "admin-read-write"]
# The admin web page's PostgreSQL roles; the API creates them from these.
ADMIN_WEB_DATABASE_PASSWORDS = ["postgres-read-only", "postgres-read-write"]
# redis-cli in the redis container, signed in as operator. A container from
# before Redis had users has no password file and needs none. The password
# goes through the environment, not the command line.
REDIS_CLI = (
    'if [ -f /run/secrets/redis/operator ]; then '
    'export REDISCLI_AUTH="$(cat /run/secrets/redis/operator)"; set -- --user operator "$@"; fi; '
    'exec redis-cli "$@"'
)
# redis-cli exits with 0 when Redis refuses a command; its answer says so.
REDIS_REFUSALS = ("NOAUTH", "WRONGPASS", "NOPERM", "AUTH failed", "ERR ")
PG_RESTORE = ["pg_restore", "-U", "billing", "-d", "billing", "--clean", "--if-exists", "--no-owner",
              "--single-transaction", "--exit-on-error"]


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


def prepare_files(root: Path) -> list[str]:
    """Create the files Compose needs before anything starts, and say what
    was created. Existing files are never changed."""
    created = []
    env = root / ".env"
    if not env.exists():
        shutil.copyfile(root / ".env.example", env)
        created.append("created .env from .env.example; change what you need, then restart")
    if create_password(root, "secrets/postgres_password"):
        created.append("created a random PostgreSQL password in secrets/postgres_password")
    new_users = [name for name in REDIS_USERS if create_password(root, f"secrets/redis/{name}")]
    if new_users:
        created.append(f"created random Redis passwords in secrets/redis: {', '.join(new_users)}")
    new_roles = [name for name in ADMIN_WEB_DATABASE_PASSWORDS if create_password(root, f"secrets/admin-web/{name}")]
    if new_roles:
        created.append(f"created random admin web page passwords in secrets/admin-web: {', '.join(new_roles)}")
    # Created here rather than by Docker, which would make it root's. The
    # prepare service gives it to the services and keeps its group.
    (root / "billing-data" / "backups").mkdir(parents=True, exist_ok=True)
    return created


def create_password(root: Path, relative: str) -> bool:
    """Create a random password file at root/relative unless it exists.
    Returns whether it was created; it is never printed."""
    password = root / relative
    # Docker creates a missing bind-mounted file as an empty folder, owned by
    # root on Linux.
    root_owned = f"it was probably created by Docker as root. Run: sudo rm -r {relative} " \
                 "&& sudo chown -R \"$(id -u):$(id -g)\" secrets, then start again"
    if password.is_dir():
        try:
            password.rmdir()
        except PermissionError:
            raise RuntimeError(f"{relative} is a folder, not a file: {root_owned}") from None
        except OSError:
            raise RuntimeError(f"{password} is a folder, not a file: move it away and start again") from None
    if password.exists():
        return False
    for folder in reversed(password.parents):
        if folder.is_relative_to(root) and folder != root and not folder.exists():
            folder.mkdir()
            # PostgreSQL, Redis, and the services read the secrets as other users.
            folder.chmod(0o755)
    try:
        # Letters and digits only, so it needs no quoting anywhere.
        password.write_text(secrets.token_hex(24), encoding="utf-8")
    except PermissionError:
        raise RuntimeError(f"cannot write {relative}: {root_owned}") from None
    # Readable whatever the umask: PostgreSQL, Redis, and the services run as
    # other users and read it through a bind mount.
    password.chmod(0o644)
    return True


def share_billing_data(environment) -> None:
    """Let the prepare service make billing-data writable by this user's
    group, so backups can be copied, moved, or deleted without sudo."""
    if hasattr(os, "getgid") and os.getgid() != 0:
        environment.setdefault("BILLING_DATA_GROUP", str(os.getgid()))


def select_gpu_build(args, environment) -> None:
    """Build the worker for older GPUs when asked. Set in the environment,
    it overrides .env for every docker compose command of this run."""
    if getattr(args, "legacy_gpu", False):
        environment[GPU_BUILD_VARIABLE] = "legacy"
        say("building the worker for older GPUs (GTX 10 series and similar)")


class Stack:
    """The Docker Compose stack, reached through the docker CLI."""

    def __init__(self, profiles: list[str], run=subprocess.run, popen=subprocess.Popen, root: Path = ROOT):
        self.profiles = profiles
        self._run = run
        self._popen = popen
        self.root = root

    def prepare_files(self) -> list[str]:
        return prepare_files(self.root)

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

    def redis(self, *arguments: str) -> str:
        result = self._run(self.compose() + ["exec", "-T", "redis", "sh", "-c", REDIS_CLI, "redis-cli", *arguments],
                           capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError((result.stderr or result.stdout).strip() or "the redis service is not running")
        answer = result.stdout.strip()
        if answer.startswith(REDIS_REFUSALS):
            raise RuntimeError(f"Redis refused: {answer.splitlines()[0]}")
        return answer

    def start_build(self):
        return self._popen(self.compose() + ["build"])

    def up(self, command: str | None = None, build: bool = True) -> int:
        if command:
            return self._run(shlex.split(command, posix=(sys.platform != "win32"))).returncode
        return self._run(self.compose() + ["up", "-d"] + (["--build"] if build else [])).returncode

    def stop(self, down: bool) -> int:
        return self._run(self.compose(ALL_PROFILES) + (["down"] if down else ["stop"])).returncode

    def fetch_backup(self, which: str, key: str) -> tuple[int, str]:
        # The key goes on standard input: arguments and the environment are
        # visible to other processes.
        result = self._run(
            self.compose() + ["run", "--rm", "--no-deps", "-T", "api", "python", "-m", "app.backups", "fetch", which],
            input=key, capture_output=True, text=True,
        )
        return result.returncode, "\n".join(part.strip() for part in (result.stdout, result.stderr) if part.strip())

    def host_path(self, container_path: str) -> Path:
        # billing-data is mounted at /billing in docker-compose.yml.
        return self.root / "billing-data" / Path(container_path).relative_to("/billing")

    def start_postgres(self) -> int:
        return self._run(self.compose() + ["up", "-d", "--wait", "postgres"]).returncode

    def stop_services(self, services: list[str]) -> int:
        return self._run(self.compose() + ["stop", *services]).returncode

    def backup_now(self) -> int:
        return self._run(self.compose() + ["run", "--rm", "--no-deps", "api", "python", "-m", "app.backups", "now"]
                         ).returncode

    def restore_dump(self, dump: Path) -> int:
        # pg_restore reads a custom-format dump from standard input, so
        # nothing is copied into the container.
        with dump.open("rb") as stream:
            return self._run(self.compose() + ["exec", "-T", "postgres", *PG_RESTORE], stdin=stream).returncode


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


def persist_redis(stack: Stack, clock: Clock) -> None:
    """Turn on Redis's append-only file in the running Redis and wait until
    it is written. Redis started with it on over a volume that only has a
    snapshot starts empty, so this keeps queued jobs through the first
    restart with the setting. Nothing changes when it is already on."""
    try:
        stack.redis("config", "set", "appendonly", "yes")
        deadline = clock.now() + AOF_TIMEOUT
        while True:
            info = stack.redis("info", "persistence")
            if "aof_rewrite_in_progress:0" in info and "aof_rewrite_scheduled:0" in info:
                return
            if clock.now() >= deadline:
                say(f"Redis did not finish writing its append-only file in {AOF_TIMEOUT} s; queued jobs may be lost")
                return
            clock.sleep(1)
    except RuntimeError as error:
        say(f"could not turn on Redis persistence ({error}); queued jobs may be lost")


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


def prepare(stack: Stack) -> bool:
    """Create missing files, such as a password added by an update, before
    anything is announced or started. False, having said why, if one cannot
    be created."""
    try:
        for message in stack.prepare_files():
            say(message)
    except (OSError, RuntimeError) as error:
        say(str(error))
        return False
    return True


def restart(args, stack: Stack, clock: Clock) -> int:
    if not prepare(stack):
        return 1
    if args.command:
        say("custom --command: nothing is built before the countdown ends")
    code = countdown(stack, clock, "restart", args, build_images=not (args.command or args.no_build))
    if code is not None:
        return code
    drain(stack, clock, args.grace)
    persist_redis(stack, clock)
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
    persist_redis(stack, clock)
    say("stopping every service" + (" and removing the containers" if args.down else ""))
    code = stack.stop(args.down)
    if code != 0:
        say(f"docker compose failed with exit code {code}")
        return code
    # The notice stays in Redis; the API removes it when it starts again.
    say("stopped. Start again with: python scripts/server.py start")
    return 0


def start(args, stack: Stack, clock: Clock) -> int:
    if not prepare(stack):
        return 1
    say("starting")
    # Built every time, so a start after an update never runs old code; an
    # unchanged image is a quick cache hit.
    code = stack.up()
    if code != 0:
        say(f"docker compose failed with exit code {code}")
        return code
    if not wait_ready(stack, clock):
        say(f"the API is not ready after {READY_TIMEOUT} s; check `docker compose logs api`")
        return 1
    say("done: the API is ready")
    return 0


def read_key() -> str:
    """The private key: typed without being shown, or piped in."""
    if sys.stdin.isatty():
        return getpass.getpass("Private key of the off-site backups (AGE-SECRET-KEY-..., not shown): ")
    return sys.stdin.read()


def confirm(question: str) -> bool:
    return input(question).strip().lower() in ("y", "yes")


def remote_backup(which: str, stack: Stack) -> tuple[Path | None, str]:
    """Download, decrypt, and check an off-site backup. Returns the file on
    the host, or why there is none."""
    key = read_key()
    if "AGE-SECRET-KEY-" not in key:
        return None, "no private key given (AGE-SECRET-KEY-...)"
    say(f"fetching and checking off-site backup {which}")
    code, output = stack.fetch_backup(which, key)
    del key
    if output:
        print(output, flush=True)
    fetched = FETCHED.search(output)
    if code != 0 or not fetched:
        return None, "fetching the off-site backup failed"
    return stack.host_path(fetched.group(1)), ""


def local_backup(which: str, stack: Stack) -> tuple[Path | None, str]:
    """A backup in billing-data/backups: latest, a name, or a number counted
    from the newest, as `python -m app.backups list` shows them."""
    folder = stack.root / "billing-data" / "backups"
    found = {}
    if folder.is_dir():
        for path in folder.iterdir():
            match = LOCAL_BACKUP.match(path.name)
            if match and path.is_file():
                found[int(match.group(1))] = path
    newest_first = [found[timestamp] for timestamp in sorted(found, reverse=True)]
    if not newest_first:
        return None, "there are no local backups in billing-data/backups"
    if which == "latest":
        chosen = newest_first[0]
    elif which.isdigit():
        if not 1 <= int(which) <= len(newest_first):
            return None, f"there are {len(newest_first)} local backups: choose 1 (the newest) to {len(newest_first)}"
        chosen = newest_first[int(which) - 1]
    else:
        match = re.search(r"billing-(\d+)", which)
        chosen = found.get(int(match.group(1))) if match else None
        if chosen is None:
            return None, f"no local backup named {which}"
    if chosen.suffix != ".dump":
        return None, f"{chosen.name} is a legacy SQLite backup; PostgreSQL cannot restore it"
    say(f"using local backup {chosen.name}")
    return chosen, ""


BACKUP_SOURCES = {"remote": remote_backup, "local": local_backup}


def restore(args, stack: Stack, clock: Clock) -> int:
    sources = [args.source]
    if args.fallback:
        sources += [source for source in BACKUP_SOURCES if source != args.source]
    dump = None
    for position, source in enumerate(sources):
        dump, problem = BACKUP_SOURCES[source](args.backup, stack)
        if dump is not None:
            break
        if position + 1 < len(sources):
            # A number or "latest" picks from the other place's own list.
            say(f"{problem}; trying the {sources[position + 1]} backups")
        else:
            say(f"{problem}: nothing was changed")
            return 1
    if not args.yes:
        if not sys.stdin.isatty():
            say("standard input is not a terminal, so nothing can be asked: add --yes. Nothing was changed")
            return 1
        if not confirm(f"Replace the live billing database with {dump.name} ({source})? "
                       "Running jobs are interrupted. [y/N] "):
            say("cancelled: nothing was changed")
            return 0
    if stack.start_postgres() != 0:
        say("PostgreSQL did not start: nothing was changed")
        return 1
    say("stopping the API and the workers")
    if stack.stop_services(BILLING_SERVICES) != 0:
        say("could not stop the services: nothing was changed")
        return 1
    say("taking a safety backup of the current database")
    if stack.backup_now() != 0:
        say("the safety backup failed, so nothing was restored. Start again with: python scripts/server.py start")
        return 1
    say(f"restoring {dump.name}")
    code = stack.restore_dump(dump)
    if code != 0:
        # --single-transaction: a failed restore leaves the database as it was.
        say(f"pg_restore failed with exit code {code}; the database is unchanged")
    else:
        say("restored; the previous database is the newest file in billing-data/backups")
    started = start(args, stack, clock)
    return code or started


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
        subparser.add_argument("--legacy-gpu", action="store_true",
                               help="build the worker for older NVIDIA GPUs such as the GTX 1050 Ti")

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

    restore_parser = commands.add_parser("restore", help="replace the billing database with a backup")
    restore_parser.add_argument("backup", nargs="?", default="latest",
                                help="latest (default), a name, or a number from `python -m app.backups list`: "
                                     "1 is the newest, 2 the one before")
    restore_parser.add_argument("--from", dest="source", choices=list(BACKUP_SOURCES), default="remote",
                                help="off-site backups (remote, the default) or billing-data/backups (local)")
    restore_parser.add_argument("--fallback", action="store_true",
                                help="if there is no such backup there, or it cannot be fetched, use the other place")
    restore_parser.add_argument("--yes", action="store_true", help="do not ask before replacing the database")
    profiles(restore_parser)

    commands.add_parser("status", help="print the pending restart or shutdown")
    commands.add_parser("cancel", help="cancel the pending restart or shutdown")

    args = parser.parse_args(arguments)
    args.profile = list(getattr(args, "profile", []))
    return args


def main(arguments: list[str], stack: Stack | None = None, clock: Clock | None = None) -> int:
    args = parse_arguments(arguments)
    if getattr(args, "outsource", False):
        say(OUTSOURCE_NOTE)
    if stack is None:
        share_billing_data(os.environ)
        select_gpu_build(args, os.environ)
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
    return {"restart": restart, "stop": stop, "start": start, "restore": restore}[args.action](args, stack, clock)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
