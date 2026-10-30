"""Where the server runs, for the host scripts.

Stack says what the scripts ask of a running server, such as a maintenance
notice, a Redis command, or a restore, once, in terms of a few primitives:
run a command in a service, run a one-off container, and start or stop the
services. ComposeStack gives those primitives with the docker CLI, and
KubeStack in scripts/kube.py with kubectl. The scripts never build a docker
command themselves; a test keeps it so.

Runs on the host with only the Python standard library.
"""

import shlex
import subprocess
import sys
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent
# A shutdown stops every service, whichever profiles started it.
ALL_PROFILES = ["*"]
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
READY_PROBE = "import urllib.request; urllib.request.urlopen('http://localhost:8000/ready', timeout=5)"


def run_options(input: str | None, stdin, capture: bool) -> dict:
    """subprocess.run's options: only those asked for, so a key on standard
    input never also lands in the environment or the command line."""
    options = {}
    if capture:
        options["capture_output"] = True
    if capture or input is not None:
        options["text"] = True
    if input is not None:
        options["input"] = input
    if stdin is not None:
        options["stdin"] = stdin
    return options


def failure(result, fallback: str) -> str:
    return (result.stderr or result.stdout or "").strip() or fallback


class Stack:
    """The server's services, wherever they run. Subclasses give the
    primitives; what the scripts ask for is built on them here."""

    root: Path = ROOT
    # The program the primitives run, for messages.
    cli: str = ""
    # How to run the scripts' commands again on this stack, for messages.
    script: str = ""
    # Whether its Redis is the stack's own and keeps queued jobs: then a
    # restart saves them first and reloads Redis's users after.
    owns_redis: bool = False
    # Why restore cannot replace this stack's database, if it cannot.
    cannot_restore: str = ""

    # --- Primitives --------------------------------------------------------------

    def exec(self, service: str, *command: str, input: str | None = None, stdin=None, capture: bool = True,
             terminal: bool = False, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        """Run a command in the running service; with terminal, give it this
        terminal, and env adds to its environment."""
        raise NotImplementedError

    def one_off(self, service: str, *command: str, input: str | None = None, capture: bool = True,
                terminal: bool = False) -> subprocess.CompletedProcess:
        """Run a command in a new container of the service's image, which
        works while the service is stopped."""
        raise NotImplementedError

    def prepare_files(self) -> list[str]:
        """Create what must exist before anything starts; say what was created."""
        return []

    def start_build(self):
        raise NotImplementedError

    def up(self, command: str | None = None, build: bool = True) -> int:
        raise NotImplementedError

    def stop(self, down: bool) -> int:
        raise NotImplementedError

    def start_postgres(self) -> int:
        raise NotImplementedError

    def stop_services(self, services: list[str]) -> int:
        raise NotImplementedError

    def host_path(self, container_path: str) -> Path:
        """Where a file a one-off container saved is on this machine."""
        raise NotImplementedError

    def logs_command(self, service: str) -> str:
        """The command that shows the service's log, for messages."""
        raise NotImplementedError

    def restart_command(self, service: str) -> str:
        """The command that restarts one service, for messages."""
        raise NotImplementedError

    # --- What the scripts ask for ------------------------------------------------

    def config(self, *arguments: str, capture: bool = True) -> subprocess.CompletedProcess:
        """python -m app.config, which reads the settings and secrets as the
        services do."""
        return self.one_off("api", "python", "-m", "app.config", *arguments, capture=capture)

    def maintenance(self, *arguments: str) -> str:
        result = self.exec("api", "python", "-m", "app.maintenance", *arguments)
        if result.returncode != 0:
            raise RuntimeError(failure(result, "the api service is not running"))
        return result.stdout.strip()

    def ready(self) -> bool:
        return self.exec("api", "python", "-c", READY_PROBE).returncode == 0

    def reload_redis_users(self) -> str:
        """Make the running Redis read its users again (redis/start.sh reload).
        Returns what went wrong, or an empty string."""
        result = self.exec("redis", "sh", "/redis/start.sh", "reload")
        answer = (result.stdout or "").strip()
        if result.returncode != 0 or answer != "OK":
            return (result.stderr or answer).strip() or f"exit code {result.returncode}"
        return ""

    def redis(self, *arguments: str) -> str:
        result = self.exec("redis", "sh", "-c", REDIS_CLI, "redis-cli", *arguments)
        if result.returncode != 0:
            raise RuntimeError(failure(result, "the redis service is not running"))
        answer = result.stdout.strip()
        if answer.startswith(REDIS_REFUSALS):
            raise RuntimeError(f"Redis refused: {answer.splitlines()[0]}")
        return answer

    def fetch_backup(self, which: str, key: str) -> tuple[int, str]:
        # The key goes on standard input: arguments and the environment are
        # visible to other processes.
        result = self.one_off("api", "python", "-m", "app.backups", "fetch", which, input=key)
        return result.returncode, "\n".join(part.strip() for part in (result.stdout, result.stderr) if part.strip())

    def backup_now(self) -> int:
        return self.one_off("api", "python", "-m", "app.backups", "now", capture=False, terminal=True).returncode

    def restore_dump(self, dump: Path) -> int:
        # pg_restore reads a custom-format dump from standard input, so
        # nothing is copied into the container.
        with dump.open("rb") as stream:
            return self.exec("postgres", *PG_RESTORE, stdin=stream, capture=False).returncode


class ComposeStack(Stack):
    """The Docker Compose stack, reached through the docker CLI from the
    checkout, wherever the script was started."""

    cli = "docker"
    script = "python scripts/server.py"
    owns_redis = True

    def __init__(self, profiles: list[str], run=subprocess.run, popen=subprocess.Popen, root: Path = ROOT,
                 prepare: Callable[[Path], list[str]] | None = None):
        self.profiles = profiles
        self._run = run
        self._popen = popen
        self.root = root
        self._prepare = prepare

    def compose(self, profiles: list[str] | None = None) -> list[str]:
        selected = self.profiles if profiles is None else profiles
        return ["docker", "compose"] + [part for name in selected for part in ("--profile", name)]

    def docker(self, command: list[str], **options) -> subprocess.CompletedProcess:
        return self._run(command, cwd=self.root, **options)

    def exec(self, service, *command, input=None, stdin=None, capture=True, terminal=False, env=None):
        no_terminal = [] if terminal else ["-T"]
        variables = [part for name, value in (env or {}).items() for part in ("-e", f"{name}={value}")]
        return self.docker(self.compose() + ["exec", *no_terminal, *variables, service, *command],
                           **run_options(input, stdin, capture))

    def one_off(self, service, *command, input=None, capture=True, terminal=False):
        no_terminal = [] if terminal else ["-T"]
        return self.docker(self.compose() + ["run", "--rm", "--no-deps", *no_terminal, service, *command],
                           **run_options(input, None, capture))

    def prepare_files(self) -> list[str]:
        return self._prepare(self.root) if self._prepare else []

    def build(self, *services: str, quiet: bool = False) -> int:
        return self.docker(self.compose() + ["build", *(["--quiet"] if quiet else []), *services]).returncode

    def start_build(self):
        return self._popen(self.compose() + ["build"], cwd=self.root)

    def up(self, command: str | None = None, build: bool = True) -> int:
        if command:
            return self.docker(shlex.split(command, posix=(sys.platform != "win32"))).returncode
        return self.docker(self.compose() + ["up", "-d"] + (["--build"] if build else [])).returncode

    def stop(self, down: bool) -> int:
        return self.docker(self.compose(ALL_PROFILES) + (["down"] if down else ["stop"])).returncode

    def start_postgres(self) -> int:
        return self.docker(self.compose() + ["up", "-d", "--wait", "postgres"]).returncode

    def stop_services(self, services: list[str]) -> int:
        return self.docker(self.compose() + ["stop", *services]).returncode

    def host_path(self, container_path: str) -> Path:
        # billing-data is mounted at /billing in docker-compose.yml.
        return self.root / "billing-data" / Path(container_path).relative_to("/billing")

    def logs_command(self, service: str) -> str:
        return f"docker compose logs {service}"

    def restart_command(self, service: str) -> str:
        return f"docker compose restart {service}"
