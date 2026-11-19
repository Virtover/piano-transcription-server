"""The configuration's problems as data, for scripts/setup.py and agents:
`check --json`, `validate`, and `session`.

Each problem names the settings and secret files it is about, so a setup can
ask for exactly those again, and some carry a fix a script can make, such as
renaming a deprecated setting. `session` answers the commands of
`python -m app.config` in one container, reading the env files of a mounted
checkout as they change, so a setup does not start a container per question.
"""

import contextlib
import io
import json
import logging
import os
import re
import sys
import traceback
from pathlib import Path
from typing import Callable, get_args

from dotenv import dotenv_values
from pydantic import ValidationError

from app import config as app_config

# Where scripts/stacks.py mounts the checkout for a session, read-only.
SESSION_ROOT = "/setup"
SETTING_NAME = re.compile(r"\b[A-Z][A-Z0-9_]*[A-Z0-9]\b")
# Secret files that scripts write, never people: scripts/server.py start
# creates the passwords, admin.py nodes hub the hub bucket's keys.
MANAGED_SECRETS = ("postgres_password", "cluster_storage_access_key", "cluster_storage_secret_key")
# Folders in the secrets folder that scripts manage, file by file.
MANAGED_SECRET_FOLDERS = ("redis", "admin-web", "node")
# Settings that are wiring, set by docker-compose.yml or the Kubernetes
# manifests rather than chosen: a setup does not offer to change them.
WIRING_GROUPS = ("service",)
WIRING_SETTINGS = ("CLOUDFLARE_TUNNEL_DEPLOYED",)


# --- Names in a message ---------------------------------------------------------

def setting_names() -> set[str]:
    return (set(app_config.known_variables()) | app_config.COMPOSE_ONLY_VARIABLES | set(app_config.ALIASES)
            | set(app_config.DEPRECATED))


def secret_names() -> list[str]:
    """Every secret file people give, by its path in the secrets folder."""
    names = [app_config.env_name(app_config.SecretSettings, field) for field in app_config.SecretSettings.model_fields]
    names.append(app_config.TUNNEL_TOKEN_FILE)
    return [name for name in names if name not in MANAGED_SECRETS]


def mentioned_names(text: str) -> list[str]:
    """The settings and secret files a message names, in its order."""
    known = setting_names()
    found = [(match.start(), match.group()) for match in SETTING_NAME.finditer(text) if match.group() in known]
    for name in [*secret_names(), *MANAGED_SECRETS, *app_config.SECRET_ALIASES]:
        # Not inside a longer name: storage_access_key is not in cluster_storage_access_key.
        match = re.search(rf"(?<![\w.-]){re.escape(name)}(?![\w-])", text)
        if match:
            found.append((match.start(), name))
    return list(dict.fromkeys(name for _, name in sorted(found)))


def problem(text: str, feature: str | None = None, fix: dict | None = None) -> dict:
    item = {"text": text, "names": mentioned_names(text), "feature": feature}
    if fix:
        item["fix"] = fix
    return item


# --- check --json ---------------------------------------------------------------

def name_problems(environment: dict[str, str], secrets_dir: str | None) -> tuple[list[dict], list[dict]]:
    """Errors and warnings about names: deprecated and unknown settings, and
    secret files under an old name, each with the rename that fixes it."""
    errors, warnings = [], []
    for old, alias in app_config.ALIASES.items():
        if old not in environment:
            continue
        if alias.new_name in environment:
            errors.append(problem(f"{old} and {alias.new_name} are both set; remove {old}",
                                  fix={"action": "remove", "name": old}))
        else:
            warnings.append(problem(f"{old} is deprecated; use {alias.new_name} instead", fix={
                "action": "rename", "name": old, "to": alias.new_name, "value": alias.convert(environment[old])}))
    for name, advice in app_config.DEPRECATED.items():
        if name in environment:
            warnings.append(problem(f"{name} is deprecated; {advice}"))
    for name, suggestion in app_config.unknown_variables(environment):
        if suggestion and suggestion not in environment and suggestion not in app_config.ALIASES:
            warnings.append(problem(f"Unknown setting {name}; did you mean {suggestion}?",
                                    fix={"action": "rename", "name": name, "to": suggestion}))
        else:
            hint = f"; did you mean {suggestion}?" if suggestion else ""
            warnings.append(problem(f"Unknown setting {name}{hint}", fix={"action": "remove", "name": name}))
    folder = Path(secrets_dir) if secrets_dir else None
    for old, new in app_config.SECRET_ALIASES.items():
        if folder is None or not (folder / old).is_file():
            continue
        if (folder / new).is_file():
            errors.append(problem(f"The secret files {old} and {new} both exist; remove {old}"))
        else:
            warnings.append(problem(f"The secret file {old} is deprecated; rename it to {new}",
                                    fix={"action": "rename_secret", "name": old, "to": new}))
    return errors, warnings


