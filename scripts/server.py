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
import importlib.util
import json
import os
import re
import secrets
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_script(name: str):
    spec = importlib.util.spec_from_file_location(f"{name}_script", Path(__file__).with_name(f"{name}.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


STACKS = load_script("stacks")
Stack, ComposeStack = STACKS.Stack, STACKS.ComposeStack

UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
DEFAULT_GRACE = "5m"
READY_TIMEOUT = 180
AOF_TIMEOUT = 60
STATUS_INTERVAL = 10
DRAIN_INTERVAL = 5
# docker-compose.yml passes it to Dockerfile.worker as GPU_BUILD.
GPU_BUILD_VARIABLE = "WORKER_GPU_BUILD"
OUTSOURCE_NOTE = "--outsource is no longer needed: OUTSOURCE_PROVIDER in server.env turns outsourcing on or off"
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
# The folder of app.config.TUNNEL_TOKEN_FILE, which the workers hide.
TUNNEL_TOKEN_FOLDER = "secrets/cloudflare"


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


# The variables Docker Compose itself reads, which go in .env; everything
# else is the server's and goes in server.env. ADMIN_WEB_MODE is both: Compose
# picks the admin service's roles by it, and the services get .env too.
# COMPOSE_ONLY_VARIABLES in app/config.py, which a test compares; another
# test fails if docker-compose.yml reads a variable missing here.
COMPOSE_ONLY_VARIABLES = {"API_BIND_ADDRESS", "POSTGRES_DATA_LOCATION", "BILLING_DATA_GROUP", "WORKER_GPU_BUILD",
                          "COMPOSE_PROFILES", "ADMIN_WEB_MODE"}
COMPOSE_ENV, SERVER_ENV = ".env", "server.env"
ADDED_HEADER = "# --- Added by scripts/setup.py or scripts/server.py"
# A line that python-dotenv and Docker Compose read as a setting: also with
# export, spaces around =, or indented.
ASSIGNMENT = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_.]*)\s*=\s*(.*)$")
SPLIT_BACKUP = ".env.before-split"


# --- .env and server.env, edited in place ------------------------------------------

class EnvFile:
    """An env file as lines, so comments and order survive. A setting is
    changed on its own line, or on its commented example line from the
    template, which stays in its block; a new one goes at the end. Where a
    setting is written twice, the last applies, as for dotenv and Compose."""

    def __init__(self, path: Path, lines: list[str] | None = None):
        self.path = path
        if lines is not None:
            self.lines = list(lines)
        else:
            # utf-8-sig: a BOM, as Windows editors write one, is not part of the first name.
            self.lines = path.read_text(encoding="utf-8-sig").splitlines() if path.exists() else []

    def copy(self, path: Path | None = None) -> "EnvFile":
        return EnvFile(path or self.path, self.lines)

    def indexes(self, name: str) -> list[int]:
        return [index for index, line in enumerate(self.lines)
                if (match := ASSIGNMENT.match(line)) and match.group(1) == name]

    def _find(self, name: str, commented: bool) -> int | None:
        if not commented:
            found = self.indexes(name)
            return found[-1] if found else None
        pattern = re.compile(rf"^#\s?(?:export\s+)?{re.escape(name)}\s*=")
        return next((index for index, line in enumerate(self.lines) if pattern.match(line)), None)

    def get(self, name: str) -> str | None:
        index = self._find(name, commented=False)
        if index is None:
            return None
        return _unquote(ASSIGNMENT.match(self.lines[index]).group(2).strip())

    def assignments(self) -> dict[str, int]:
        """Each setting the file sets, by the line that applies: its last."""
        found = {}
        for index, line in enumerate(self.lines):
            match = ASSIGNMENT.match(line)
            if match:
                found[match.group(1)] = index
        return found

    def values(self) -> dict[str, str]:
        """Each setting's value as written, quotes and all."""
        return {name: ASSIGNMENT.match(self.lines[index]).group(2).strip() for name, index in self.assignments().items()}

    def unfinished_value(self) -> str | None:
        """A setting whose quoted value goes on over the next lines, which
        moving single lines would break."""
        for name, value in self.values().items():
            if value[:1] in "'\"" and (len(value) < 2 or not value.rstrip().split(" #")[0].rstrip().endswith(value[0])):
                return name
        return None

    def set(self, name: str, value: str) -> None:
        self.set_line(name, f"{name}={_quote(value)}")

    def set_line(self, name: str, line: str) -> None:
        """Put a setting's whole line, as another file had it."""
        index = self._find(name, commented=False)
        if index is None:
            index = self._find(name, commented=True)
        if index is not None:
            self.lines[index] = line
            return
        if ADDED_HEADER not in self.lines:
            self.lines += ["", ADDED_HEADER]
        self.lines.append(line)

    def take(self, name: str) -> str:
        """Remove every line of the setting, and return the one that applied."""
        found = self.indexes(name)
        line = self.lines[found[-1]]
        for index in reversed(found):
            del self.lines[index]
        return line

    def unset(self, name: str, note: str = "") -> None:
        """Comment the setting out, every line of it, so its default applies again."""
        for index in self.indexes(name):
            self.lines[index] = f"# {self.lines[index]}{note}"

    def profiles(self) -> list[str]:
        return [profile.strip() for profile in (self.get("COMPOSE_PROFILES") or "").split(",") if profile.strip()]

    def add_profile(self, profile: str) -> None:
        profiles = self.profiles()
        if profile not in profiles:
            self.set("COMPOSE_PROFILES", ",".join([*profiles, profile]))

    def remove_profile(self, profile: str) -> None:
        profiles = [item for item in self.profiles() if item != profile]
        if profiles:
            self.set("COMPOSE_PROFILES", ",".join(profiles))
        else:
            self.unset("COMPOSE_PROFILES")

    def save(self, mode_from: Path | None = None) -> None:
        """Written whole or not at all: to a file beside it, then swapped in,
        keeping the file's permissions (or those of mode_from, for a new one)."""
        partial = self.path.with_name(self.path.name + ".partial")
        partial.write_text("\n".join(self.lines) + "\n", encoding="utf-8")
        model = self.path if self.path.exists() else mode_from
        if model is not None and model.exists():
            shutil.copymode(model, partial)
        os.replace(partial, self.path)


