from pathlib import Path

import pytest
import yaml

from app import config as config_module
from app import online_check
from app.config import configuration_warnings, load_settings, validate_settings
from app.requirements import build_requirements

ROOT = Path(__file__).resolve().parent.parent


def load(tmp_path, text="", token=None):
    env_file = tmp_path / ".env"
    env_file.write_text(text, encoding="utf-8")
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir(exist_ok=True)
    if token is not None:
        (secrets_dir / "cloudflare").mkdir(exist_ok=True)
        (secrets_dir / "cloudflare" / "tunnel_token").write_text(token, encoding="utf-8")
    return load_settings(str(env_file), str(secrets_dir))


# --- Configuration ------------------------------------------------------------

@pytest.mark.parametrize(
    ("profiles", "enabled"),
    [("", False), ("cloudflare", True), ("other, cloudflare", True), ("*", True), ("cloudflared", False)],
)
def test_compose_profiles_turn_the_tunnel_on(tmp_path, profiles, enabled):
    assert load(tmp_path, f"COMPOSE_PROFILES={profiles}\n").tunnel.enabled is enabled


def test_tunnel_needs_its_token(tmp_path):
    [problem] = validate_settings(load(tmp_path, "COMPOSE_PROFILES=cloudflare\n"), "all")

    assert "cloudflare/tunnel_token" in problem
    assert validate_settings(load(tmp_path, "COMPOSE_PROFILES=cloudflare\n", token="eyJ"), "all") == []


def test_empty_token_is_missing(tmp_path):
    assert validate_settings(load(tmp_path, "COMPOSE_PROFILES=cloudflare\n", token=""), "all")


def test_docker_created_directory_is_not_a_token(tmp_path):
    (tmp_path / "secrets" / "cloudflare" / "tunnel_token").mkdir(parents=True)

    assert validate_settings(load(tmp_path, "COMPOSE_PROFILES=cloudflare\n"), "all")


def test_missing_token_does_not_stop_the_services(tmp_path):
    # Only cloudflared reads it; the API keeps serving locally.
    settings = load(tmp_path, "COMPOSE_PROFILES=cloudflare\n")

    for role in ("api", "worker", "remote", "cleanup"):
        assert validate_settings(settings, role) == []


def test_token_is_not_needed_without_the_tunnel(tmp_path):
    assert validate_settings(load(tmp_path), "all") == []


@pytest.mark.parametrize(("value", "stored"), [("Piano.Example.com", "piano.example.com"), ("piano.example.com.", "piano.example.com")])
def test_hostname_is_normalised(tmp_path, value, stored):
    assert load(tmp_path, f"CLOUDFLARE_TUNNEL_HOSTNAME={value}\n").tunnel.hostname == stored


@pytest.mark.parametrize("value", ["https://piano.example.com", "piano.example.com/api", "localhost", "piano example.com"])
def test_hostname_must_be_bare(tmp_path, value):
    with pytest.raises(config_module.ConfigError, match="CLOUDFLARE_TUNNEL_HOSTNAME"):
        load(tmp_path, f"CLOUDFLARE_TUNNEL_HOSTNAME={value}\n")


def test_misspelled_tunnel_setting_is_reported(tmp_path, caplog):
    load(tmp_path, "CLOUDFLARE_TUNNEL_HOSTANME=piano.example.com\n")

    assert "did you mean CLOUDFLARE_TUNNEL_HOSTNAME?" in caplog.text


def test_open_server_behind_the_tunnel_is_warned_about(tmp_path):
    settings = load(tmp_path, "COMPOSE_PROFILES=cloudflare\n", token="eyJ")

    warnings = configuration_warnings(settings, {"API_BIND_ADDRESS": "0.0.0.0"})

    assert len(warnings) == 2
    assert "neither a password nor sign-in" in warnings[0]
    assert "API_BIND_ADDRESS=127.0.0.1" in warnings[1]
    # Unset, Compose publishes on 127.0.0.1.
    assert not any("Port 8000" in warning for warning in configuration_warnings(settings, {}))


def test_protected_server_behind_the_tunnel_has_no_warnings(tmp_path):
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "server_password").write_text("a-long-password-123", encoding="utf-8")
    settings = load(tmp_path, "COMPOSE_PROFILES=cloudflare\n", token="eyJ")

    assert configuration_warnings(settings, {"API_BIND_ADDRESS": "127.0.0.1"}) == []


def test_no_warnings_without_the_tunnel(tmp_path):
    assert configuration_warnings(load(tmp_path), {}) == []


# --- Online check -------------------------------------------------------------

class Response:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class Http:
    """Answers by URL; an exception as the answer is raised."""

    def __init__(self, answers):
        self.answers = answers
        self.urls = []

    def get(self, url, timeout):
        self.urls.append(url)
        answer = self.answers[url]
        if isinstance(answer, Exception):
            raise answer
        return answer


READY = online_check.TUNNEL_READY_URL
CONNECTED = Response(200, {"status": 200, "readyConnections": 4})


@pytest.fixture
def tunnel(default_settings):
    default_settings.tunnel.compose_profiles = "cloudflare"
    default_settings.tunnel.hostname = "piano.example.com"
    return default_settings


