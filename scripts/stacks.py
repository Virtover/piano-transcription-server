"""Where the server runs, for the host scripts.

Stack says what the scripts ask of a running server, such as a maintenance
notice, a Redis command, or a restore, once, in terms of a few primitives:
run a command in a service, run a one-off container, and start or stop the
services. ComposeStack gives those primitives with the docker CLI, and
KubeStack in scripts/kube.py with kubectl. The scripts never build a docker
command themselves; a test keeps it so.

Runs on the host with only the Python standard library.
"""

import json
import re
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
# Where a configuration session sees the checkout (app/diagnosis.py).
SESSION_ROOT = "/setup"
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


# --- Images from Docker Hub, or an identical copy elsewhere -------------------

# Registries that copy Docker Hub's official images (python, redis, postgres)
# byte for byte, by digest: AWS's in the US, then Google's worldwide one.
OFFICIAL_IMAGE_COPIES = ["public.ecr.aws/docker/library/", "mirror.gcr.io/library/"]
# Other Docker Hub images: their publisher's own registry first, then
# Google's copy of Docker Hub, which keeps the images many people pull.
PUBLISHER_REGISTRIES = {"nvidia/cuda": ["nvcr.io/"]}
OTHER_IMAGE_COPIES = ["mirror.gcr.io/"]
# The longest one source may take to give an image.
PULL_TIMEOUT = 900
FROM_LINE = re.compile(r"^FROM\s+(?:--\S+\s+)*(\S+)(?:\s+AS\s+(\S+))?", re.IGNORECASE | re.MULTILINE)


def image_sources(image: str) -> list[str]:
    """Where image can be pulled from: Docker Hub, then copies of it. An
    image of another registry has only its own."""
    name = image.split("@")[0]
    if ":" in name.rsplit("/", 1)[-1]:
        name = name.rsplit(":", 1)[0]
    first = name.split("/")[0]
    if "/" in name and ("." in first or ":" in first or first == "localhost"):
        return [image]
    if "/" not in name:
        return [image] + [copy + image for copy in OFFICIAL_IMAGE_COPIES]
    return [image] + [registry + image for registry in PUBLISHER_REGISTRIES.get(name, [])] \
        + [copy + image for copy in OTHER_IMAGE_COPIES]


def base_images(dockerfiles: list[Path]) -> list[str]:
    """The images the Dockerfiles start FROM, without their own stages and
    names chosen by a build argument."""
    images = []
    for dockerfile in dockerfiles:
        if not dockerfile.is_file():
            continue
        stages = set()
        for image, stage in FROM_LINE.findall(dockerfile.read_text(encoding="utf-8")):
            if "$" not in image and image not in stages and image != "scratch" and image not in images:
                images.append(image)
            if stage:
                stages.add(stage)
    return images