def _quote(value: str) -> str:
    # Compose and python-dotenv end a value at " #", and trim spaces.
    if " #" in value or value != value.strip():
        return "'" + value.replace("'", "\\'") + "'"
    return value


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


def applying_values(compose: EnvFile, server: EnvFile) -> dict[str, str]:
    """What each setting is, by the file it belongs to: .env for Compose's,
    server.env for the server's; the other file's only when its own has none."""
    compose_values, server_values = compose.values(), server.values()
    values = {}
    for name in compose_values.keys() | server_values.keys():
        own, other = (compose_values, server_values) if name in COMPOSE_ONLY_VARIABLES else (server_values, compose_values)
        values[name] = own[name] if name in own else other[name]
    return values


def separate_env_files(root: Path) -> list[str]:
    """Put every line in its file: Docker Compose's in .env, the server's in
    server.env, and say what moved, by name only. A .env from before
    server.env existed, which held everything, is split into both templates
    with its values, and kept as .env.before-split. A setting in both files
    keeps the value of the file it belongs to; the other line becomes a
    comment.

    Nothing is written unless every setting keeps its value afterwards, and
    each file is written whole, so a failure leaves the files as they were."""
    compose, server = EnvFile(root / COMPOSE_ENV), EnvFile(root / SERVER_ENV)
    first_split = not server.path.exists()
    for file in (compose, server):
        name = file.unfinished_value()
        if name:
            return [
                f"{file.path.name} sets {name} over several lines, which the scripts do not move; put each "
                f"setting in its file by hand (docs/configuration.md says which). Nothing was changed."
            ]
    to_server = [name for name in compose.assignments() if name not in COMPOSE_ONLY_VARIABLES]
    to_compose = [name for name in server.assignments() if name in COMPOSE_ONLY_VARIABLES]
    if not to_server and not to_compose:
        if first_split:
            server = EnvFile(root / "server.env.example").copy(root / SERVER_ENV)
            server.save(mode_from=compose.path)
            return [f"created {SERVER_ENV} from server.env.example: the server's settings"]
        return []

    before = applying_values(compose, server)
    if first_split:
        new_compose = EnvFile(root / ".env.example").copy(compose.path)
        new_server = EnvFile(root / "server.env.example").copy(server.path)
        for name, index in compose.assignments().items():
            (new_compose if name in COMPOSE_ONLY_VARIABLES else new_server).set_line(name, compose.lines[index])
        # The templates' own active lines that the old file did not set stay
        # examples: a split changes no value.
        for target in (new_compose, new_server):
            for name in target.assignments():
                if name not in before:
                    target.unset(name)
    else:
        new_compose, new_server = compose.copy(), server.copy()
        for source, target, names in ((new_compose, new_server, to_server), (new_server, new_compose, to_compose)):
            for name in names:
                if target.indexes(name):
                    # The value of the file it belongs to applies; keep the other as a comment.
                    source.unset(name, note=f"  (moved: {target.path.name} sets it)")
                else:
                    target.set_line(name, source.take(name))

    after_compose, after_server = new_compose.values(), new_server.values()
    in_both = after_compose.keys() & after_server.keys()
    if in_both or applying_values(new_compose, new_server) != before:
        changed = sorted(in_both) or sorted(
            name for name in before.keys() | applying_values(new_compose, new_server).keys()
            if before.get(name) != applying_values(new_compose, new_server).get(name)
        )
        return [
            f"could not move {', '.join(changed)} between .env and {SERVER_ENV} without changing a value; put each "
            "setting in its file by hand (docs/configuration.md says which). Nothing was changed."
        ]

    if first_split:
        shutil.copy2(compose.path, root / SPLIT_BACKUP)
    # server.env first: should the second write fail, a setting is in both
    # files rather than in neither, and the next run moves it again.
    new_server.save(mode_from=compose.path)
    new_compose.save()
    if first_split:
        return [
            f"split .env into .env, for Docker Compose, and {SERVER_ENV}, for the server's settings, "
            f"keeping your values; the old file is {SPLIT_BACKUP}"
        ]
    messages = []
    if to_server:
        what = "it is one of the server's settings" if len(to_server) == 1 else "they are the server's settings"
        messages.append(f"moved {', '.join(to_server)} from .env to {SERVER_ENV}: {what}")
    if to_compose:
        what = "Docker Compose reads it" if len(to_compose) == 1 else "Docker Compose reads them"
        messages.append(f"moved {', '.join(to_compose)} from {SERVER_ENV} to .env: {what}")
    return messages


