"""Set up the server's optional features, step by step, and turn them off.

    python scripts/setup.py                   # what is on, then a menu
    python scripts/setup.py status            # what is on and what is missing
    python scripts/setup.py outsourcing       # set one feature up, test it, and fix what fails
    python scripts/setup.py check [feature]   # test what is on with the real services, and fix what fails
    python scripts/setup.py fix               # repair the env files, the secret files, and the settings
    python scripts/setup.py set [NAME]        # change any one setting or secret file, checked
    python scripts/setup.py off outsourcing   # turn one off
    python scripts/setup.py cluster           # a home cluster: what it needs, and this machine as its hub

A basic server needs none of this: python scripts/server.py start runs it.

Every value is checked as it is typed, the way the server will read it, and
every problem names the settings and secret files it is about, so the
script asks again for exactly those. Nothing here needs a file edited by
hand. The questions go to one container of the API's image, which reads
the files as they change (app/diagnosis.py).

Runs on the host with only the Python standard library and the docker CLI.
What the features are, what each needs, and what is missing come from the
server itself (python -m app.config features --json), and what external
services must provide from python -m app.config requirements, which states
what is needed and why, never where a provider's console puts it: that
changes, so this script needs no update when a provider does. Settings go to
server.env, and the services Compose starts to COMPOSE_PROFILES in .env,
keeping their comments; secrets are asked without showing them and written
to ./secrets, and never printed.
"""

import argparse
import difflib
import getpass
import importlib.util
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs" / "configuration.md"
WIDTH = 78


