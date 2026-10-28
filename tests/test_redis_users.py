"""Redis users: who may do what (redis/users.acl), how Redis gets them
(redis/start.sh and Compose), how the services sign in, and the online
check. fakeredis does not enforce a user's rules, so what Redis itself
allows was checked against a real Redis; check --online repeats that on
the live stack."""

import importlib.util
import re
from pathlib import Path

import pytest
from redis.exceptions import AuthenticationError, ConnectionError, NoPermissionError, ResponseError

from app import config, online_check

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "server.py"
spec = importlib.util.spec_from_file_location("server_script_for_redis", SCRIPT)
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)

# What would let a client destroy data, read or change Redis's settings or
# users, write its files, load code, or copy the data elsewhere.
DANGEROUS = {
    "flushall", "flushdb", "swapdb", "config", "acl", "debug", "shutdown", "module", "function", "script|flush",
    "save", "bgsave", "bgrewriteaof", "replicaof", "slaveof", "sync", "psync", "failover", "migrate", "restore",
    "monitor", "cluster",
}
# Dramatiq's Lua dispatcher runs KEYS, and Lua scripts run with the caller's rules.
NEEDED_BY_THE_SERVICES = {"keys", "eval", "evalsha", "script"}


def compose():
    import yaml

    return yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))


def acl_users():
    users = {}
    for line in (ROOT / "redis" / "users.acl").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            keyword, name, *rules = line.split()
            assert keyword == "user"
            users[name] = rules
    return users


# --- The users -----------------------------------------------------------------

def test_connecting_without_a_password_is_refused():
    assert acl_users()["default"] == ["off", "resetpass", "resetkeys", "resetchannels", "-@all"]


def test_every_user_has_a_password_that_server_py_creates():
    users = acl_users()

    assert set(users) - {"default"} == set(server.REDIS_USERS)
    for name in server.REDIS_USERS:
        assert users[name][:2] == ["on", f"#{{{name}}}"]
        # A password only as a hash; never nopass or a plain one.
        assert "nopass" not in users[name] and not any(rule.startswith(">") for rule in users[name])


def test_the_services_cannot_run_dangerous_commands():
    rules = acl_users()["app"]

    assert "+@all" in rules
    denied = {rule[1:] for rule in rules if rule.startswith("-")}
    assert denied == DANGEROUS
    assert not denied & NEEDED_BY_THE_SERVICES


def test_the_start_script_is_read_by_linux():
    for path in (ROOT / "redis" / "start.sh", ROOT / "redis" / "users.acl"):
        assert b"\r" not in path.read_bytes(), path
    assert "redis/* text eol=lf" in (ROOT / ".gitattributes").read_text(encoding="utf-8")


# --- Compose -------------------------------------------------------------------

def test_the_services_sign_in_as_app():
    wiring = compose()["x-wiring"]

    assert wiring["REDIS_URL"] == "redis://app@redis:6379/0"
    assert wiring["REDIS_PASSWORD_FILE"] == "/run/redis/password"


@pytest.mark.parametrize("name", ["api", "worker", "remote-worker"])
def test_services_with_secrets_see_only_their_own_redis_password(name):
    volumes = compose()["services"][name]["volumes"]

    assert "./secrets/redis/app:/run/redis/password:ro" in volumes
    # secrets/ is mounted whole, so the folder with the other passwords is
    # covered by an empty one.
    assert "./secrets:/run/secrets:ro" in volumes
    assert {"type": "tmpfs", "target": "/run/secrets/redis"} in volumes


def test_cleanup_gets_its_redis_password_and_no_other_secret():
    volumes = compose()["services"]["cleanup"]["volumes"]

    assert sorted(volumes) == ["./secrets/redis/app:/run/redis/password:ro", "data:/data"]


def test_only_redis_sees_every_redis_password():
    services = compose()["services"]
    mounting = [
        name for name, service in services.items()
        if any(isinstance(volume, str) and volume.startswith("./secrets/redis:") for volume in service.get("volumes", []))
    ]

    assert mounting == ["redis"]
    redis = services["redis"]
    assert redis["entrypoint"] == ["/bin/sh", "/redis/start.sh"]
    assert "./redis:/redis:ro" in redis["volumes"]
    assert "--user operator" in redis["healthcheck"]["test"][1]


