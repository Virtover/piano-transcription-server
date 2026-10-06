from collections.abc import Iterator
import time

import fakeredis
import pytest
from fastapi.testclient import TestClient


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

    settings.billing_provider = "none"
    settings.billing_products = {}
    settings.free_minutes = None
    settings.free_minutes_period = None
    settings.google_play_package_name = None
    settings.google_play_service_account_file = None
    settings.google_oauth_client_ids = ["test-client-id"]
    settings.billing_database_path = str(tmp_path / "billing.sqlite3")
    settings.data_dir = str(tmp_path / "data")
    settings.max_video_length_minutes = 20

    redis = fakeredis.FakeRedis(decode_responses=True)
    billing = Billing(settings.billing_database_path)
    monkeypatch.setattr(billing_routes, "redis", redis)
    monkeypatch.setattr(transcription_routes, "redis", redis)
    monkeypatch.setattr(tasks, "redis", redis)
    monkeypatch.setattr(billing_routes, "billing", billing)
    monkeypatch.setattr(transcription_routes, "billing", billing)
    monkeypatch.setattr(tasks, "billing", billing)
    monkeypatch.setattr(transcription_routes, "video_duration", lambda source_url: 130.0)
    monkeypatch.setattr(transcription_routes.transcribe_job, "send", lambda *args: None)
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
            {"aud": "test-client-id", "sub": token, "exp": str(int(time.time()) + 3600)},
        )

    monkeypatch.setattr(auth.requests, "get", fake_google_request)

    monkeypatch.setattr(main, "fail_active_jobs", lambda reason: None)
    monkeypatch.setattr(main, "reconcile_billing", lambda: None)
    monkeypatch.setattr(main, "sync_products", lambda: None)
    monkeypatch.setattr(main, "enable_transcription_submissions", lambda: transcription_routes.enable_transcription_submissions())
    transcription_routes.transcription_submissions_enabled = False
    google_play.synced_products = None

    with TestClient(main.app) as client:
        yield client, settings, billing

    redis.flushall()