def load_server_script():
    """scripts/server.py, for the files a start creates and how env files
    are edited."""
    spec = importlib.util.spec_from_file_location("server_script", Path(__file__).with_name("server.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SERVER_SCRIPT = load_server_script()
# The user ID the services run as (docker-compose.yml), who must read the secrets.
CONTAINER_UID = 10001
KUBE_FOLDER = Path("deploy") / "kubernetes"


def load_script(name: str):
    """Another script of scripts/, such as kube.py, only when it is needed."""
    spec = importlib.util.spec_from_file_location(f"{name}_script", Path(__file__).with_name(f"{name}.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
# Setup checks the files on this machine, which kube.py copies to a cluster,
# so it always asks Compose.
COMPOSE = SERVER_SCRIPT.ComposeStack([], root=ROOT)
EnvFile = SERVER_SCRIPT.EnvFile
ADDED_HEADER = SERVER_SCRIPT.ADDED_HEADER
COMPOSE_ENV, SERVER_ENV = SERVER_SCRIPT.COMPOSE_ENV, SERVER_SCRIPT.SERVER_ENV


class EnvFiles:
    """server.env and .env, each setting in the one it belongs to."""

    def __init__(self, root: Path):
        self.server = EnvFile(root / SERVER_ENV)
        self.compose = EnvFile(root / COMPOSE_ENV)

    def of(self, name: str) -> "EnvFile":
        return self.compose if name in SERVER_SCRIPT.COMPOSE_ONLY_VARIABLES else self.server

    def save(self) -> None:
        # COMPOSE_FILE follows WORKER_PROCESSOR, so plain docker compose does too.
        SERVER_SCRIPT.select_compose_files(self.compose)
        self.server.save(mode_from=self.compose.path)
        self.compose.save()


def worker_processor(root: Path) -> tuple[str, str]:
    """What the worker of this machine's stack runs on, and why, as .env says."""
    compose = EnvFile(root / COMPOSE_ENV)
    chosen = compose.get(SERVER_SCRIPT.PROCESSOR_VARIABLE)
    if SERVER_SCRIPT.processor(compose) == "cpu":
        return "cpu", ("on the CPU (WORKER_PROCESSOR=cpu, docker-compose.cpu.yml): with invited nodes it leaves piano "
                       "and ensemble jobs to them and RunPod")
    if chosen is None:
        return "gpu", "on the GPU (the default; a start sets WORKER_PROCESSOR from whether nvidia-smi finds one)"
    return "gpu", "on the GPU (WORKER_PROCESSOR=gpu)"


# --- Reading the files people write ------------------------------------------------------

def env_line_problems(lines: list[str]) -> tuple[list[int], dict[str, list[int]]]:
    """Lines that are no setting, comment, or blank line; and the settings
    written on more than one line, with their lines. A quoted value that goes
    on over lines is one setting."""
    broken, seen, quote = [], {}, None
    for index, line in enumerate(lines):
        if quote:
            if line.rstrip().split(" #")[0].rstrip().endswith(quote):
                quote = None
            continue
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        match = SERVER_SCRIPT.ASSIGNMENT.match(line)
        if not match:
            broken.append(index)
            continue
        value = match.group(2).strip()
        if value[:1] in "'\"" and (len(value) < 2 or not value.split(" #")[0].rstrip().endswith(value[0])):
            quote = value[0]
        seen.setdefault(match.group(1), []).append(index)
    return broken, {name: indexes for name, indexes in seen.items() if len(indexes) > 1}


def clean_secret(value: str) -> str:
    """A secret as the services read it: without a byte order mark, and
    without the spaces and line ends around it."""
    return value.lstrip("﻿").strip()


def decoded_secret(raw: bytes) -> tuple[str | None, list[str]]:
    """A secret file's text, cleaned, and what had to go: a byte order mark,
    UTF-16 (which Windows PowerShell's > writes), Windows line ends, or
    spaces. None for a file that is no text."""
    reasons = []
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        reasons.append("UTF-16 text, as Windows PowerShell's > writes it")
        try:
            text = raw.decode("utf-16")
        except UnicodeDecodeError:
            return None, []
    else:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None, []
    if text.startswith("﻿"):
        reasons.append("a byte order mark (BOM)")
    cleaned = clean_secret(text)
    if "\r" in text:
        reasons.append("Windows line ends")
    # One line end after the value is what most editors write, and harmless.
    elif text.lstrip("﻿") not in (cleaned, cleaned + "\n"):
        reasons.append("spaces or empty lines around the value")
    return cleaned, reasons


def read_secret_source(path: Path, name: str) -> tuple[str, str | None]:
    """A downloaded file's text for the secret name, or why it cannot be."""
    if not path.is_file():
        return "", f"{path} is not a file."
    try:
        text, _ = decoded_secret(path.read_bytes())
    except OSError as error:
        return "", f"{path} cannot be read ({type(error).__name__})."
    if text is None:
        return "", f"{path} is no text."
    if name.endswith(".json"):
        try:
            json.loads(text)
        except ValueError as error:
            return "", f"{path} is not valid JSON ({getattr(error, 'msg', error)}): download it again."
    return text, None


def comment_above(lines: list[str], name: str) -> list[str]:
    """The comment lines just above a setting's line, or its commented
    example, as an example file explains it."""
    pattern = re.compile(rf"^#?\s?{re.escape(name)}\s*=")
    index = next((number for number, line in enumerate(lines) if pattern.match(line)), None)
    if index is None:
        return []
    comments = []
    for line in reversed(lines[:index]):
        if not line.startswith("#") or SERVER_SCRIPT.ASSIGNMENT.match(line.lstrip("# ")):
            break
        comments.insert(0, line.lstrip("#").strip())
    return [line for line in comments if line]


def _shortened(text: str, width: int = 50) -> str:
    text = text.strip()
    return text if len(text) <= width else text[:width - 3] + "..."


def _value_text(value) -> str | None:
    """A value from the server, as an env file would have it; None when
    unset. describe shows a duration with its seconds, and auto with what it
    means: 15m (900 s) is 15m."""
    if value in (None, "", [], {}, "(unset)"):
        return None
    text = _shown(value)
    return text.split(" (", 1)[0] if isinstance(value, str) else text


def _sentence(text: str) -> str:
    """A message as a sentence of a question: with its end mark."""
    return text if text.endswith((".", "?", "!")) else text + "."


def in_sentence(title: str) -> str:
    """A title inside a sentence: "Object storage" becomes "object storage"."""
    return title[0].lower() + title[1:] if title[1:2].islower() else title


# --- Descriptions, from docs/configuration.md --------------------------------------------

def setting_descriptions(path: Path = DOCS) -> dict[str, str]:
    """Each setting's description from its row in the docs, which a test keeps
    complete, as plain text."""
    descriptions = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^\| `([A-Z][A-Z0-9_]+)` \|(.*)\|\s*$", line)
        if match:
            cells = [cell.strip() for cell in match.group(2).split("|")]
            descriptions.setdefault(match.group(1), _plain(cells[-1]))
    return descriptions


def _plain(markdown: str) -> str:
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", markdown)
    return text.replace("`", "").replace("**", "")


# --- The server, asked through Docker ----------------------------------------------------

def run_config(*arguments: str, capture: bool = True) -> subprocess.CompletedProcess:
    """python -m app.config in a one-off API container, which reads .env, server.env, and
    ./secrets as the services do. The first run builds the API image."""
    return COMPOSE.config(*arguments, capture=capture)


class ConfigSession:
    """python -m app.config in one container for the whole run, started at
    the first question (app/diagnosis.py): a container per question takes
    seconds. If the session cannot start, or stops answering, as an image from
    before sessions does, each question runs in a container of its own."""

    def __init__(self, stack=None, fallback: Callable[..., subprocess.CompletedProcess] | None = None):
        self.stack = stack or COMPOSE
        self.fallback = fallback or run_config
        self.process = None
        self.failed = False

    def __call__(self, *arguments: str, capture: bool = True) -> subprocess.CompletedProcess:
        answer = None if self.failed else self._ask(arguments)
        if answer is None:
            return self.fallback(*arguments, capture=capture)
        result = subprocess.CompletedProcess(arguments, answer["returncode"], answer["stdout"], answer["stderr"])
        if not capture:
            sys.stdout.write(result.stdout)
            sys.stderr.write(result.stderr)
        return result

    def _ask(self, arguments) -> dict | None:
        try:
            if self.process is None:
                self.process = self.stack.config_session()
            self.process.stdin.write(json.dumps({"args": list(arguments)}) + "\n")
            self.process.stdin.flush()
            line = self.process.stdout.readline()
            if not line:
                raise OSError("the session ended")
            answer = json.loads(line)
            if not isinstance(answer, dict) or "returncode" not in answer:
                raise ValueError("not an answer")
            return answer
        except (OSError, ValueError, NotImplementedError):
            self.failed = True
            self.close()
            return None

    def close(self) -> None:
        process, self.process = self.process, None
        if process is None:
            return
        try:
            process.stdin.close()
            process.wait(timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            process.kill()


def build_api_image() -> None:
    """Bring the API image up to this code, which answers the questions. A
    cached build that finds nothing changed takes seconds."""
    COMPOSE.build("api", quiet=True)


# --- Output ---------------------------------------------------------------------------------

class Style:
    """ANSI colors, only for a terminal that shows them, and never with
    NO_COLOR set (no-color.org)."""

    CODES = {"bold": "1", "dim": "2", "red": "31", "green": "32", "yellow": "33", "cyan": "36"}

    def __init__(self, enabled: bool = False):
        self.enabled = enabled

    def __call__(self, text: str, *names: str) -> str:
        if not self.enabled or not names or not text:
            return text
        return f"\033[{';'.join(self.CODES[name] for name in names)}m{text}\033[0m"

    @classmethod
    def for_terminal(cls, stream=sys.stdout) -> "Style":
        if os.environ.get("NO_COLOR") or not getattr(stream, "isatty", lambda: False)():
            return cls(False)
        return cls(os.name != "nt" or _windows_colors())


def _windows_colors() -> bool:
    """Turn on ANSI colors in a Windows console, as Windows Terminal has them."""
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        # ENABLE_VIRTUAL_TERMINAL_PROCESSING
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:
        return False


@dataclass
class Console:
    """Input and output, replaced in tests."""

    ask: Callable[[str], str] = input
    ask_hidden: Callable[[str], str] = getpass.getpass
    say: Callable[[str], None] = print
    style: Style = field(default_factory=Style)

    def wrap(self, text: str, indent: str = "  ", *styles: str) -> None:
        """Wrapped before coloring, so the colors do not count as width."""
        for line in textwrap.wrap(text, WIDTH - len(indent)) or [""]:
            self.say(indent + self.style(line, *styles))

    def labelled(self, label: str, text: str, indent: str = "  ", *styles: str) -> None:
        """label, styled, then text wrapped beside it, its lines lined up."""
        lines = textwrap.wrap(text, WIDTH - len(indent) - len(label)) or [""]
        self.say(indent + self.style(label, *styles) + lines[0])
        for line in lines[1:]:
            self.say(indent + " " * len(label) + line)

    def heading(self, text: str) -> None:
        self.say("")
        self.say(self.style(text, "bold", "cyan"))

    def yes(self, question: str, default: bool = True) -> bool:
        answer = self.ask(f"{question} [{'Y/n' if default else 'y/N'}] ").strip().lower()
        return default if not answer else answer.startswith("y")


def host_paths(text: str) -> str:
    """The server names secrets by their path in the container; on the host
    they are in ./secrets."""
    return text.replace("/run/secrets/", "secrets/")


@dataclass
class Missing:
    """What keeps a feature that is on from working, sorted for reading."""

    secrets: list[dict]
    settings: list[str]
    problems: list[str]

    @classmethod
    def of(cls, feature: dict) -> "Missing":
        if not feature["enabled"]:
            return cls([], [], [])
        problems = [host_paths(problem) for problem in feature["problems"]]
        # A secret the feature's own rules check, as one with an alternative,
        # is missing only when they say so.
        secrets_missing = [
            secret for secret in feature["secrets"]
            if not secret["present"] and not secret["optional"]
            and (secret.get("checked", True) or any(secret["name"] in problem for problem in problems))
        ]
        settings_missing = [
            setting["name"] for setting in feature["settings"]
            if setting["required"] and setting["value"] in (None, "", [], {})
        ]
        # The rest, without what the two lists already say.
        said = [f"secrets/{secret['name']}" for secret in secrets_missing]
        said += [f"{name} is required" for name in settings_missing]
        problems = [problem for problem in problems if not any(item in problem for item in said)]
        return cls(secrets_missing, settings_missing, problems)

    @property
    def count(self) -> int:
        return len(self.secrets) + len(self.settings) + len(self.problems)

    def show(self, console: Console, indent: str) -> None:
        style = console.style
        for secret in self.secrets:
            console.say(f"{indent}{style('missing secret file', 'yellow')}  {style('secrets/' + secret['name'], 'bold')}"
                        f"  {style(secret['description'], 'dim')}")
        for name in self.settings:
            console.say(f"{indent}{style('missing setting', 'yellow')}      {style(name, 'bold')}"
                        f"  {style('in server.env', 'dim')}")
        for problem in self.problems:
            console.wrap(f"! {problem}", indent, "red")


# --- A home cluster's checks, on the host --------------------------------------------------

NODES = SERVER_SCRIPT.NODES
STACKS = SERVER_SCRIPT.STACKS


def run_quietly(command: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
    except (OSError, subprocess.TimeoutExpired) as error:
        return subprocess.CompletedProcess(command, 127, "", str(error))


def tailscale_addresses(run=run_quietly) -> list[str]:
    """This machine's Tailscale IPv4 addresses; none without Tailscale."""
    if shutil.which("tailscale") is None:
        return []
    result = run(["tailscale", "ip", "-4"])
    return [line.strip() for line in (result.stdout or "").splitlines() if result.returncode == 0 and line.strip()]


@dataclass
class ClusterCheck:
    ok: bool
    text: str
    fix: str = ""
    # Not needed; only shown.
    optional: bool = False


def cluster_checks(root: Path, features: dict | None, run=run_quietly, platform: str = sys.platform) -> list[ClusterCheck]:
    """What a home cluster needs on this machine (docs/cluster.md): a hub's
    or, with node.env, a node's. features is `features --json`, or None when
    it cannot be read here, as on a node, which has no API image."""
    checks = []
    addresses = tailscale_addresses(run)
    installed = shutil.which("tailscale") is not None
    checks.append(ClusterCheck(
        bool(addresses), f"Tailscale, signed in: {', '.join(addresses)}" if addresses else "Tailscale, signed in",
        "Install Tailscale from tailscale.com on every device and sign in to one tailnet" if not installed
        else "Tailscale is installed but has no address: sign in with `tailscale up`"))
    checks.append(ClusterCheck(shutil.which("docker") is not None, "Docker", "Install Docker Desktop or Docker Engine"))
    if NODES.is_node(root):
        values = NODES.env_values(root / NODES.NODE_ENV)
        hub = values.get("HUB_ADDRESS", "")
        checks.append(ClusterCheck(True, f"joined as the node {values['NODE_NAME']}, of the hub at {hub}"))
        checks.append(ClusterCheck((root / NODES.NODE_SECRETS / "redis_password").is_file(),
                                   "its password from the invite, in secrets/node",
                                   "Invite this device again on the hub and join with the new invite"))
        checks.append(ClusterCheck(bool(hub) and NODES.reachable(hub, NODES.REDIS_PORT, timeout=3),
                                   f"the hub's Redis answers at {hub}:{NODES.REDIS_PORT}",
                                   "Start the hub, and check Tailscale on both devices (`tailscale ping <hub>`)"))
        if values.get("NODE_PROCESSOR", "gpu") != "cpu":
            checks.append(ClusterCheck(shutil.which("nvidia-smi") is not None, "an NVIDIA driver, for the GPU",
                                       "Install the NVIDIA driver, or join again with --cpu"))
        return checks
    values = NODES.env_values(root / ".env")
    address = values.get("HUB_ADDRESS")
    checks.append(ClusterCheck(
        bool(address) and "docker-compose.hub.yml" in values.get("COMPOSE_FILE", ""),
        f"this machine is the hub, on {address}" if address else "this machine is the hub",
        "Run: python scripts/setup.py cluster, or python scripts/admin.py nodes hub"))
    lan = values.get("HUB_BIND_ADDRESS")
    if address and lan:
        checks.append(ClusterCheck(False, f"published on the local network ({lan}), without Tailscale's encryption",
                                   "Fine for a test; for real use: python scripts/admin.py nodes hub", optional=True))
    elif address and addresses and address not in addresses:
        checks.append(ClusterCheck(False, f"HUB_ADDRESS {address} is this machine's Tailscale address",
                                   "Run: python scripts/admin.py nodes hub"))
    elif address and addresses:
        served = run(["tailscale", "serve", "status"])
        missing = [port for port in NODES.HUB_PORTS if f":{port}" not in (served.stdout or "")]
        checks.append(ClusterCheck(not missing, "Tailscale serves Redis and the bucket to the tailnet",
                                   f"Run: python scripts/admin.py nodes hub (ports {', '.join(map(str, missing))} are not served)"))
    processor, text = worker_processor(root)
    if processor == "cpu":
        checks.append(ClusterCheck(True, f"the hub's worker {text}"))
        outsourcing = (features or {}).get("outsourcing")
        if outsourcing is not None and address:
            checks.append(ClusterCheck(
                bool(outsourcing.get("enabled")), "RunPod, for piano and ensemble jobs while no node is up",
                "Without it they wait for a node: python scripts/setup.py outsourcing", optional=True))
    elif STACKS.has_nvidia_gpu() is False:
        checks.append(ClusterCheck(
            False, "an NVIDIA GPU for the hub's worker (WORKER_PROCESSOR=gpu)",
            "Docker cannot start the worker without one. Install the NVIDIA driver, or run it on the CPU: "
            "python scripts/setup.py set WORKER_PROCESSOR"))
    if address:
        cloud = NODES.env_values(root / "server.env").get("JOB_STORAGE") == "s3"
        checks.append(ClusterCheck(
            True, "job files in your STORAGE_ bucket (JOB_STORAGE=s3), whose keys every node gets" if cloud
            else "job files in the hub's own bucket (JOB_STORAGE=local), each node with keys of its own"))
    invited = NODES.invited(root)
    checks.append(ClusterCheck(bool(invited), f"devices invited: {', '.join(invited)}" if invited else "devices invited",
                               "On this machine: python scripts/admin.py nodes invite <name>", optional=not address))
    if platform == "win32" and address:
        checks.append(ClusterCheck(False, "Docker Desktop starts with Windows, and Windows signs in by itself",
                                   "A Windows hub serves only once Docker Desktop runs; a Linux hub is the better hub",
                                   optional=True))
    checks.append(ClusterCheck(bool(values.get("HUB_REGISTRY")), "a registry for the nodes' image",
                               "Optional: python scripts/admin.py nodes hub --registry <its Tailscale name>",
                               optional=True))
    return checks


@dataclass
class Setup:
    console: Console = field(default_factory=Console)
    root: Path = ROOT
    config: Callable[..., subprocess.CompletedProcess] = run_config
    build: Callable[[], None] = build_api_image
    descriptions: dict[str, str] = field(default_factory=dict)
    # Runs a program on the host, for the home cluster's checks.
    run: Callable[..., subprocess.CompletedProcess] = field(default=lambda command, **options: run_quietly(command))
    # Whether the server runs, and an announced restart of it, which applies
    # the settings and secrets.
    running: Callable[[], bool] = field(default=lambda: COMPOSE.ready())
    restart: Callable[[], int] = field(default=lambda: subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "server.py"), "restart", "1m"], cwd=ROOT).returncode)
    # Set when a setup is undone after a restart ran with it.
    restart_after_undo: bool = field(default=False, init=False)
    # Why the last check could not be read, as Docker Compose said it.
    unreadable: str = field(default="", init=False)

    @property
    def secrets_dir(self) -> Path:
        return self.root / "secrets"

    @property
    def style(self) -> Style:
        return self.console.style

    # --- Reading the server -----------------------------------------------------------

    def features(self) -> dict[str, dict]:
        result = self.config("features", "--json")
        if result.returncode != 0:
            raise SystemExit(f"Could not read the features:\n{host_paths(result.stderr.strip())}\n"
                             "Fix the configuration: python scripts/setup.py fix")
        return {feature["key"]: feature for feature in json.loads(result.stdout)}

    def requirements(self) -> dict[str, dict]:
        result = self.config("requirements", "--json")
        if result.returncode != 0:
            return {}
        return {section["name"]: section for section in json.loads(result.stdout)}

    def unknown(self, key: str, features: dict) -> bool:
        self.console.say(self.style(f"Unknown feature {key}.", "red") + f" Choose from: {', '.join(features)}")
        return False

    # --- Status -------------------------------------------------------------------------

    # How each feature's role on Kubernetes reads, and in which color.
    KUBERNETES_USES = {
        "needed": ("required", ("cyan",)),
        "optional": ("optional", ("dim",)),
        "unavailable": ("not used", ("yellow",)),
    }

    def kubernetes_use(self, feature: dict, where: str = "kubernetes") -> tuple[str, tuple[str, ...]]:
        use = feature.get(where, {}).get("use", "optional")
        return self.KUBERNETES_USES.get(use, (use, ()))

    @staticmethod
    def differs_on_kubernetes(feature: dict, where: str = "kubernetes") -> bool:
        """Whether the cluster (Kubernetes, or with where="cluster" a home
        cluster) has it otherwise than this machine: required there and off
        here, or on here and not used there."""
        use = feature.get(where, {}).get("use", "optional")
        return (use == "needed" and not feature["enabled"]) or (use == "unavailable" and feature["enabled"])

    def status(self, legend: bool = True, commands: bool = True) -> int:
        """The features, each with its state, and what is wrong. The menu
        shows the legend only the first time, and its own keys instead of the
        commands."""
        say, style = self.console.say, self.style
        report = self.report()
        if report is None and self.unreadable:
            say(style("The configuration cannot be read, so the server would not start:", "red", "bold"))
            self.console.wrap(f"! {self.unreadable}", "  ", "red")
            say(f"  Fix it: {style('python scripts/setup.py fix', 'bold')}")
            return 1
        if report is not None and not report["loaded"]:
            say(style("The configuration cannot be read, so the server would not start:", "red", "bold"))
            self.show_problems(report["errors"])
            say(f"  Fix it: {style('python scripts/setup.py fix', 'bold')}")
            return 1
        features = self.features()
        states = {}
        for key, feature in features.items():
            missing = Missing.of(feature)
            if not feature["enabled"]:
                states[key] = (missing, "off", ("dim",))
            elif missing.count:
                states[key] = (missing, f"on, {missing.count} missing", ("yellow", "bold"))
            else:
                states[key] = (missing, "on, ready", ("green",))
        title_width = max(len(feature["title"]) for feature in features.values())
        name_width = max(len(key) for key in features)
        state_width = max(len("Status"), *(len(state) for _, state, _ in states.values()))

        def row(number: str, title: str, name: str, state: str, cluster: str, kubernetes: str) -> str:
            return f"  {number:>2}  {title}  {name}  {state}  {cluster}  {kubernetes}".rstrip()

        # Every use is at most "required *" long.
        cluster_width = max(len("Home cluster"), len("required *"))
        say(style("Optional features", "bold") + style("  (a basic server needs none of them)", "dim"))
        say("")
        say(style(row("#", "Feature".ljust(title_width), "Name".ljust(name_width),
                      "Status".ljust(state_width), "Home cluster".ljust(cluster_width), "On Kubernetes"), "bold"))
        different = {"cluster": [], "kubernetes": []}
        for number, (key, feature) in enumerate(features.items(), 1):
            missing, state, state_styles = states[key]
            uses = {}
            for where in different:
                text, styles = self.kubernetes_use(feature, where)
                if self.differs_on_kubernetes(feature, where):
                    different[where].append(feature)
                    text += " *"
                uses[where] = (text, styles)
            title = feature["title"].ljust(title_width)
            say(row(str(number), style(title, "dim") if not feature["enabled"] else title,
                    style(key.ljust(name_width), "cyan"), style(state.ljust(state_width), *state_styles),
                    style(uses["cluster"][0].ljust(cluster_width), *uses["cluster"][1]),
                    style(uses["kubernetes"][0], *uses["kubernetes"][1])))
            missing.show(self.console, " " * 6)

        explained = [
            ("Status", "on or off in server.env, which the server on this machine runs with; "
                       "ready when nothing it needs is missing"),
            ("Home cluster", "required: must be on on the hub, for its nodes too; optional: your choice; "
                             "not used: must be off there (docs/cluster.md)"),
            ("On Kubernetes", "required: must be on in the cluster; optional: your choice; "
                              "not used: must be off there"),
        ]
        if legend:
            say("")
            for label, text in explained:
                self.console.labelled(label.ljust(15), text, "  ", "bold")
        for where, place in (("cluster", "in a home cluster"), ("kubernetes", "on Kubernetes")):
            if legend and different[where]:
                say("")
                say(f"  {style('*', 'bold')} Different {place} than here, and why:")
                for feature in different[where]:
                    self.console.labelled(f"{feature['key']:{name_width}}  ", feature[where]["why"], "    ", "cyan")
        general = [error for error in (report or {}).get("errors", []) if error["feature"] is None]
        if general:
            say("")
            say(style("Other problems, which keep the server from starting:", "red", "bold"))
            self.show_problems(general)
        say("")
        say(style("Worker", "bold") + "  " + worker_processor(self.root)[1])
        say("")
        self.show_cluster(features, brief=True)
        if not commands:
            return 0
        say("")
        for label, command in [("Set one up", "python scripts/setup.py <name>"),
                               ("Fix problems", "python scripts/setup.py fix"),
                               ("Test what is on", "python scripts/setup.py check [<name>]"),
                               ("Change a setting", "python scripts/setup.py set [<NAME>]"),
                               ("Turn one off", "python scripts/setup.py off <name>"),
                               ("Home cluster", "python scripts/setup.py cluster"),
                               ("Kubernetes", "python scripts/kube.py status")]:
            say(f"  {style(label.ljust(18), 'dim')}{command}")
        return 0

    # --- A home cluster (docs/cluster.md) ----------------------------------------------------

    def show_cluster(self, features: dict | None, brief: bool = False) -> list[ClusterCheck]:
        """What a home cluster needs here, and what is missing: on a node,
        the node's side; elsewhere, the hub's. Brief: a few lines for status."""
        say, style = self.console.say, self.style
        checks = cluster_checks(self.root, features, self.run)
        node = NODES.is_node(self.root)
        title = "Home cluster: this device is a node" if node else "Home cluster"
        ready = all(check.ok or check.optional for check in checks)
        hub = not node and bool(NODES.env_values(self.root / ".env").get("HUB_ADDRESS"))
        if brief and not (node or hub):
            say(style(title, "bold") + style("  (other devices run work for this server; not set up)", "dim"))
            return checks
        state = style("ready", "green") if ready else style("not ready", "yellow", "bold")
        say(style(title, "bold") + f"  {state}" + style("  (docs/cluster.md)", "dim"))
        for check in checks:
            if brief and (check.ok or check.optional):
                continue
            mark = style("ok  ", "green") if check.ok else style("--  ", "dim") if check.optional else style("no  ", "red")
            say(f"  {mark}{check.text}")
            if not check.ok and check.fix and not brief:
                self.console.wrap(check.fix, "        ", "dim")
        return checks

    def cluster(self) -> int:
        """Show what a home cluster needs; on a machine that is no hub yet,
        offer to make it one."""
        self.console.heading("== Home cluster ==")
        self.console.wrap("Your other devices, Linux or Windows, with a GPU or without, run work for this server over "
                          "Tailscale. This machine is the hub; the others join as nodes. See docs/cluster.md.")
        say = self.console.say
        say("")
        features = None
        if not NODES.is_node(self.root):
            # A node has no API image to ask; its settings are the hub's.
            try:
                features = self.features()
            except SystemExit:
                pass
        checks = self.show_cluster(features)
        if NODES.is_node(self.root):
            return 0 if all(check.ok or check.optional for check in checks) else 1
        values = NODES.env_values(self.root / ".env")
        addresses = tailscale_addresses(self.run)
        say("")
        if not values.get("HUB_ADDRESS") and addresses and self.console.yes(
                f"Make this machine the hub, on its Tailscale address {addresses[0]}?", default=False):
            arguments = ["nodes", "hub"]
            result = self.run([sys.executable, str(self.root / "scripts" / "admin.py"), *arguments])
            for line in (result.stdout or "").splitlines():
                say(f"  {line}")
            if result.returncode != 0:
                self.console.say(self.style((result.stderr or "").strip(), "red"))
                return 1
            say("")
            self.show_cluster(None)
        say("")
        for label, command in [("Apply on the hub", "python scripts/server.py restart 1m"),
                               ("Invite a device", "python scripts/admin.py nodes invite <name>"),
                               ("On the device", "python scripts/server.py join <name>.json"),
                               ("See every device", "python scripts/admin.py nodes")]:
            say(f"  {self.style(label.ljust(18), 'dim')}{command}")
        return 0

    # --- Setting a feature up --------------------------------------------------------------

    def set_up(self, key: str, chain: tuple[str, ...] = ()) -> bool:
        features = self.features()
        if key not in features:
            return self.unknown(key, features)
        feature = features[key]
        say, wrap, style = self.console.say, self.console.wrap, self.style
        self.console.heading(f"== {feature['title']} ==")
        wrap(feature["summary"])
        wrap(f"Guide: {feature['docs']}", "  ", "dim")

        for other in feature["excludes"]:
            if features[other]["enabled"]:
                wrap(f"It cannot be on together with {features[other]['title']}.", "  ", "yellow")
                if not self.console.yes(f"Turn {features[other]['title']} off?", default=False):
                    return False
                if not self.turn_off(other, confirm=False):
                    return False
        for other in feature["needs"]:
            if not features[other]["enabled"] and other not in chain:
                wrap(f"It needs {features[other]['title']}, which is off.", "  ", "yellow")
                if not self.console.yes(f"Set up {features[other]['title']} first?"):
                    return False
                if not self.set_up(other, (*chain, key)):
                    return False
                self.console.heading(f"== {feature['title']} (continued) ==")

        steps = [
            step for step, wanted in (
                ("External services", bool(feature["requirement_sections"])),
                ("Settings", bool(feature["settings"])),
                ("Secret files in ./secrets", bool(feature["secrets"])),
            ) if wanted
        ] + ["Check"]
        numbered = {step: f"Step {number} of {len(steps)}: {step}" for number, step in enumerate(steps, 1)}

        if "External services" in numbered and not self.external_services(feature, numbered["External services"]):
            return False
        # From here on the env files change. A setup that does not finish puts
        # them back, so the feature is not left half on.
        before = self.env_files()
        switch_secret = feature["off"]["delete_secret"]
        switch_secret_existed = bool(switch_secret) and (self.secrets_dir / switch_secret).exists()
        try:
            files = EnvFiles(self.root)
            if "Settings" in numbered:
                self.ask_settings(feature, files, numbered["Settings"])
            for name, value in feature["turn_on"].items():
                if name not in {setting["name"] for setting in feature["settings"]}:
                    files.of(name).set(name, value)
            for profile in feature["profiles"]:
                files.compose.add_profile(profile)
            files.save()
            if "Secret files in ./secrets" in numbered:
                self.ask_secrets(feature, numbered["Secret files in ./secrets"])
            ready = self.verify(key, numbered["Check"])
        except BaseException:
            self.roll_back(feature, before, switch_secret if not switch_secret_existed else None)
            raise
        if not ready:
            self.roll_back(feature, before, switch_secret if not switch_secret_existed else None)
            if self.restart_after_undo:
                self.restart_after_undo = False
                self.apply("Restart once more, so the server runs as before?")
        return ready

    def env_files(self) -> dict[Path, str | None]:
        """Both env files' text, to put back."""
        paths = (self.root / SERVER_ENV, self.root / COMPOSE_ENV)
        return {path: path.read_text(encoding="utf-8") if path.exists() else None for path in paths}

    def roll_back(self, feature: dict, env_before: dict, created_switch_secret: str | None) -> None:
        """Put the env files back as they were, and remove a secret file whose
        presence alone would turn the feature on. Other secret files stay for
        the next try: a key file by itself turns nothing on."""
        for path, text in env_before.items():
            if text is None:
                path.unlink(missing_ok=True)
            else:
                path.write_text(text, encoding="utf-8")
        if created_switch_secret:
            (self.secrets_dir / created_switch_secret).unlink(missing_ok=True)
        if feature["enabled"]:
            message = f"{feature['title']} keeps its previous settings: server.env and .env are back as they were."
        else:
            message = f"{feature['title']} is off again: server.env and .env are back as they were."
        if created_switch_secret:
            message += f" secrets/{created_switch_secret} is removed: by itself it would turn the feature on."
        if any(secret["name"] != created_switch_secret for secret in feature["secrets"]):
            message += " The other secret files you gave are kept for the next try."
        self.console.wrap(message, "  ", "yellow")

    def external_services(self, feature: dict, heading: str) -> bool:
        sections = self.requirements()
        say, wrap, style = self.console.say, self.console.wrap, self.style
        self.console.heading(heading)
        wrap("What the external services must provide. Where each is set changes from provider to provider "
             "and over time: look it up in the provider's current documentation, or give this list to an AI "
             "agent.", "  ", "dim")
        for name in feature["requirement_sections"]:
            section = sections.get(name)
            if section is None:
                continue
            say("")
            say(f"  {style(section['name'], 'bold')} {style('(' + section['provider'] + ')', 'dim')}")
            for requirement in section["requirements"]:
                checked = style("  [check --online tests it]", "cyan") if requirement.get("checked") else ""
                lines = textwrap.wrap(f"{requirement['item']}: {requirement['value']}", WIDTH - 6) or [""]
                item, _, rest = lines[0].partition(": ")
                say(f"    - {style(item + ':', 'bold')} {rest}{checked if len(lines) == 1 else ''}")
                for index, line in enumerate(lines[1:], 1):
                    say(f"      {line}{checked if index == len(lines) - 1 else ''}")
                wrap(f"why: {requirement['reason']}", "      ", "dim")
        say("")
        answer = self.console.ask("Press Enter when they are ready, or q to stop for now: ").strip().lower()
        return answer != "q"

    def ask_settings(self, feature: dict, files: "EnvFiles", heading: str) -> None:
        self.console.heading(heading)
        self.console.wrap("Enter keeps the value shown; - goes back to the default.", "  ", "dim")
        for setting in feature["settings"]:
            self.ask_setting(setting, files.of(setting["name"]), feature)

    def ask_setting(self, setting: dict, env: "EnvFile", feature: dict | None = None) -> None:
        """Ask for one setting until the server would accept its value, and
        put it in env, which the caller saves. With feature, its switch's
        values that turn it off are not offered."""
        say, style = self.console.say, self.style
        name = setting["name"]
        turn_on = feature["turn_on"] if feature else {}
        off_values = set(feature["turn_off"].values()) if feature else set()
        choices = [choice for choice in setting.get("choices") or [] if choice not in off_values]
        current = env.get(name)
        if current is None or current in off_values:
            current = turn_on.get(name) or _value_text(setting.get("value"))
        say("")
        place = style(f"  in {env.path.name}", "dim")
        say(f"  {style(name, 'bold')}{style('  (required)', 'yellow') if setting.get('required') else ''}{place}")
        if name in self.descriptions:
            self.console.wrap(self.descriptions[name], "  ", "dim")
        if choices:
            self.console.wrap(f"One of: {', '.join(choices)}", "  ", "cyan")
        shown = style(current, "cyan") if current is not None else style("unset", "dim")
        while True:
            answer = self.console.ask(f"  {name} [{shown}]: ").strip()
            if answer == "-":
                if setting.get("required") or name in turn_on:
                    say(style("  It is needed; give a value.", "yellow"))
                    continue
                env.unset(name)
                return
            value = answer or current
            if value is None:
                if setting.get("required"):
                    say(style("  It is needed; give a value.", "yellow"))
                    continue
                return
            if choices and value not in choices:
                say(style(f"  Choose one of: {', '.join(choices)}", "yellow"))
                continue
            # A value from the file may be the one that is wrong.
            problems = self.value_problems(name, value) if answer or value == env.get(name) else []
            if problems:
                for problem in problems:
                    self.console.wrap(f"! {problem}", "  ", "yellow")
                continue
            if answer or name in turn_on or env.get(name) is not None or setting.get("required"):
                env.set(name, value)
            return

    def value_problems(self, name: str, value: str) -> list[str]:
        """Why the server would not accept the value, as it reads it; none
        when it would, or when this image cannot say (check finds it then)."""
        result = self.config("validate", name, value, "--json")
        try:
            return list(json.loads(result.stdout)["problems"])
        except (ValueError, KeyError, TypeError):
            return []

    def secret_list(self, feature: dict) -> None:
        """Every secret file of the feature with its state, before they are asked."""
        style = self.style
        width = max(len(secret["name"]) for secret in feature["secrets"]) + len("secrets/")
        for secret in feature["secrets"]:
            if secret["present"]:
                state = style("present ", "green")
            elif secret["optional"]:
                state = style("optional", "dim")
            else:
                state = style("missing ", "yellow", "bold")
            path = style(f"secrets/{secret['name']}".ljust(width), "bold")
            self.console.say(f"  {state}  {path}  {style(secret['description'], 'dim')}")

    def ask_secrets(self, feature: dict, heading: str) -> None:
        self.console.heading(heading)
        self.console.wrap("Each is a file of its own, never shown on screen and never in an env file.", "  ", "dim")
        self.secret_list(feature)
        for secret in feature["secrets"]:
            self.ask_secret(secret)

    def ask_secret(self, secret: dict, again: bool = False) -> bool:
        """Ask for one secret file and write it. again: it exists but did not
        work, so Enter keeps it without asking first. Returns whether it was
        written."""
        say, wrap, style = self.console.say, self.console.wrap, self.style
        name = secret["name"]
        path = self.secrets_dir / name
        present = path.is_file() and path.stat().st_size > 0
        say("")
        say(f"  {style('secrets/' + name, 'bold')}{style('  (optional)', 'dim') if secret.get('optional') else ''}"
            f"  {style(secret.get('description', ''), 'dim')}".rstrip())
        if present and not again and self.console.yes("  It exists. Keep it?"):
            return False
        keep = "keep the one there" if present else "skip"
        if secret.get("kind") == "file":
            while True:
                source = self.console.ask(f"  Path to the downloaded file (Enter to {keep}): ").strip().strip('"')
                if not source:
                    return False
                text, problem = read_secret_source(Path(source).expanduser(), name)
                if problem:
                    wrap(problem, "  ", "yellow")
                    continue
                self.write_secret(path, text)
                break
        elif secret.get("kind") == "generate" and not again and self.console.yes("  Generate a random one?"):
            self.write_secret(path, secrets.token_urlsafe(32))
            wrap(f"Generated. Whoever needs it reads it from secrets/{name}.", "  ", "green")
            self.warn_if_tracked(path)
            return True
        else:
            value = clean_secret(self.console.ask_hidden(f"  Paste it (hidden; Enter to {keep}): "))
            if not value:
                return False
            self.write_secret(path, value)
        say(style(f"  Saved secrets/{name}.", "green"))
        self.warn_if_tracked(path)
        return True

    def write_secret(self, path: Path, value: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(clean_secret(value), encoding="utf-8")
        # Read by the services, which run as another user.
        path.chmod(0o644)

    def warn_if_tracked(self, path: Path) -> None:
        if shutil.which("git") is None:
            return
        result = subprocess.run(["git", "check-ignore", "-q", str(path)], cwd=self.root, capture_output=True)
        if result.returncode == 1:
            self.console.wrap(f"Warning: git does not ignore {path.relative_to(self.root)}. Never commit it.",
                              "  ", "red", "bold")

    # --- What is wrong, and asking again for it -------------------------------------------

    def report(self, *arguments: str) -> dict | None:
        """check --json: the errors and warnings, each with the settings and
        secret files it names, and with --online the services' answers."""
        result = self.config("check", "--json", *arguments)
        try:
            return json.loads(result.stdout)
        except ValueError:
            # Docker Compose itself cannot read an env file, or the image is old.
            lines = (result.stderr or "").strip().splitlines()
            self.unreadable = host_paths(lines[-1]) if lines else ""
            return None

    def catalogue(self) -> dict[str, dict]:
        """Every setting and secret file people choose, by name, as describe
        has it: its file, value, choices, and features."""
        result = self.config("describe", "--all", "--json")
        try:
            return {facts["name"]: facts for facts in json.loads(result.stdout)}
        except (ValueError, KeyError, TypeError):
            return {}

    def features_or_none(self) -> dict[str, dict] | None:
        try:
            return self.features()
        except SystemExit:
            return None

    def lookup(self, name: str, features: dict | None, catalogue: dict) -> tuple[str, dict, dict | None] | None:
        """How to ask for a name: ("setting" or "secret", what to ask with,
        its feature); None for a name people do not give."""
        for feature in (features or {}).values():
            for setting in feature["settings"]:
                if setting["name"] == name:
                    return "setting", setting, feature
            for secret in feature["secrets"]:
                if secret["name"] == name:
                    return "secret", secret, feature
        facts = catalogue.get(name)
        if facts is None:
            return None
        if facts["kind"] == "secret":
            description = _plain((facts.get("docs") or {}).get("description") or "")
            kind = "file" if name.endswith(".json") else "value"
            return "secret", {"name": name, "description": description, "kind": kind, "optional": True}, None
        if facts["kind"] != "setting":
            return None
        return "setting", {"name": name, "value": facts.get("value"), "choices": facts.get("choices"),
                           "required": False}, None

    def askable(self, names: list[str], features: dict | None = None, catalogue: dict | None = None) -> list[str]:
        catalogue = self.catalogue() if catalogue is None else catalogue
        return [name for name in dict.fromkeys(names) if self.lookup(name, features, catalogue)]

    def ask_names(self, names: list[str], again: bool = True) -> None:
        """Ask again for the settings and secret files named, as their
        features describe them, and save the settings."""
        features, catalogue = self.features_or_none(), self.catalogue()
        files = EnvFiles(self.root)
        for name in names:
            found = self.lookup(name, features, catalogue)
            if found is None:
                continue
            kind, item, feature = found
            if kind == "secret":
                self.ask_secret(item, again=again)
            else:
                self.ask_setting(item, files.of(name), feature)
        files.save()

    def show_problems(self, items: list[dict], indent: str = "  ", *styles: str) -> None:
        for item in items:
            text = host_paths(item["text"]).replace(f"{self.secrets_dir}/", "secrets/")
            self.console.wrap(f"! {text}", indent, *(styles or ("red",)))

    def feature_names(self, feature: dict) -> set[str]:
        return ({setting["name"] for setting in feature["settings"]} | {secret["name"] for secret in feature["secrets"]}
                | set(feature["turn_on"]))

    def error_names(self, error: dict) -> list[str]:
        """What to ask again for an error. A feature's messages start with its
        switch, as in "STORAGE_BUCKET needs the secret file ...": the switch
        is right, what it names after it is missing."""
        names, text = error["names"], error["text"]
        if len(names) > 1 and text.startswith(names[0]) and " needs " in text:
            return names[1:]
        # "X is required when <switch> is set": X is missing.
        if len(names) > 1 and text.startswith(f"{names[0]} is required when "):
            return names[:1]
        return names

    def errors_of(self, report: dict, key: str | None) -> list[dict]:
        """The errors that concern a feature: its own, those that name its
        settings, and all of them while the settings cannot even be read."""
        if key is None or not report["loaded"]:
            return report["errors"]
        features = self.features_or_none() or {}
        names = self.feature_names(features[key]) if key in features else set()
        return [error for error in report["errors"]
                if error["feature"] == key or (error["feature"] is None and names & set(error["names"]))]

    def fix_errors(self, key: str | None = None) -> bool:
        """Show what keeps the server, or with key that feature, from
        working, and ask again for what each problem names, until nothing is
        left or the user stops. Returns whether nothing is left."""
        say, style = self.console.say, self.style
        while True:
            report = self.report()
            if report is None:
                say(style("  Could not check the configuration.", "red"))
                return False
            errors = self.errors_of(report, key)
            if not errors:
                return True
            count = f"{len(errors)} problem{'s' if len(errors) != 1 else ''}"
            say(style(f"  {count} to fix:", "red", "bold"))
            self.show_problems(errors)
            names = self.askable([name for error in errors for name in self.error_names(error)])
            if not names:
                self.console.wrap("Fix them as they say, then run this again.", "  ")
                return False
            if not self.console.yes(f"Give {', '.join(names)} again now?"):
                return False
            self.ask_names(names)

    def show_results(self, results: list[dict]) -> None:
        style = self.style
        labels = {"ok": ("ok  ", "green"), "warn": ("warn", "yellow"), "fail": ("FAIL", "red"), "skip": ("skip", "dim")}
        width = max((len(result["check"]) for result in results), default=0)
        for result in results:
            label, color = labels.get(result["status"], (result["status"], "bold"))
            self.console.labelled(f"{style(label, color, 'bold')}  {result['check'].ljust(width)}  ", result["detail"],
                                  "  ")
            if result.get("hint"):
                self.console.wrap(f"-> {result['hint']}", "  " + " " * (width + 8), "yellow")

    def failure_names(self, failed: list[dict]) -> list[str]:
        """What to ask again for failures: what their hints name, or else
        everything of their features."""
        features, catalogue = self.features_or_none() or {}, self.catalogue()
        named = self.askable([name for result in failed for name in result["names"]], features, catalogue)
        if named:
            return named
        everything = [name for result in failed if result["feature"] in features
                      for name in sorted(self.feature_names(features[result["feature"]]))]
        return self.askable(everything, features, catalogue)

    def test_online(self, key: str | None = None, setup: bool = False) -> str:
        """Test the services with the real settings and secrets, and ask
        again for what a failure names, until they pass or the user stops.
        Returns passed, kept (failing, kept as it is), or undo (during a
        setup, which then puts the files back)."""
        say, style = self.console.say, self.style
        while True:
            say(style("  Testing with the real services...", "dim"))
            report = self.report("--online", *(["--feature", key] if key else []))
            if report is None or report["online"] is None:
                if report and report["errors"]:
                    self.show_problems(report["errors"])
                else:
                    say(style("  Could not run the test.", "red"))
                return "kept"
            results = report["online"]
            self.show_results(results)
            failed = [result for result in results if result["status"] == "fail"]
            if not failed:
                say(style("  The services answer as they should.", "green", "bold"))
                return "passed"
            names = self.failure_names(failed)
            options = []
            if names:
                self.console.wrap(f"To change: {', '.join(names)}", "  ", "bold")
                options.append("Enter changes them")
            options.append("t tests again (after a change at the provider)")
            options.append("u undoes the setup" if setup else "k keeps it as it is")
            if setup:
                options.append("k keeps it as it is")
            answer = self.console.ask(f"  {'; '.join(options)}: ").strip().lower()
            if not answer and names:
                self.ask_names(names)
                continue
            if answer.startswith("t"):
                continue
            if answer.startswith("u") and setup:
                return "undo"
            where = f" {key}" if key else ""
            self.console.wrap(f"Kept as it is, and not working yet. Test it again: python scripts/setup.py check{where}",
                              "  ", "yellow")
            return "kept"

    def apply(self, question: str = "Restart the server now, so it runs with this?") -> bool:
        """Offer an announced restart, which makes the services read the
        settings and secrets again. Returns whether it restarted."""
        if not self.running():
            self.console.wrap("The server runs with it from its next start: python scripts/server.py start", "  ")
            return False
        if not self.console.yes(f"{question} Clients see a notice a minute before.", default=False):
            self.console.wrap("Apply it later: python scripts/server.py restart 1m. Settings and secrets are read "
                              "when the services start.", "  ")
            return False
        return self.restart() == 0

    def verify(self, key: str, heading: str = "Check") -> bool:
        """Whether a feature set up works: nothing missing, then its services
        tested, asking again for what fails. False undoes the setup."""
        say, style = self.console.say, self.style
        self.console.heading(heading)
        if not self.fix_errors(key):
            say(style(f"  {self.features()[key]['title'] if self.features_or_none() else key} is not ready yet.",
                      "red", "bold"))
            say(f"  Run this again once it is: {style(f'python scripts/setup.py {key}', 'bold')}")
            return False
        feature = self.features()[key]
        missing = Missing.of(feature)
        if not feature["enabled"] or missing.count:
            say(style(f"  {feature['title']} is not ready yet.", "red", "bold"))
            missing.show(self.console, "  ")
            say(f"  Fix {'it' if missing.count == 1 else 'them'}, then run: "
                f"{style(f'python scripts/setup.py {key}', 'bold')}")
            return False
        say(style(f"  {feature['title']} is set up.", "green", "bold"))
        restarted = False
        if feature["profiles"]:
            # Its own service starts only with the stack, and the test reaches it.
            self.console.wrap("Its service starts with the next restart, and the test reaches it there.", "  ")
            restarted = self.apply()
            if not restarted:
                self.console.wrap(f"Test it once the server runs with it: python scripts/setup.py check {key}", "  ")
                return True
        elif not feature.get("online", True):
            self.apply()
            return True
        elif not self.console.yes(f"Test {in_sentence(feature['title'])} with the real services now?"):
            self.console.wrap(f"Test it later: python scripts/setup.py check {key}", "  ")
            self.apply()
            return True
        outcome = self.test_online(key, setup=True)
        if outcome == "undo":
            if restarted:
                self.console.wrap("The server runs with the setup being undone: restart it once more.", "  ", "yellow")
                self.restart_after_undo = True
            return False
        if not restarted:
            self.apply()
        return True

    # --- python scripts/setup.py check [feature] -------------------------------------------

    def check(self, key: str | None = None) -> int:
        """Test a feature that is set up, or every one, with the real
        services; ask again for what fails."""
        self.console.heading(f"== Check{': ' + key if key else ''} ==")
        if key is not None:
            features = self.features()
            if key not in features:
                return 0 if self.unknown(key, features) else 1
            if not features[key]["enabled"]:
                self.console.say(f"{features[key]['title']} is off. Set it up: python scripts/setup.py {key}")
                return 1
        if not self.fix_errors(key):
            return 1
        return 0 if self.test_online(key) == "passed" else 1

    # --- python scripts/setup.py fix --------------------------------------------------------

    def fix(self) -> int:
        """Find what is wrong with the env files, the secret files, and the
        settings, fix what a script can, and ask for the rest."""
        say, style = self.console.say, self.style
        self.console.heading("== Fixing the configuration ==")
        self.fix_env_files()
        report = self.report()
        if report is None:
            say(style("  Could not check the configuration: the API image does not answer.", "red"))
            return 1
        self.fix_secret_files(report["secrets"])
        self.fix_names(report)
        if not self.fix_errors():
            return 1
        report = self.report() or {"warnings": []}
        remaining = [warning for warning in report["warnings"] if (warning.get("fix") or {}).get("action") != "separate"]
        if remaining:
            say(style("  Works, but probably not as meant:", "yellow", "bold"))
            self.show_problems(remaining, "  ", "yellow")
        say(style("  The configuration has no errors.", "green", "bold"))
        if self.console.yes("Test every feature that is on with the real services now?"):
            if self.test_online() != "passed":
                return 1
        self.apply()
        return 0

    def fix_env_files(self) -> None:
        """Lines Docker Compose and the server would misread: a byte order
        mark, lines that are no setting, and settings written twice."""
        say, style = self.console.say, self.style
        for name in (SERVER_ENV, COMPOSE_ENV):
            path = self.root / name
            if not path.is_file():
                continue
            file = EnvFile(path)
            broken, duplicates = env_line_problems(file.lines)
            bom = path.read_bytes().startswith(b"\xef\xbb\xbf")
            if not (broken or duplicates or bom):
                continue
            say(style(f"  {name}:", "bold"))
            if bom:
                self.console.wrap("! It starts with a byte order mark (BOM), as some Windows editors write: Docker "
                                  "Compose reads it as part of the first setting's name.", "  ", "yellow")
            for index in broken:
                self.console.wrap(f"! Line {index + 1} is no setting (NAME=value): {_shortened(file.lines[index])}",
                                  "  ", "yellow")
            for setting, indexes in duplicates.items():
                lines = ", ".join(str(index + 1) for index in indexes)
                self.console.wrap(f"! {setting} is set on lines {lines}; only the last applies.", "  ", "yellow")
            if not self.console.yes(f"Fix {name}? Lines that are no setting and earlier copies are commented out, "
                                    "so nothing that applies changes"):
                continue
            for index in broken:
                file.lines[index] = f"# {file.lines[index]}  # not a setting; commented out by setup.py fix"
            for indexes in duplicates.values():
                for index in indexes[:-1]:
                    file.lines[index] = f"# {file.lines[index]}  # set again below; commented out by setup.py fix"
            file.save()
            say(style(f"  Fixed {name}.", "green"))

    def fix_secret_files(self, known: dict) -> None:
        """Secret files that are folders, as Docker creates for a missing
        file, that have a byte order mark, UTF-16 text, or spaces around the
        value, that the services cannot read, or that are named almost as
        one the server reads. Values are never shown."""
        say, wrap, style = self.console.say, self.console.wrap, self.style
        folder = self.secrets_dir
        if not folder.is_dir():
            return
        for name in [*known["given"], *known["managed"]]:
            path = folder / name
            if path.is_dir():
                self.remove_folder(path, f"secrets/{name}")
            elif path.is_file() and name in known["given"]:
                self.clean_secret_file(path, f"secrets/{name}")
        expected = {name.split("/")[0] for name in [*known["given"], *known["managed"], *known["aliases"]]}
        files = [name for name in known["given"] if "/" not in name]
        for entry in sorted(folder.iterdir()):
            if entry.name in expected or entry.name in known["managed_folders"] or entry.name.startswith("."):
                continue
            stem = re.sub(r"\.txt$", "", entry.name)
            close = difflib.get_close_matches(stem, files, n=1, cutoff=0.8)
            if not entry.is_file() or not close:
                continue
            target = folder / close[0]
            if target.exists():
                wrap(f"! secrets/{entry.name} is no file the server reads; secrets/{close[0]} is.", "  ", "yellow")
            elif self.console.yes(f"secrets/{entry.name} is no file the server reads. Is it secrets/{close[0]}? "
                                  "Rename it?"):
                entry.rename(target)
                say(style(f"  Renamed it to secrets/{close[0]}.", "green"))
        if shutil.which("git"):
            tracked = subprocess.run(["git", "ls-files", "secrets"], cwd=self.root, capture_output=True, text=True)
            for line in (tracked.stdout or "").splitlines():
                wrap(f"! git keeps {line}, a secret, in the repository: git rm --cached {line}, and replace the "
                     "secret, which its history still has.", "  ", "red", "bold")

    def remove_folder(self, path: Path, shown: str) -> None:
        wrap = self.console.wrap
        try:
            path.rmdir()
        except PermissionError:
            wrap(f"! {shown} is a folder, which Docker creates as root for a missing file: sudo rm -r {shown}, "
                 "then run this again.", "  ", "yellow")
            return
        except OSError:
            wrap(f"! {shown} is a folder with files in it, not a file: move it away.", "  ", "yellow")
            return
        wrap(f"Removed {shown}, an empty folder where its file belongs, as Docker creates for a missing file.",
             "  ", "green")

    def clean_secret_file(self, path: Path, shown: str) -> None:
        raw = path.read_bytes()
        text, reasons = decoded_secret(raw)
        if text is None:
            self.console.wrap(f"! {shown} is no text: write it again.", "  ", "yellow")
        elif reasons and self.console.yes(f"{shown} has {' and '.join(reasons)}, which the services would read as "
                                          "part of it. Remove them? The value is not shown"):
            path.write_text(text, encoding="utf-8")
            self.console.say(self.style(f"  Cleaned {shown}.", "green"))
        if os.name != "nt":
            status = path.stat()
            if not status.st_mode & 0o004 and status.st_uid != CONTAINER_UID and self.console.yes(
                    f"The services, which run as user {CONTAINER_UID}, cannot read {shown}. Let them?"):
                path.chmod(0o644)

    def fix_names(self, report: dict) -> None:
        """Renames and removals that check names: deprecated settings and
        typos in setting names, and secret files under an old name."""
        say, style = self.console.say, self.style
        files = EnvFiles(self.root)
        changed = False
        for item in [*report["errors"], *report["warnings"]]:
            fix = item.get("fix") or {}
            action, name = fix.get("action"), fix.get("name")
            if action == "rename_secret":
                if self.console.yes(f"{_sentence(item['text'])} Rename it?"):
                    (self.secrets_dir / name).rename(self.secrets_dir / fix["to"])
                    say(style(f"  Renamed secrets/{name} to secrets/{fix['to']}.", "green"))
                continue
            if action not in ("rename", "remove"):
                continue
            source = next((file for file in (files.server, files.compose) if file.indexes(name)), None)
            if source is None:
                # From the environment Docker was started with, which no file here sets.
                continue
            if action == "rename" and self.console.yes(f"{_sentence(item['text'])} Rename {name} to {fix['to']}?"):
                value = fix.get("value") or source.get(name) or ""
                source.take(name)
                files.of(fix["to"]).set(fix["to"], value)
                changed = True
            elif action == "remove" and self.console.yes(f"{_sentence(item['text'])} Comment it out?", default=False):
                source.unset(name, "  # commented out by setup.py fix")
                changed = True
        if changed:
            files.save()
            say(style("  Saved.", "green"))

    # --- python scripts/setup.py set [NAME] -------------------------------------------------

    def change(self, name: str | None = None) -> int | None:
        """Change any one setting or secret file, or a Kubernetes cluster's
        choice, checked as the server reads it."""
        say, style = self.console.say, self.style
        catalogue = self.catalogue()
        choices = self.cluster_choices()
        names = [*catalogue, *choices]
        if name is None:
            self.console.heading("== Change a setting ==")
            name = self.choose(catalogue, choices)
            if name is None:
                return None
        else:
            match = next((known for known in names if known.lower() == name.lower()), None)
            if match is None:
                close = difflib.get_close_matches(name.upper(), [known.upper() for known in names], n=3)
                hint = f" Did you mean {', '.join(close)}?" if close else ""
                say(style(f"Unknown setting {name}.", "red") + hint)
                return 1
            name = match
        if name in choices:
            return self.change_cluster_choice(name, choices[name])
        facts = catalogue[name]
        self.show_facts(facts)
        found = self.lookup(name, self.features_or_none(), catalogue)
        if found is None:
            return 1
        kind, item, _ = found
        before = self.env_files()
        secret_path = self.secrets_dir / name
        secret_before = secret_path.read_bytes() if kind == "secret" and secret_path.is_file() else None
        if kind == "secret":
            if not self.ask_secret(item):
                return 0
        else:
            files = EnvFiles(self.root)
            self.ask_setting(item, files.of(name))
            files.save()
        if self.env_files() == before and kind == "setting":
            say("  Nothing changed.")
            return 0
        report = self.report()
        related = [error for error in (report or {}).get("errors", [])
                   if name in error["names"] or not report["loaded"]]
        if related:
            say(style("  With it, the server would not start:", "red", "bold"))
            self.show_problems(related)
            if self.console.yes("Undo the change?"):
                if kind == "secret":
                    if secret_before is None:
                        secret_path.unlink(missing_ok=True)
                    else:
                        secret_path.write_bytes(secret_before)
                else:
                    for path, text in before.items():
                        if text is None:
                            path.unlink(missing_ok=True)
                        else:
                            path.write_text(text, encoding="utf-8")
                say("  Undone.")
                return 1
        else:
            say(style(f"  Saved {name}.", "green"))
        features = self.features_or_none() or {}
        for use in facts.get("features", []):
            feature = features.get(use["key"])
            if feature and feature["enabled"] and feature.get("online") and not feature["profiles"] and self.console.yes(
                    f"Test {in_sentence(feature['title'])} with the real services now?"):
                self.test_online(use["key"])
        self.apply()
        return 0

    def choose(self, catalogue: dict, choices: dict) -> str | None:
        """A setting found by part of its name or a word of its description."""
        say, style = self.console.say, self.style
        while True:
            query = self.console.ask("\nPart of a setting's name, or a word of what it does (Enter to stop): ")
            query = query.strip().lower()
            if not query:
                return None
            by_name = [name for name in [*catalogue, *choices] if query in name.lower()]
            by_text = [name for name in catalogue if name not in by_name
                       and query in self.descriptions.get(name, _plain((catalogue[name].get("docs") or {})
                                                                        .get("description") or "")).lower()]
            found = by_name + by_text
            if not found:
                say("  Nothing matches.")
                continue
            if len(found) == 1:
                return found[0]
            shown = found[:30]
            width = max(len(name) for name in shown)
            for number, name in enumerate(shown, 1):
                facts = catalogue.get(name, {})
                value = facts.get("value") if facts.get("kind") == "setting" else facts.get("value", "cluster.env")
                say(f"  {number:>2}  {name.ljust(width)}  {style(str(value if value is not None else ''), 'dim')}")
            if len(found) > len(shown):
                say(style(f"  and {len(found) - len(shown)} more: search for more of the name", "dim"))
            answer = self.console.ask("Number (Enter to search again): ").strip()
            if answer.isdigit() and 1 <= int(answer) <= len(shown):
                return shown[int(answer) - 1]

    def show_facts(self, facts: dict) -> None:
        say, style = self.console.say, self.style
        self.console.heading(facts["name"])
        where = facts.get("file", "")
        say(f"  {style(facts['kind'], 'dim')} in {where}" + (f", now {facts['value']}" if "value" in facts else ""))
        docs = facts.get("docs") or {}
        if docs.get("default"):
            say(style(f"  default: {_plain(docs['default'])}", "dim"))
        uses = [use["key"] for use in facts.get("features", [])]
        if uses:
            say(style(f"  used by: {', '.join(uses)}", "dim"))

    def cluster_choices(self) -> dict[str, str]:
        """A Kubernetes cluster's own choices (deploy/kubernetes/cluster.env)
        and their defaults; none without the deployment."""
        if not (self.root / KUBE_FOLDER / "cluster.env.example").is_file():
            return {}
        return dict(load_script("kube").CLUSTER_CHOICES)

    def change_cluster_choice(self, name: str, default: str) -> int:
        say, style = self.console.say, self.style
        kube = load_script("kube")
        path = self.root / KUBE_FOLDER / "cluster.env"
        example = path.with_name("cluster.env.example")
        file = EnvFile(path) if path.exists() else EnvFile(example).copy(path)
        self.console.heading(f"{name}  (deploy/kubernetes/cluster.env)")
        for line in comment_above(EnvFile(example).lines, name):
            self.console.wrap(line, "  ", "dim")
        current = file.get(name)
        shown = style(current, "cyan") if current else style(f"default: {default or 'none'}", "dim")
        while True:
            answer = self.console.ask(f"  {name} [{shown}]: ").strip()
            if not answer:
                return 0
            if answer == "-":
                file.unset(name)
                break
            values = {choice: file.get(choice) for choice in kube.CLUSTER_CHOICES}
            values[name] = answer
            try:
                kube.cluster_component(values)
            except ValueError as error:
                self.console.wrap(f"! {error}", "  ", "yellow")
                continue
            file.set(name, answer)
            break
        file.save()
        say(style(f"  Saved {name} in deploy/kubernetes/cluster.env.", "green")
            + " Apply it: python scripts/kube.py update")
        return 0

    # --- Turning a feature off -------------------------------------------------------------

    def turn_off(self, key: str, confirm: bool = True) -> bool:
        features = self.features()
        if key not in features:
            return self.unknown(key, features)
        feature = features[key]
        style, wrap = self.style, self.console.wrap
        if not feature["enabled"]:
            self.console.say(f"{feature['title']} is already off.")
            return True
        users = [features[other]["title"] for other in feature["needed_by"]]
        if users:
            wrap(f"{', '.join(users)} need{'s' if len(users) == 1 else ''} it; turn "
                 f"{'it' if len(users) == 1 else 'them'} off first.", "", "yellow")
            return False
        if confirm and not self.console.yes(f"Turn {feature['title']} off?", default=False):
            return False
        files = EnvFiles(self.root)
        actions = feature["off"]
        for name, value in actions["set"].items():
            files.of(name).set(name, value)
        for name in actions["unset"]:
            files.of(name).unset(name)
        for profile in actions["remove_profiles"]:
            files.compose.remove_profile(profile)
        files.save()
        secret = actions["delete_secret"]
        if secret and self.console.yes(f"Delete secrets/{secret}? It cannot be undone.", default=False):
            (self.secrets_dir / secret).unlink(missing_ok=True)
        elif secret:
            wrap(f"It stays on while secrets/{secret} exists; move the file away to turn it off.", "", "yellow")
        self.console.say(style(f"{feature['title']} is off.", "green")
                         + " Apply it: python scripts/server.py restart 1m.")
        return True

    # --- The menu ---------------------------------------------------------------------------

    def menu(self) -> int:
        first = True
        while True:
            if self.status(legend=first, commands=False) != 0:
                if not self.console.yes("\nFix the configuration now?"):
                    return 1
                self.fix()
                self.pause()
                continue
            first = False
            if not self.menu_choice():
                return 0

    def menu_choice(self) -> bool:
        """One choice of the menu, done; False to quit. The result stays on
        screen until Enter, before the menu shows again."""
        keys = list(self.features())
        key_style = lambda key: self.style(key, "bold")  # noqa: E731
        while True:
            self.console.say("")
            self.console.say(f"  {key_style(f'1-{len(keys)}')}  sets a feature up, tests it, or turns it off")
            self.console.say(f"  {key_style('f')} fixes the configuration, {key_style('c')} tests what is on, "
                             f"{key_style('s')} changes any setting")
            answer = self.console.ask("Choose, or Enter to quit: ").strip().lower()
            if not answer:
                return False
            if answer in ("f", "c", "s"):
                done = {"f": self.fix, "c": self.check, "s": self.change}[answer]()
                if done is None:
                    # Nothing chosen: straight back, nothing to read.
                    continue
                break
            if answer.isdigit() and 1 <= int(answer) <= len(keys):
                key = keys[int(answer) - 1]
                if not self.features()[key]["enabled"]:
                    self.set_up(key)
                    break
                choice = self.console.ask(f"{self.features()[key]['title']} is on. Set it up again (s), test it "
                                          "(c), turn it off (o), or Enter to go back? ").strip().lower()
                if not choice:
                    continue
                if choice.startswith("o"):
                    self.turn_off(key)
                elif choice.startswith("c"):
                    self.check(key)
                elif choice.startswith("s"):
                    self.set_up(key)
                else:
                    continue
                break
            self.console.say(self.style(f"  {answer!r} is none of these.", "yellow"))
        self.pause()
        return True

    def pause(self) -> None:
        self.console.ask(self.style("\nPress Enter to go back to the menu. ", "dim"))


def prepare(console: Console, root: Path = ROOT) -> None:
    """.env, server.env, and the generated passwords, as a start creates
    them, with every line in its file: the API container that answers needs
    them."""
    for message in SERVER_SCRIPT.prepare_files(root):
        console.say(message)


def main(arguments: list[str], setup: Setup | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python scripts/setup.py",
        description="Set up the server's optional features, or turn them off. A basic server needs none.",
    )
    parser.add_argument("command", nargs="?",
                        help="status, fix, check, set, off, cluster (a home cluster), or a feature's name; "
                             "nothing for a menu")
    parser.add_argument("feature", nargs="?",
                        help="with off: the feature to turn off; with check: the one to test; with set: the setting")
    args = parser.parse_args(arguments)
    # A node of a home cluster has none of the hub's files, and gets none: it
    # takes the server's settings from the hub.
    on_node = NODES.is_node(setup.root if setup else ROOT)
    if args.command == "cluster" or on_node:
        if on_node and args.command not in (None, "status", "cluster"):
            print("This device is a node of a home cluster: set features up on the hub. See: "
                  "python scripts/setup.py cluster", file=sys.stderr)
            return 1
        setup = setup or Setup(console=Console(style=Style.for_terminal()), descriptions=setting_descriptions(),
                               config=run_config if shutil.which("docker") else
                               (lambda *arguments, **options: subprocess.CompletedProcess(arguments, 1, "", "")))
        return setup.cluster()
    if setup is None:
        if shutil.which("docker") is None:
            print("The docker CLI is needed: it runs the server's own configuration check.", file=sys.stderr)
            return 1
        session = ConfigSession()
        setup = Setup(console=Console(style=Style.for_terminal()), descriptions=setting_descriptions(),
                      config=session)
        try:
            prepare(setup.console)
        except RuntimeError as error:
            # A secret file Docker made a folder of, as root.
            print(error, file=sys.stderr)
            return 1
        setup.console.say(setup.style(
            "Bringing the API image up to this code (the first time takes a few minutes)...", "dim"
        ))
        setup.build()
        try:
            return run_command(setup, args, parser)
        finally:
            session.close()
    return run_command(setup, args, parser)


def run_command(setup: Setup, args, parser) -> int:
    if args.command is None:
        return setup.menu()
    if args.command == "status":
        return setup.status()
    if args.command == "fix":
        return setup.fix()
    if args.command == "check":
        return setup.check(args.feature)
    if args.command == "set":
        return setup.change(args.feature) or 0
    if args.command == "off":
        if not args.feature:
            parser.error("off needs the feature to turn off")
        return 0 if setup.turn_off(args.feature) else 1
    return 0 if setup.set_up(args.command) else 1


def _shown(value) -> str:
    """A value as an env file writes it."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, dict)):
        return json.dumps(value, separators=(",", ":"))
    return str(value)


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except (KeyboardInterrupt, EOFError):
        # Ctrl+C, or the end of piped answers: a setup puts its files back.
        print("\nStopped.")
        sys.exit(130)