def prepare_files(root: Path) -> list[str]:
    """Create the files Compose needs before anything starts, and say what
    was created. Existing files are never changed."""
    created = []
    env = root / ".env"
    if not env.exists():
        shutil.copyfile(root / ".env.example", env)
        created.append("created .env from .env.example, for Docker Compose")
    # Also moves a line that is in the wrong one of the two files.
    created += separate_env_files(root)
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
    # Even without a tunnel: the workers hide the token's folder under an
    # empty one, which Docker can only mount where a folder exists.
    if create_folders(root, root / TUNNEL_TOKEN_FOLDER):
        created.append(f"created the empty folder {TUNNEL_TOKEN_FOLDER}, for a Cloudflare Tunnel's token")
    return created


def create_folders(root: Path, folder: Path) -> bool:
    """Create folder and its missing parents below root, readable by the
    services. Returns whether folder was created."""
    if folder.is_dir():
        return False
    for path in [*reversed(folder.parents), folder]:
        if path.is_relative_to(root) and path != root and not path.exists():
            path.mkdir()
            # PostgreSQL, Redis, and the services read the secrets as other users.
            path.chmod(0o755)
    return True


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
    create_folders(root, password.parent)
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
    restart with the setting. Nothing changes when it is already on, or
    when the Redis is not the stack's own."""
    if not stack.owns_redis:
        return
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


def reload_redis_users(stack: Stack) -> None:
    """Redis builds its users only when its container starts, and an update
    that adds a user or password may not recreate it."""
    if not stack.owns_redis:
        return
    problem = stack.reload_redis_users()
    if problem:
        say(f"Redis did not reload its users ({problem}); a new Redis user cannot sign in until: "
            f"{stack.restart_command('redis')}")


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
    reload_redis_users(stack)
    if not wait_ready(stack, clock):
        say(f"the API is not ready after {READY_TIMEOUT} s; check `{stack.logs_command('api')}`. "
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
        say(f"{stack.cli} failed with exit code {code}")
        return code
    # The notice stays in Redis; the API removes it when it starts again.
    say(f"stopped. Start again with: {stack.script} start")
    return 0


def start(args, stack: Stack, clock: Clock) -> int:
    if not prepare(stack):
        return 1
    say("starting")
    # Built every time, so a start after an update never runs old code; an
    # unchanged image is a quick cache hit.
    code = stack.up()
    if code != 0:
        say(f"{stack.cli} failed with exit code {code}")
        return code
    reload_redis_users(stack)
    if not wait_ready(stack, clock):
        say(f"the API is not ready after {READY_TIMEOUT} s; check `{stack.logs_command('api')}`")
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
    if stack.cannot_restore:
        say(f"{stack.cannot_restore}: nothing was changed")
        return 1
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
        say(f"the safety backup failed, so nothing was restored. Start again with: {stack.script} start")
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


def actions() -> list[str]:
    """The commands, which scripts/kube.py also runs on a cluster."""
    commands = next(action for action in build_parser()._actions if isinstance(action, argparse._SubParsersAction))
    return list(commands.choices)


def parse_arguments(arguments: list[str], prog: str = "python scripts/server.py") -> argparse.Namespace:
    args = build_parser(prog).parse_args(arguments)
    args.profile = list(getattr(args, "profile", []))
    return args


def build_parser(prog: str = "python scripts/server.py") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
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
    return parser


def main(arguments: list[str], stack: Stack | None = None, clock: Clock | None = None) -> int:
    args = parse_arguments(arguments, stack.script if stack else "python scripts/server.py")
    if getattr(args, "outsource", False):
        say(OUTSOURCE_NOTE)
    if stack is None:
        share_billing_data(os.environ)
        select_gpu_build(args, os.environ)
    stack = stack or ComposeStack(args.profile, prepare=prepare_files)
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
