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
.env, keeping its comments; secrets are asked without showing them and
written to ./secrets, and never printed.
"""

import argparse
import getpass
import importlib.util
import json
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
ADDED_HEADER = "# --- Added by scripts/setup.py"
WIDTH = 78


def load_server_script():
    """scripts/server.py, for the files a start creates."""
    spec = importlib.util.spec_from_file_location("server_script", Path(__file__).with_name("server.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- .env, edited in place ---------------------------------------------------------

class EnvFile:
    """.env as lines, so comments and order survive. A setting is changed on
    its own line, or on its commented example line from .env.example, which
    stays in its block; a new one goes at the end."""

    def __init__(self, path: Path):
        self.path = path
        self.lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []

    def _find(self, name: str, commented: bool) -> int | None:
        pattern = re.compile(rf"^#\s?{re.escape(name)}=" if commented else rf"^{re.escape(name)}=")
        return next((index for index, line in enumerate(self.lines) if pattern.match(line)), None)

    def get(self, name: str) -> str | None:
        index = self._find(name, commented=False)
        if index is None:
            return None
        return _unquote(self.lines[index].split("=", 1)[1].strip())

    def set(self, name: str, value: str) -> None:
        line = f"{name}={_quote(value)}"
        index = self._find(name, commented=False)
        if index is None:
            index = self._find(name, commented=True)
        if index is not None:
            self.lines[index] = line
            return
        if ADDED_HEADER not in self.lines:
            self.lines += ["", ADDED_HEADER]
        self.lines.append(line)

    def unset(self, name: str) -> None:
        """Comment the setting out, so its default applies again."""
        index = self._find(name, commented=False)
        if index is not None:
            self.lines[index] = f"# {self.lines[index]}"

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

    def save(self) -> None:
        self.path.write_text("\n".join(self.lines) + "\n", encoding="utf-8")


def _quote(value: str) -> str:
    # Compose and python-dotenv end a value at " #", and trim spaces.
    if " #" in value or value != value.strip():
        return "'" + value.replace("'", "\\'") + "'"
    return value


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


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
    """python -m app.config in a one-off API container, which reads .env and
    ./secrets as the services do. The first run builds the API image."""
    command = ["docker", "compose", "run", "--rm", "--no-deps", "-T", "api", "python", "-m", "app.config", *arguments]
    return subprocess.run(command, cwd=ROOT, capture_output=capture, text=True)


def build_api_image() -> None:
    subprocess.run(["docker", "compose", "build", "api"], cwd=ROOT, check=False)


@dataclass
class Console:
    """Input and output, replaced in tests."""

    ask: Callable[[str], str] = input
    ask_hidden: Callable[[str], str] = getpass.getpass
    say: Callable[[str], None] = print

    def wrap(self, text: str, indent: str = "  ") -> None:
        self.say(textwrap.fill(text, WIDTH, initial_indent=indent, subsequent_indent=indent))

    def yes(self, question: str, default: bool = True) -> bool:
        answer = self.ask(f"{question} [{'Y/n' if default else 'y/N'}] ").strip().lower()
        return default if not answer else answer.startswith("y")


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

    # --- Reading the server -----------------------------------------------------------

    def features(self) -> dict[str, dict]:
        result = self.config("features", "--json")
        if result.returncode != 0 and "invalid choice" in result.stderr:
            # An API image built before the features command.
            self.console.say("The API image is older than this code; building it...")
            self.build()
            result = self.config("features", "--json")
        if result.returncode != 0:
            raise SystemExit(f"Could not read the features:\n{result.stderr.strip()}")
        return {feature["key"]: feature for feature in json.loads(result.stdout)}

    def requirements(self) -> dict[str, dict]:
        result = self.config("requirements", "--json")
        if result.returncode != 0:
            return {}
        return {section["name"]: section for section in json.loads(result.stdout)}

    # --- Status -------------------------------------------------------------------------

    def status(self) -> int:
        features = self.features()
        say = self.console.say
        say("Optional features (a basic server needs none):")
        for number, feature in enumerate(features.values(), 1):
            state = "on " if feature["enabled"] else "off"
            say(f"  {number:2}. {state}  {feature['title']}  [{feature['key']}]")
            for problem in feature["problems"]:
                self.console.wrap(f"! {problem}", indent="            ")
        return 0

    # --- Setting a feature up --------------------------------------------------------------

    def set_up(self, key: str, chain: tuple[str, ...] = ()) -> bool:
        features = self.features()
        if key not in features:
            self.console.say(f"Unknown feature {key}; choose from: {', '.join(features)}")
            return False
        feature = features[key]
        say, wrap = self.console.say, self.console.wrap
        say("")
        say(f"== {feature['title']} ==")
        wrap(feature["summary"])
        wrap(f"Guide: {feature['docs']}")

        for other in feature["excludes"]:
            if features[other]["enabled"]:
                wrap(f"It cannot be on together with {features[other]['title']}.")
                if not self.console.yes(f"Turn {features[other]['title']} off?", default=False):
                    return False
                if not self.turn_off(other, confirm=False):
                    return False
        for other in feature["needs"]:
            if not features[other]["enabled"] and other not in chain:
                wrap(f"It needs {features[other]['title']}, which is off.")
                if not self.console.yes(f"Set up {features[other]['title']} first?"):
                    return False
                if not self.set_up(other, (*chain, key)):
                    return False
                say("")
                say(f"== {feature['title']} (continued) ==")

        if not self.external_services(feature):
            return False
        env = EnvFile(self.root / ".env")
        self.ask_settings(feature, env)
        for name, value in feature["turn_on"].items():
            if name not in {setting["name"] for setting in feature["settings"]}:
                env.set(name, value)
        for profile in feature["profiles"]:
            env.add_profile(profile)
        env.save()
        self.ask_secrets(feature)
        return self.verify(key)

    def external_services(self, feature: dict) -> bool:
        if not feature["requirement_sections"]:
            return True
        sections = self.requirements()
        say, wrap = self.console.say, self.console.wrap
        say("")
        wrap("What the external services must provide. Where each is set changes from provider to "
             "provider and over time: look it up in the provider's current documentation, or give this list "
             "to an AI agent.", indent="")
        for name in feature["requirement_sections"]:
            section = sections.get(name)
            if section is None:
                continue
            say("")
            say(f"{section['name']} ({section['provider']})")
            for requirement in section["requirements"]:
                checked = "  [check --online tests it]" if requirement.get("checked") else ""
                wrap(f"- {requirement['item']}: {requirement['value']}{checked}")
                wrap(f"why: {requirement['reason']}", indent="      ")
        say("")
        answer = self.console.ask("Press Enter when they are ready, or q to stop for now: ").strip().lower()
        return answer != "q"

    def ask_settings(self, feature: dict, env: EnvFile) -> None:
        if not feature["settings"]:
            return
        say = self.console.say
        say("")
        say("Settings, written to .env. Enter keeps the value shown; - goes back to the default.")
        off_values = set(feature["turn_off"].values())
        for setting in feature["settings"]:
            name = setting["name"]
            choices = [choice for choice in setting["choices"] or [] if choice not in off_values]
            current = env.get(name)
            if current is None or current in off_values:
                current = feature["turn_on"].get(name) or (
                    None if setting["value"] in (None, [], {}, "") else _shown(setting["value"])
                )
            say("")
            say(f"{name}{'  (required)' if setting['required'] else ''}")
            if name in self.descriptions:
                self.console.wrap(self.descriptions[name])
            if choices:
                self.console.wrap(f"One of: {', '.join(choices)}")
            while True:
                answer = self.console.ask(f"  {name} [{current if current is not None else 'unset'}]: ").strip()
                if answer == "-":
                    if setting["required"] or name in feature["turn_on"]:
                        say("  It is needed; give a value.")
                        continue
                    env.unset(name)
                    break
                value = answer or current
                if value is None:
                    if setting["required"]:
                        say("  It is needed; give a value.")
                        continue
                    break
                if choices and value not in choices:
                    say(f"  Choose one of: {', '.join(choices)}")
                    continue
                if answer or name in feature["turn_on"] or env.get(name) is not None or setting["required"]:
                    env.set(name, value)
                break

    def ask_secrets(self, feature: dict) -> None:
        if not feature["secrets"]:
            return
        say, wrap = self.console.say, self.console.wrap
        say("")
        say("Secrets, written to ./secrets and never shown.")
        for secret in feature["secrets"]:
            path = self.secrets_dir / secret["name"]
            say("")
            say(f"secrets/{secret['name']}{'  (optional)' if secret['optional'] else ''}")
            wrap(secret["description"])
            if secret["present"] and self.console.yes("  It exists. Keep it?"):
                continue
            if secret["kind"] == "file":
                source = self.console.ask("  Path to the downloaded file (Enter to skip): ").strip().strip('"')
                if not source:
                    continue
                if not Path(source).is_file():
                    say(f"  {source} is not a file; skipped. Run this again to add it.")
                    continue
                self.write_secret(path, Path(source).read_text(encoding="utf-8"))
            elif secret["kind"] == "generate" and self.console.yes("  Generate a random one?"):
                self.write_secret(path, secrets.token_urlsafe(32))
                wrap(f"Generated. Whoever needs it reads it from secrets/{secret['name']}.")
            else:
                value = self.console.ask_hidden("  Paste it (hidden; Enter to skip): ").strip()
                if not value:
                    continue
                self.write_secret(path, value)
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
            self.console.wrap(f"Warning: git does not ignore {path.relative_to(self.root)}. Never commit it.")

    def verify(self, key: str) -> bool:
        feature = self.features()[key]
        say, wrap = self.console.say, self.console.wrap
        say("")
        if not feature["enabled"] or feature["problems"]:
            say(f"{feature['title']} is not ready yet:")
            for problem in feature["problems"] or ["it is still off; run this again"]:
                wrap(f"- {problem}")
            wrap(f"Fix them, then run: python scripts/setup.py {key}")
            return False
        say(f"{feature['title']} is set up.")
        if feature["requirement_sections"] and self.console.yes(
            "Test the external services now (check --online)?", default=False
        ):
            self.config("check", "--online", capture=False)
        wrap("Apply it: python scripts/server.py restart 1m on a running server, or python scripts/server.py "
             "start. Settings and secrets are read when the services start.", indent="")
        return True

    # --- Turning a feature off -------------------------------------------------------------

    def turn_off(self, key: str, confirm: bool = True) -> bool:
        features = self.features()
        if key not in features:
            self.console.say(f"Unknown feature {key}; choose from: {', '.join(features)}")
            return False
        feature = features[key]
        if not feature["enabled"]:
            self.console.say(f"{feature['title']} is already off.")
            return True
        users = [features[other]["title"] for other in feature["needed_by"]]
        if users:
            self.console.wrap(f"{', '.join(users)} need{'s' if len(users) == 1 else ''} it; turn "
                              f"{'it' if len(users) == 1 else 'them'} off first.", indent="")
            return False
        if confirm and not self.console.yes(f"Turn {feature['title']} off?", default=False):
            return False
        env = EnvFile(self.root / ".env")
        actions = feature["off"]
        for name, value in actions["set"].items():
            env.set(name, value)
        for name in actions["unset"]:
            env.unset(name)
        for profile in actions["remove_profiles"]:
            env.remove_profile(profile)
        env.save()
        secret = actions["delete_secret"]
        if secret and self.console.yes(f"Delete secrets/{secret}? It cannot be undone.", default=False):
            (self.secrets_dir / secret).unlink(missing_ok=True)
        elif secret:
            self.console.wrap(f"It stays on while secrets/{secret} exists; move the file away to turn it off.",
                              indent="")
        self.console.wrap(f"{feature['title']} is off in .env. Apply it: python scripts/server.py restart 1m.",
                          indent="")
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
    """.env and the generated passwords, as a start creates them: the API
    container that answers needs them."""
    for message in load_server_script().prepare_files(root):
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
        setup = Setup(descriptions=setting_descriptions())
        prepare(setup.console)
        setup.console.say("Reading the server's features (the first time builds the API image, a few minutes)...")
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
    """A value as .env writes it."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, dict)):
        return json.dumps(value, separators=(",", ":"))
    return str(value)


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        print("\nStopped; .env keeps what was saved.")
        sys.exit(130)