def ensure_image(docker: Callable[[list[str]], subprocess.CompletedProcess], image: str,
                 say: Callable[[str], None] = lambda text: print(text, file=sys.stderr)) -> bool:
    """When this machine lacks image, pull it from the first source that
    gives it, under its own name, so builds and Compose find it as if Docker
    Hub had given it. An image this machine has is left as it is, as builds
    and Compose do: a start needs no network and rebuilds nothing."""
    def run(command: list[str]) -> bool:
        try:
            return docker(command).returncode == 0
        except subprocess.TimeoutExpired:
            return False

    if run(["docker", "image", "inspect", "--format", "{{.Id}}", image]):
        return True
    sources = image_sources(image)
    for source in sources:
        if not run(["docker", "pull", "--quiet", source]):
            continue
        if source == image:
            return True
        if run(["docker", "tag", source, image]):
            run(["docker", "image", "rm", source])
            say(f"{image}: taken from {source}, an identical copy, as Docker Hub failed.")
            return True
    say(f"{image}: no registry gave it ({', '.join(sources)}); the build or start that needs it will fail.")
    return False


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

    def config_session(self, stderr=None) -> subprocess.Popen:
        """python -m app.config session: one container of the API's image
        that answers configuration commands on standard input, reading this
        checkout's env files as they change (app/diagnosis.py)."""
        raise NotImplementedError

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

    def remove_stale_containers(self) -> list[str]:
        """Remove what cannot be started again as it is, so starting
        creates it anew; the names removed."""
        return []

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
        self._services: dict | None = None
        self._ensured: set[str] = set()

    def compose(self, profiles: list[str] | None = None) -> list[str]:
        selected = self.profiles if profiles is None else profiles
        return ["docker", "compose"] + [part for name in selected for part in ("--profile", name)]

    def docker(self, command: list[str], **options) -> subprocess.CompletedProcess:
        return self._run(command, cwd=self.root, **options)

    def quiet_docker(self, command: list[str]) -> subprocess.CompletedProcess:
        # A registry that accepts the connection and then stalls must not
        # hold a start: after the timeout, the next source is tried.
        timeout = {"timeout": PULL_TIMEOUT} if command[:2] == ["docker", "pull"] else {}
        return self.docker(command, capture_output=True, text=True, **timeout)

    def ensure_images(self, services: tuple[str, ...] = (), build: bool = True, start: bool = True) -> None:
        """Before a build or start: the images it needs that this machine
        lacks, from Docker Hub or a copy of it (ensure_image). The images the
        builds start from with build, and the services' own with start; only
        those of services, when given."""
        if self._services is None:
            result = self.quiet_docker(self.compose() + ["config", "--format", "json"])
            try:
                self._services = json.loads(result.stdout)["services"] if result.returncode == 0 else {}
            except (TypeError, ValueError, KeyError):
                self._services = {}
        dockerfiles, images = [], []
        for name, service in self._services.items():
            if services and name not in services:
                continue
            if service.get("build"):
                path = Path(service["build"].get("context") or self.root) / service["build"].get("dockerfile", "Dockerfile")
                if build and path not in dockerfiles:
                    dockerfiles.append(path)
            elif start and service.get("image") and service["image"] not in images:
                images.append(service["image"])
        self._ensure(base_images(dockerfiles) + images)

    def _ensure(self, images: list[str]) -> None:
        for image in images:
            if image not in self._ensured:
                self._ensured.add(image)
                ensure_image(self.quiet_docker, image)

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

    def config_session(self, stderr=None):
        # The folder, not the files: a save replaces a file, which a mount
        # of the file alone would not show.
        return self._popen(
            self.compose() + ["run", "--rm", "--no-deps", "-T", "-v", f"{self.root}:{SESSION_ROOT}:ro", "api",
                              "python", "-m", "app.config", "session", "--root", SESSION_ROOT],
            cwd=self.root, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr or subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace")

    def build(self, *services: str, quiet: bool = False) -> int:
        self.ensure_images(services, start=False)
        return self.docker(self.compose() + ["build", *(["--quiet"] if quiet else []), *services]).returncode

    def start_build(self):
        self.ensure_images(start=False)
        return self._popen(self.compose() + ["build"], cwd=self.root)

    def up(self, command: str | None = None, build: bool = True) -> int:
        if command:
            return self.docker(shlex.split(command, posix=(sys.platform != "win32"))).returncode
        self.ensure_images(build=build)
        return self.docker(self.compose() + ["up", "-d"] + (["--build"] if build else [])).returncode

    def stop(self, down: bool) -> int:
        return self.docker(self.compose(ALL_PROFILES) + (["down"] if down else ["stop"])).returncode

    def start_postgres(self) -> int:
        return self.docker(self.compose() + ["up", "-d", "--wait", "postgres"]).returncode

    def stop_services(self, services: list[str]) -> int:
        return self.docker(self.compose() + ["stop", *services]).returncode

    def restart_services(self, services: list[str]) -> int:
        return self.docker(self.compose() + ["restart", *services]).returncode

    def remove_stale_containers(self) -> list[str]:
        # A stopped container keeps the ID of its network. After the network
        # is created again, for example by `docker network prune` or a Docker
        # Desktop reset, Docker cannot start it ("network ... not found"), and
        # `up` does not replace it. Removed, it is created anew; its volumes stay.
        def output(command: list[str]) -> list[str] | None:
            result = self.docker(command, capture_output=True, text=True)
            return result.stdout.splitlines() if result.returncode == 0 else None

        containers = output(self.compose(ALL_PROFILES) + ["ps", "-a", "-q"])
        networks = output(["docker", "network", "ls", "-q", "--no-trunc"])
        if not containers or networks is None:
            return []
        described = output(["docker", "inspect", "--format",
                            "{{.Name}} {{.State.Running}}{{range .NetworkSettings.Networks}} {{.NetworkID}}{{end}}",
                            *containers]) or []
        stale = []
        for line in described:
            name, running, *network_ids = line.split()
            if running == "false" and set(network_ids) - set(networks):
                stale.append(name.lstrip("/"))
        if stale and output(["docker", "rm", *stale]) is None:
            return []
        return stale

    def host_path(self, container_path: str) -> Path:
        # billing-data is mounted at /billing in docker-compose.yml.
        return self.root / "billing-data" / Path(container_path).relative_to("/billing")

    def logs_command(self, service: str) -> str:
        return f"docker compose logs {service}"

    def restart_command(self, service: str) -> str:
        return f"docker compose restart {service}"

    # --- Images for a home cluster's nodes (the hub's registry) -------------

    def build_image(self, dockerfile: str, tag: str, build_args: dict[str, str]) -> int:
        arguments = [part for name, value in build_args.items() for part in ("--build-arg", f"{name}={value}")]
        self._ensure(base_images([self.root / dockerfile]))
        return self.docker(["docker", "build", "-f", dockerfile, *arguments, "-t", tag, "."]).returncode

    def push_image(self, tag: str) -> str | None:
        """Push tag and return its reference by digest, or None."""
        if self.docker(["docker", "push", tag]).returncode != 0:
            return None
        result = self.docker(["docker", "image", "inspect", "--format", "{{range .RepoDigests}}{{println .}}{{end}}", tag],
                             capture_output=True, text=True)
        repository = tag.rsplit(":", 1)[0]
        for line in (result.stdout or "").splitlines():
            if line.startswith(repository + "@sha256:"):
                return line.strip()
        return None


class NodeStack(ComposeStack):
    """A device of a home cluster: docker-compose.node.yml, with the GPU
    file on a device with an NVIDIA GPU, as the project piano-node, with
    node.env. It has no API, no database, and no Redis of its own."""

    script = "python scripts/server.py"
    owns_redis = False
    cannot_restore = "This device is a node of a home cluster and has no billing database; restore on the hub"
    PROJECT = "piano-node"
    ENV_FILE = "node.env"

    def __init__(self, gpu: bool, run=subprocess.run, popen=subprocess.Popen, root: Path = ROOT):
        super().__init__([], run=run, popen=popen, root=root)
        self.gpu = gpu

    def compose(self, profiles: list[str] | None = None) -> list[str]:
        files = ["-f", "docker-compose.node.yml"] + (["-f", "docker-compose.node-gpu.yml"] if self.gpu else [])
        return ["docker", "compose", "-p", self.PROJECT, *files, "--env-file", self.ENV_FILE]

    def up(self, command: str | None = None, build: bool = True, recreate: bool = False) -> int:
        # recreate: after join, whose new password a running worker read before.
        self.ensure_images(build=build)
        return self.docker(self.compose() + ["up", "-d"] + (["--build"] if build else [])
                           + (["--force-recreate"] if recreate else [])).returncode

    def pull(self) -> int:
        return self.docker(self.compose() + ["pull", "worker"]).returncode

    def stop(self, down: bool) -> int:
        return self.docker(self.compose() + (["down"] if down else ["stop"])).returncode

    def drain(self, timeout: float) -> int:
        """Let the worker finish its jobs and take no new ones."""
        return self.exec("worker", "python3", "-m", "app.worker.supervisor", "drain", f"{timeout:g}s",
                         capture=False).returncode

    def running(self) -> bool:
        result = self.docker(self.compose() + ["ps", "--status", "running", "-q", "worker"],
                             capture_output=True, text=True)
        return result.returncode == 0 and bool((result.stdout or "").strip())

    def status(self) -> int:
        return self.exec("worker", "python3", "-m", "app.cluster", "status", capture=False).returncode

    def logs_command(self, service: str) -> str:
        files = " -f docker-compose.node-gpu.yml" if self.gpu else ""
        return f"docker compose -p {self.PROJECT} -f docker-compose.node.yml{files} --env-file {self.ENV_FILE} logs {service}"

    def restart_command(self, service: str) -> str:
        return "python scripts/server.py restart"