# --- Signing in ----------------------------------------------------------------

def test_the_password_comes_from_its_file(default_settings, tmp_path):
    password = tmp_path / "password"
    password.write_text("s3cret\n", encoding="utf-8")

    assert default_settings.service.redis_options() == {}
    default_settings.service.redis_password_file = str(password)
    assert default_settings.service.redis_options() == {"password": "s3cret"}

    default_settings.service.redis_password_file = str(tmp_path / "missing")
    with pytest.raises(RuntimeError, match="server.py start creates it"):
        default_settings.service.redis_options()


def test_check_reports_a_missing_redis_password(default_settings, tmp_path):
    default_settings.service.redis_password_file = str(tmp_path / "missing")

    problems = config.validate_settings(default_settings, role="cleanup")

    assert any("Redis password file" in problem for problem in problems)


def call_arguments(text: str, start: int) -> str:
    """The text between the parenthesis at start and its match."""
    depth = 0
    for position in range(start, len(text)):
        depth += {"(": 1, ")": -1}.get(text[position], 0)
        if depth == 0:
            return text[start + 1:position]
    raise AssertionError("unbalanced call")


def test_every_redis_client_signs_in():
    calls = []
    for path in (ROOT / "app").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"Redis\.from_url\(", text):
            calls.append((path.name, "redis_options()" in call_arguments(text, match.end() - 1)))
    assert len(calls) >= 8 and all(signed for _, signed in calls), calls
    # Dramatiq ignores a password beside a URL, so it gets a client.
    tasks = (ROOT / "app" / "worker" / "tasks.py").read_text(encoding="utf-8")
    assert "RedisBroker(\n    url=" not in tasks


# --- check --online ------------------------------------------------------------

class FakeClient:
    def __init__(self, url, answers):
        self.url = url
        self.answers = answers

    def ping(self):
        answer = self.answers["anonymous" if "@" not in self.url else "app"]
        if isinstance(answer, Exception):
            raise answer
        return True

    def config_get(self, name):
        answer = self.answers["config"]
        if isinstance(answer, Exception):
            raise answer
        return {name: "0"}


def check(default_settings, tmp_path, **answers):
    password = tmp_path / "password"
    password.write_text("s3cret", encoding="utf-8")
    default_settings.service.redis_url = "redis://app@redis:6379/0"
    default_settings.service.redis_password_file = str(password)
    connections = []

    def connect(url, **options):
        connections.append((url, options.get("password")))
        return FakeClient(url, answers)

    return online_check.check_redis(default_settings, connect), connections


def test_online_check_passes_when_redis_refuses_strangers(default_settings, tmp_path):
    result, connections = check(
        default_settings, tmp_path,
        anonymous=AuthenticationError("Authentication required."),
        app=True,
        config=NoPermissionError("User app has no permissions to run the 'config|get' command"),
    )

    assert result.status == "ok"
    assert connections == [("redis://redis:6379/0", None), ("redis://app@redis:6379/0", "s3cret")]


def test_online_check_fails_when_redis_has_no_users(default_settings, tmp_path):
    result, _ = check(default_settings, tmp_path, anonymous=True, app=True, config=None)

    assert result.status == "fail" and "without a password" in result.detail


def test_online_check_fails_when_the_services_may_change_the_configuration(default_settings, tmp_path):
    result, _ = check(
        default_settings, tmp_path,
        anonymous=ResponseError("NOAUTH Authentication required."), app=True, config=None,
    )

    assert result.status == "fail" and "configuration" in result.detail
    assert "force-recreate redis" in result.hint


def test_online_check_when_redis_cannot_be_reached(default_settings, tmp_path):
    result, _ = check(default_settings, tmp_path, anonymous=ConnectionError("refused"), app=True, config=None)

    assert result.status == "fail" and "cannot reach Redis" in result.detail


def test_online_check_without_redis_users_warns(default_settings):
    result = online_check.check_redis(default_settings, connect=lambda *args, **kwargs: pytest.fail("connected"))

    assert result.status == "warn"