def test_connected_tunnel_reaching_this_server(tunnel):
    http = Http({READY: CONNECTED, "https://piano.example.com/health": Response(200, {"status": "ok"})})

    results = online_check.check_tunnel(tunnel, http)

    assert [result.status for result in results] == ["ok", "ok"]
    assert "4 connections" in results[0].detail


def test_cloudflared_not_running(tunnel):
    [result] = online_check.check_tunnel(tunnel, Http({READY: ConnectionError("no such host")}))

    assert result.status == "fail"
    assert "docker compose run" in result.hint


@pytest.mark.parametrize("response", [Response(503, {"status": 503, "readyConnections": 0}), Response(200, {})])
def test_tunnel_not_connected_to_cloudflare(tunnel, response):
    [result] = online_check.check_tunnel(tunnel, Http({READY: response}))

    assert result.status == "fail"
    assert "token" in result.hint


def test_public_address_not_checked_without_hostname(tunnel):
    tunnel.tunnel.hostname = None

    results = online_check.check_tunnel(tunnel, Http({READY: CONNECTED}))

    assert results[1].status == "warn"
    assert "CLOUDFLARE_TUNNEL_HOSTNAME" in results[1].detail


@pytest.mark.parametrize(
    "health",
    [Response(502), Response(403), Response(200), Response(200, {"status": "something else"})],
    ids=["origin unreachable", "challenge", "not json", "another server"],
)
def test_public_address_not_routed_to_the_api(tunnel, health):
    http = Http({READY: CONNECTED, "https://piano.example.com/health": health})

    result = online_check.check_tunnel(tunnel, http)[1]

    assert result.status == "fail"
    assert "http://api:8000" in result.hint


def test_public_address_that_does_not_resolve(tunnel):
    http = Http({READY: CONNECTED, "https://piano.example.com/health": ConnectionError("no DNS")})

    result = online_check.check_tunnel(tunnel, http)[1]

    assert result.status == "fail"
    assert "DNS" in result.hint


def test_public_address_keeps_the_password(tunnel):
    tunnel.secrets.server_password = "a-long-password-123"
    answers = {
        READY: CONNECTED,
        "https://piano.example.com/health": Response(200, {"status": "ok"}),
        "https://piano.example.com/api/server-info": Response(401),
    }

    assert online_check.check_tunnel(tunnel, Http(answers))[1].status == "ok"

    answers["https://piano.example.com/api/server-info"] = Response(200, {})
    result = online_check.check_tunnel(tunnel, Http(answers))[1]
    assert result.status == "fail"
    assert "without the server password" in result.detail


def test_tunnel_is_checked_only_when_enabled(default_settings, monkeypatch):
    monkeypatch.setattr(online_check, "check_tunnel", lambda config: [online_check.Result("Cloudflare Tunnel", "ok", "")])

    def tunnel_result(config):
        return next(result for result in online_check.run_checks(config) if result.name == "Cloudflare Tunnel")

    assert tunnel_result(default_settings).status == "skip"
    default_settings.tunnel.compose_profiles = "cloudflare"
    assert tunnel_result(default_settings).status == "ok"


# --- Requirements -------------------------------------------------------------

def tunnel_requirements(config):
    [section] = [section for section in build_requirements(config) if section.name == "Cloudflare Tunnel"]
    return section


def test_requirements_follow_the_configuration(tunnel):
    tunnel.limits.max_upload_size_mb = 100
    section = tunnel_requirements(tunnel)
    values = {requirement.item: requirement.value for requirement in section.requirements}

    assert section.enabled
    assert values["Public hostname"] == "piano.example.com, sent to the service http://api:8000"
    assert values["Request size"].startswith("request bodies up to 100 MB")


def test_requirements_without_the_tunnel(default_settings):
    section = tunnel_requirements(default_settings)

    assert not section.enabled
    assert "<set CLOUDFLARE_TUNNEL_HOSTNAME>" in {requirement.value.split(",")[0] for requirement in section.requirements}


# --- Compose ------------------------------------------------------------------

def compose():
    return yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))


def test_cloudflared_runs_only_with_its_profile():
    service = compose()["services"]["cloudflared"]

    assert service["profiles"] == [config_module.TUNNEL_PROFILE]
    # Outbound only: nothing is published on the host.
    assert "ports" not in service


def test_cloudflared_is_pinned_and_reads_only_its_token():
    service = compose()["services"]["cloudflared"]

    assert service["image"].split(":")[1][0].isdigit()
    assert service["volumes"] == ["./secrets/cloudflare:/run/secrets/cloudflare:ro"]
    command = service["command"]
    assert command[command.index("--token-file") + 1] == "/run/secrets/" + config_module.TUNNEL_TOKEN_FILE
    assert "--token" not in command


def test_readiness_address_matches_the_online_check():
    command = compose()["services"]["cloudflared"]["command"]
    port = command[command.index("--metrics") + 1].rsplit(":", 1)[1]

    assert online_check.TUNNEL_READY_URL == f"http://cloudflared:{port}/ready"


def test_other_services_never_get_the_profile():
    services = compose()["services"]

    # Only cloudflared has the tunnel's profile; the admin page has its own.
    assert {name: service["profiles"] for name, service in services.items() if service.get("profiles")} == {
        "cloudflared": ["cloudflare"],
        "admin": ["admin"],
    }