def report(env_file, secrets_dir: str | None, online: bool = False, features: list[str] | None = None,
           run_checks: Callable | None = None) -> dict:
    """What `check` finds, as data: errors that keep the server from
    starting, warnings, and with online the services' answers."""
    environment = app_config.read_environment(env_file)
    errors, warnings = name_problems(environment, secrets_dir)
    said = {item["text"] for item in errors}
    config = None
    try:
        config = app_config.load_settings(env_file, secrets_dir)
    except app_config.ConfigError as error:
        errors += [problem(text) for text in error.problems if text not in said]
    if config is not None:
        from app.features import FEATURES, feature_problems

        owner: dict[str, str] = {}
        for feature in FEATURES:
            for text in feature_problems(feature, config, "all"):
                owner.setdefault(text, feature.key)
        errors += [problem(text, owner.get(text)) for text in app_config.validate_settings(config, "all")]
        warnings += [problem(text) for text in app_config.configuration_warnings(config, environment)]
        warnings += [problem(text, fix={"action": "separate"}) for text in app_config.misplaced_settings(env_file)]
    results = None
    if online and config is not None and not errors:
        if run_checks is None:
            from app.online_check import run_checks
        results = [
            {"check": result.name, "status": result.status, "detail": result.detail, "hint": result.hint,
             "feature": result.feature, "names": mentioned_names(f"{result.detail} {result.hint or ''}")}
            for result in (run_checks(config, features) if features else run_checks(config))
        ]
    return {
        "ok": not errors and not any(result["status"] == "fail" for result in results or []),
        "loaded": config is not None,
        "errors": errors,
        "warnings": warnings,
        "online": results,
        "secrets": {"given": secret_names(), "managed": list(MANAGED_SECRETS),
                    "managed_folders": list(MANAGED_SECRET_FOLDERS), "aliases": dict(app_config.SECRET_ALIASES)},
    }


# --- validate -------------------------------------------------------------------

def validate_value(name: str, value: str, env_file) -> dict:
    """Whether a value would be accepted for a setting, as the services read
    it: from the environment, over the env files. Only its own group's rules
    apply; check finds what other settings make of it."""
    variables = app_config.known_variables()
    result = {"name": name, "ok": True, "problems": [],
              "file": app_config.COMPOSE_ENV_FILE if name in app_config.COMPOSE_ONLY_VARIABLES else app_config.SERVER_ENV_FILE}
    if name in app_config.ALIASES:
        return {**result, "ok": False, "problems": [f"{name} is deprecated; use {app_config.ALIASES[name].new_name}"]}
    if name not in variables:
        if name not in app_config.COMPOSE_VARIABLES:
            return {**result, "ok": False, "problems": [f"Unknown setting {name}"]}
        # Docker Compose reads it, which says when a value is wrong.
        return result
    group_name, field = variables[name]
    group = app_config.GROUPS[group_name]
    before = os.environ.get(name)
    os.environ[name] = value
    try:
        group(_env_file=tuple(app_config.env_file_paths(env_file)) or None, _secrets_dir=None)
    except ValidationError as error:
        problems = app_config._format_errors(group, error, {})
        # This setting's, and the group's rules across its settings; not
        # another setting that is wrong already.
        own = [text for text, item in zip(problems, error.errors())
               if not item["loc"] or str(item["loc"][0]) in (field, name, name.lower())]
        if own:
            result = {**result, "ok": False, "problems": [text.removeprefix(f"{name}: ") for text in own]}
    finally:
        if before is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = before
    return result


