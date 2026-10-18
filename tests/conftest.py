from collections.abc import Iterator
import time

import fakeredis
import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def default_settings(tmp_path):
    """Every test starts from the code defaults, not the developer's .env."""
    from app import config

    saved = {name: getattr(config.settings, name) for name in config.GROUPS}
    fresh = config.load_settings(env_file=None, secrets_dir=None)
    for name in config.GROUPS:
        setattr(config.settings, name, getattr(fresh, name))
    config.settings.secrets_dir = str(tmp_path / "secrets")
    yield config.settings
    for name, group in saved.items():
        setattr(config.settings, name, group)
    config.settings.secrets_dir = config.SECRETS_DIR


@pytest.fixture
def api_context(tmp_path, monkeypatch) -> Iterator[tuple[TestClient, object, object]]:
    from app import google_play
    from app import auth
    from app import main
    from app.api.routes import billing as billing_routes
    from app.api.routes import transcriptions as transcription_routes
    from app.billing import Billing
    from app.config import settings
    from app.worker import tasks

    settings.auth.client_ids = ["test-client-id"]
    settings.billing.database_path = str(tmp_path / "billing.sqlite3")
    settings.billing.backup_dir = str(tmp_path / "backups")
    settings.service.data_dir = str(tmp_path / "data")
    settings.limits.max_video_length_minutes = 20

    redis = fakeredis.FakeRedis(decode_responses=True)
    billing = Billing(settings.billing.database_path)
    monkeypatch.setattr(billing_routes, "redis", redis)
    monkeypatch.setattr(transcription_routes, "redis", redis)
    monkeypatch.setattr(tasks, "redis", redis)
    monkeypatch.setattr(billing_routes, "billing", billing)
    monkeypatch.setattr(transcription_routes, "billing", billing)
    monkeypatch.setattr(tasks, "billing", billing)
    monkeypatch.setattr(transcription_routes, "video_duration", lambda source_url: 130.0)
    monkeypatch.setattr(transcription_routes.transcribe_job, "send", lambda *args, **kwargs: None)
    class FakeResponse:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self._payload = payload

        @property
        def ok(self):
            return 200 <= self.status_code < 300

        def json(self):
            return self._payload

    def fake_google_request(url, **kwargs):
        token = kwargs["params"]["access_token"]
        if token == "invalid":
            return FakeResponse(401, {})
        return FakeResponse(
            200,
            {
                "aud": "test-client-id",
                "sub": token,
                "email": f"{token}@example.com",
                "email_verified": "true",
                "exp": str(int(time.time()) + 3600),
            },
        )

    monkeypatch.setattr(auth.requests, "get", fake_google_request)
    auth._token_cache.clear()

    monkeypatch.setattr(main, "fail_active_jobs", lambda reason: None)
    monkeypatch.setattr(main, "reconcile_billing", lambda: None)
    monkeypatch.setattr(main, "sync_products", lambda: None)
    monkeypatch.setattr(main, "backup_billing_database", lambda: None)
    monkeypatch.setattr(main, "enable_transcription_submissions", lambda: transcription_routes.enable_transcription_submissions())
    transcription_routes.transcription_submissions_enabled = False
    google_play.synced_products = None

    with TestClient(main.app) as client:
        yield client, settings, billing

    redis.flushall()
