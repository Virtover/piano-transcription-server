import re
from pathlib import Path

import pytest

from app import config

ROOT = Path(__file__).resolve().parent.parent
SERVICE_DOCKERFILES = ["Dockerfile.api", "Dockerfile.worker"]
# Video sites change often; an old yt-dlp stops working.
UNPINNED = {"yt-dlp", "yt-dlp-ejs"}


def requirement_lines(name):
    for line in (ROOT / name).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith(("#", "-r")):
            yield line


@pytest.mark.parametrize("name", ["requirements.api.txt", "requirements.worker.txt", "requirements.runpod.txt", "requirements.test.txt"])
def test_requirements_are_pinned(name):
    unpinned = [
        line for line in requirement_lines(name)
        if "==" not in line and re.split(r"[\[<>=]", line)[0] not in UNPINNED
    ]
    assert unpinned == []


@pytest.mark.parametrize("dockerfile", SERVICE_DOCKERFILES)
def test_services_do_not_run_as_root(dockerfile):
    text = (ROOT / dockerfile).read_text(encoding="utf-8")

    assert f"useradd --uid {config.CONTAINER_UID}" in text
    assert "chown app:app /data /billing" in text
    # USER comes after everything that needs root.
    assert text.rstrip().splitlines()[-3] == "USER app"


@pytest.mark.parametrize("dockerfile", SERVICE_DOCKERFILES)
def test_deno_is_pinned_and_checked(dockerfile):
    text = (ROOT / dockerfile).read_text(encoding="utf-8")

    assert "releases/latest" not in text
    assert re.search(r"ARG DENO_VERSION=\d+\.\d+\.\d+", text)
    assert re.search(r"ARG DENO_SHA256=[0-9a-f]{64}", text)
    assert 'sha256sum -c -' in text


def test_deno_is_the_same_in_both_images():
    versions = {
        re.search(r"ARG DENO_VERSION=(\S+)", (ROOT / name).read_text(encoding="utf-8")).group(1)
        for name in SERVICE_DOCKERFILES
    }
    assert len(versions) == 1


def test_runpod_torch_is_pinned():
    assert re.search(r"torch==[\d.]+ torchaudio==[\d.]+", (ROOT / "Dockerfile.runpod").read_text(encoding="utf-8"))


def test_api_bind_address_is_configurable():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert '"${API_BIND_ADDRESS:-127.0.0.1}:8000:8000"' in compose


def test_postgres_data_location_defaults_to_its_own_folder():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    # Separate from the backups in billing-data/backups.
    assert "- ${POSTGRES_DATA_LOCATION:-./billing-data/postgres}:/var/lib/postgresql/data" in compose


def test_postgres_data_volume_is_declared():
    # POSTGRES_DATA_LOCATION=postgres_data names it; Compose rejects an
    # undeclared volume.
    import yaml

    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))

    assert "postgres_data" in compose["volumes"]


def compose_services():
    import yaml

    return yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))["services"]


@pytest.mark.parametrize("service", ["worker", "remote-worker", "cleanup"])
def test_only_the_api_mounts_billing_data(service):
    # The workers process untrusted media; they reach billing through
    # PostgreSQL and need neither the backups nor the cluster files.
    services = compose_services()

    assert "./billing-data:/billing" in services["api"]["volumes"]
    assert not any("billing-data" in volume for volume in services[service].get("volumes", []))


def test_every_service_rotates_its_logs():
    for name, service in compose_services().items():
        assert service["logging"]["driver"] == "json-file", name
        assert service["logging"]["options"]["max-size"], name


# --- Writable directories ---------------------------------------------------

@pytest.fixture
def directories(default_settings, tmp_path):
    default_settings.service.data_dir = str(tmp_path / "data")
    default_settings.billing.database_path = str(tmp_path / "billing" / "billing.sqlite3")
    default_settings.billing.backup_dir = str(tmp_path / "billing" / "backups")
    for directory in ("data", "billing", "billing/backups"):
        (tmp_path / directory).mkdir()
    return default_settings


def test_writable_directories_pass(directories):
    assert config.unwritable_directories(directories, "api") == []


def test_root_owned_volume_explains_the_fix(directories, monkeypatch):
    data_dir = directories.service.data_dir
    monkeypatch.setattr(config.os, "access", lambda path, mode: path != data_dir)

    [problem] = config.unwritable_directories(directories, "api")

    assert problem.startswith(f"{data_dir} is not writable")
    assert config.OWNERSHIP_FIX in problem


@pytest.mark.parametrize(
    ("role", "checked"),
    [("api", {"data", "billing", "backups"}), ("worker", {"data", "billing"}), ("cleanup", {"data"})],
)
def test_each_role_checks_only_what_it_writes(directories, monkeypatch, role, checked):
    seen = set()

    def access(path, mode):
        seen.add(Path(path).name)
        return True

    monkeypatch.setattr(config.os, "access", access)
    config.unwritable_directories(directories, role)

    assert seen == checked


def test_missing_directories_are_created_later_not_reported(default_settings, tmp_path):
    default_settings.service.data_dir = str(tmp_path / "not-yet")

    assert config.unwritable_directories(default_settings, "cleanup") == []


def test_startup_fails_on_unwritable_directories(directories, monkeypatch):
    monkeypatch.setattr(config, "settings", directories)
    monkeypatch.setattr(config.os, "access", lambda path, mode: False)

    with pytest.raises(config.ConfigError, match="not writable"):
        config.ensure_valid("cleanup")