# --- describe --all -------------------------------------------------------------

def setting_choices(name: str) -> list[str] | None:
    variables = app_config.known_variables()
    if name not in variables:
        return None
    group_name, field = variables[name]
    info = app_config.GROUPS[group_name].model_fields[field]
    if "Literal" not in str(info.annotation):
        return None
    return [str(choice) for choice in get_args(info.annotation) if choice is not None] or None


def all_setting_names() -> list[str]:
    """Every setting and secret file people choose, for a setup to offer."""
    variables = app_config.known_variables()
    names = [name for name, (group, _) in variables.items()
             if group not in WIRING_GROUPS and name not in WIRING_SETTINGS]
    names += sorted(app_config.COMPOSE_VARIABLES - {"COMPOSE_FILE", "COMPOSE_PATH_SEPARATOR"})
    return [*names, *secret_names()]


# --- session --------------------------------------------------------------------

class _CurrentStderr(logging.StreamHandler):
    """Logs to whatever sys.stderr is now, so each answer has its warnings."""

    @property
    def stream(self):
        return sys.stderr

    @stream.setter
    def stream(self, value):
        pass


def forget_environment(paths: list[str]) -> None:
    """Remove what Compose gave the container from the env files when it
    started, so the files are read as they are now. Wiring, which
    docker-compose.yml sets over them, differs and stays."""
    for path in paths:
        if not Path(path).is_file():
            continue
        for name, value in dotenv_values(path).items():
            if value is not None and os.environ.get(name) == value:
                del os.environ[name]


def serve(root: str, main: Callable[[list[str]], int], stdin=None, stdout=None) -> int:
    """Answer commands of python -m app.config, one JSON line each
    ({"args": [...]}), with one JSON line each: returncode, stdout, stderr.
    Every command reads root's env files, as they are when it runs."""
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    files = [str(Path(root) / name) for name in app_config.ENV_FILES]
    unreadable = [path for path in files if Path(path).exists() and not os.access(path, os.R_OK)]
    if unreadable:
        # Docker Compose reads the env files as the host's user, the services'
        # user may not: ended before any answer, scripts/setup.py asks each
        # question in a container of its own, which Compose gives the files.
        print(f"cannot read {', '.join(unreadable)} as user {getattr(os, 'getuid', lambda: '?')()}", file=sys.stderr)
        return 3
    forget_environment(files)
    docs = Path(root) / "docs" / "configuration.md"
    if docs.is_file():
        app_config.CONFIGURATION_DOCS = docs
    logging.basicConfig(level=logging.WARNING, format="warning: %(message)s", handlers=[_CurrentStderr()])
    for line in stdin:
        if not line.strip():
            continue
        try:
            arguments = [str(argument) for argument in json.loads(line)["args"]]
        except (ValueError, KeyError, TypeError):
            stdout.write(json.dumps({"returncode": 2, "stdout": "", "stderr": "not a request"}) + "\n")
            stdout.flush()
            continue
        if arguments[:1] == ["session"]:
            arguments = ["--help"]
        if "--env-file" not in arguments and arguments[:1] != ["--help"]:
            arguments += [part for path in files for part in ("--env-file", path)]
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            try:
                with contextlib.suppress(app_config.ConfigError):
                    # Modules that read app.config.settings get the files as they are.
                    app_config.settings = app_config.load_settings(files)
                code = main(arguments)
            except SystemExit as error:
                code = error.code if isinstance(error.code, int) else 1
            except Exception:  # noqa: BLE001
                errors.write(traceback.format_exc())
                code = 1
        stdout.write(json.dumps({"returncode": code, "stdout": output.getvalue(), "stderr": errors.getvalue()}) + "\n")
        stdout.flush()
    return 0
