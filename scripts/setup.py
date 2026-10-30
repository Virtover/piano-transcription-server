"""Set up the server's optional features, step by step, and turn them off.

    python scripts/setup.py                   # what is on, then a menu
    python scripts/setup.py status            # what is on and what is missing
    python scripts/setup.py outsourcing       # set one feature up
    python scripts/setup.py off outsourcing   # turn one off

A basic server needs none of this: python scripts/server.py start runs it.

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
        self.server.save(mode_from=self.compose.path)
        self.compose.save()


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


@dataclass
class Setup:
    console: Console = field(default_factory=Console)
    root: Path = ROOT
    config: Callable[..., subprocess.CompletedProcess] = run_config
    build: Callable[[], None] = build_api_image
    descriptions: dict[str, str] = field(default_factory=dict)

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
            raise SystemExit(f"Could not read the features:\n{result.stderr.strip()}")
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
        "needed": ("needed", ("cyan", "bold")),
        "optional": ("optional", ("dim",)),
        "unavailable": ("not available", ("red",)),
    }

    def kubernetes_tag(self, feature: dict) -> str:
        use = feature.get("kubernetes", {}).get("use", "optional")
        text, styles = self.KUBERNETES_USES.get(use, (use, ()))
        return self.style(text.ljust(13), *styles)

    def status(self) -> int:
        features = self.features()
        say, style = self.console.say, self.style
        say(style("Optional features", "bold") + style("  (a basic server needs none)", "dim"))
        width = max(len(feature["title"]) for feature in features.values())
        say(style(f"  {'':4}{'here':5}{'':{width}}  {'name':16} {'on Kubernetes':13}", "dim"))
        for number, feature in enumerate(features.values(), 1):
            missing = Missing.of(feature)
            if not feature["enabled"]:
                state, tag = style("off", "dim"), ""
            elif missing.count:
                state, tag = style("on ", "green"), style(f"{missing.count} missing", "yellow", "bold")
            else:
                state, tag = style("on ", "green"), style("ready", "green")
            title = feature["title"].ljust(width)
            title = style(title, "dim") if not feature["enabled"] else title
            say(f"  {number:2}. {state}  {title}  {style(feature['key'].ljust(16), 'cyan')} "
                f"{self.kubernetes_tag(feature)} {tag}".rstrip())
            missing.show(self.console, " " * 12)
        say(style("  Set one up: python scripts/setup.py <name>; turn one off: python scripts/setup.py off <name>",
                  "dim"))
        say(style("  On Kubernetes: needed ones must be on there, not available ones off; "
                  "python scripts/kube.py status checks the cluster.", "dim"))
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
        say, style = self.console.say, self.style
        self.console.heading(heading)
        self.console.wrap("Enter keeps the value shown; - goes back to the default.", "  ", "dim")
        off_values = set(feature["turn_off"].values())
        for setting in feature["settings"]:
            name = setting["name"]
            env = files.of(name)
            choices = [choice for choice in setting["choices"] or [] if choice not in off_values]
            current = env.get(name)
            if current is None or current in off_values:
                current = feature["turn_on"].get(name) or (
                    None if setting["value"] in (None, [], {}, "") else _shown(setting["value"])
                )
            say("")
            place = style(f"  in {env.path.name}", "dim")
            say(f"  {style(name, 'bold')}{style('  (required)', 'yellow') if setting['required'] else ''}{place}")
            if name in self.descriptions:
                self.console.wrap(self.descriptions[name], "  ", "dim")
            if choices:
                self.console.wrap(f"One of: {', '.join(choices)}", "  ", "cyan")
            shown = style(current, "cyan") if current is not None else style("unset", "dim")
            while True:
                answer = self.console.ask(f"  {name} [{shown}]: ").strip()
                if answer == "-":
                    if setting["required"] or name in feature["turn_on"]:
                        say(style("  It is needed; give a value.", "yellow"))
                        continue
                    env.unset(name)
                    break
                value = answer or current
                if value is None:
                    if setting["required"]:
                        say(style("  It is needed; give a value.", "yellow"))
                        continue
                    break
                if choices and value not in choices:
                    say(style(f"  Choose one of: {', '.join(choices)}", "yellow"))
                    continue
                if answer or name in feature["turn_on"] or env.get(name) is not None or setting["required"]:
                    env.set(name, value)
                break

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
        say, wrap, style = self.console.say, self.console.wrap, self.style
        self.console.heading(heading)
        wrap("Each is a file of its own, never shown on screen and never in an env file.", "  ", "dim")
        self.secret_list(feature)
        for secret in feature["secrets"]:
            path = self.secrets_dir / secret["name"]
            say("")
            say(f"  {style('secrets/' + secret['name'], 'bold')}{style('  (optional)', 'dim') if secret['optional'] else ''}")
            if secret["present"] and self.console.yes("  It exists. Keep it?"):
                continue
            if secret["kind"] == "file":
                source = self.console.ask("  Path to the downloaded file (Enter to skip): ").strip().strip('"')
                if not source:
                    continue
                if not Path(source).is_file():
                    say(style(f"  {source} is not a file; skipped. Run this again to add it.", "yellow"))
                    continue
                self.write_secret(path, Path(source).read_text(encoding="utf-8"))
            elif secret["kind"] == "generate" and self.console.yes("  Generate a random one?"):
                self.write_secret(path, secrets.token_urlsafe(32))
                wrap(f"Generated. Whoever needs it reads it from secrets/{secret['name']}.", "  ", "green")
                self.warn_if_tracked(path)
                continue
            else:
                value = self.console.ask_hidden("  Paste it (hidden; Enter to skip): ").strip()
                if not value:
                    continue
                self.write_secret(path, value)
            say(style(f"  Saved secrets/{secret['name']}.", "green"))
            self.warn_if_tracked(path)

    def write_secret(self, path: Path, value: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value.strip(), encoding="utf-8")
        # Read by the services, which run as another user.
        path.chmod(0o644)

    def warn_if_tracked(self, path: Path) -> None:
        if shutil.which("git") is None:
            return
        result = subprocess.run(["git", "check-ignore", "-q", str(path)], cwd=self.root, capture_output=True)
        if result.returncode == 1:
            self.console.wrap(f"Warning: git does not ignore {path.relative_to(self.root)}. Never commit it.",
                              "  ", "red", "bold")

    def verify(self, key: str, heading: str = "Check") -> bool:
        feature = self.features()[key]
        say, wrap, style = self.console.say, self.console.wrap, self.style
        self.console.heading(heading)
        missing = Missing.of(feature)
        if not feature["enabled"] or missing.count:
            say(style(f"  {feature['title']} is not ready yet.", "red", "bold"))
            missing.show(self.console, "  ")
            say(f"  Fix {'it' if missing.count == 1 else 'them'}, then run: "
                f"{style(f'python scripts/setup.py {key}', 'bold')}")
            return False
        say(style(f"  {feature['title']} is set up.", "green", "bold"))
        if feature["requirement_sections"] and self.console.yes(
            "Test the external services now (check --online)?", default=False
        ):
            self.config("check", "--online", capture=False)
        wrap("Apply it: python scripts/server.py restart 1m on a running server, or python scripts/server.py "
             "start. Settings and secrets are read when the services start.", "  ")
        return True

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
        while True:
            self.status()
            keys = list(self.features())
            answer = self.console.ask("\nA number to set up or turn off, or Enter to quit: ").strip()
            if not answer:
                return 0
            if not answer.isdigit() or not 1 <= int(answer) <= len(keys):
                continue
            key = keys[int(answer) - 1]
            if self.features()[key]["enabled"]:
                choice = self.console.ask("It is on. Set it up again (s) or turn it off (o)? ").strip().lower()
                if choice.startswith("o"):
                    self.turn_off(key)
                    continue
                if not choice.startswith("s"):
                    continue
            self.set_up(key)


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
    parser.add_argument("command", nargs="?", help="status, off, or a feature's name; nothing for a menu")
    parser.add_argument("feature", nargs="?", help="with off: the feature to turn off")
    args = parser.parse_args(arguments)
    if setup is None:
        if shutil.which("docker") is None:
            print("The docker CLI is needed: it runs the server's own configuration check.", file=sys.stderr)
            return 1
        setup = Setup(console=Console(style=Style.for_terminal()), descriptions=setting_descriptions())
        prepare(setup.console)
        setup.console.say(setup.style(
            "Bringing the API image up to this code (the first time takes a few minutes)...", "dim"
        ))
        setup.build()
    if args.command is None:
        return setup.menu()
    if args.command == "status":
        return setup.status()
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
    except KeyboardInterrupt:
        print("\nStopped.")
        sys.exit(130)
