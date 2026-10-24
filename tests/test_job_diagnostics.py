import sqlite3
import sys
import time
import types
from pathlib import Path

import pytest

from app import main
from app.worker import tasks


@pytest.fixture
def worker(api_context):
    client, settings, billing = api_context
    settings.billing.provider = "google_play"
    settings.billing.products = {"starter": 10}
    billing.credit_purchase("user", "starter", "token", "order")
    return client, settings, billing


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def diagnostics(settings, job_id):
    connection = sqlite3.connect(settings.billing.database_path)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute("SELECT * FROM job_diagnostics WHERE job_id = ?", (job_id,)).fetchone()
        return dict(row) if row else None
    finally:
        connection.close()


def install_pipeline(monkeypatch, transcribe_source):
    module = types.ModuleType("app.transcription.pipeline")
    module.transcribe_source = transcribe_source
    monkeypatch.setitem(sys.modules, "app.transcription.pipeline", module)


def submit(client, token="user"):
    response = client.post("/api/transcriptions", headers=auth(token), json={"source_url": "https://example.com/video"})
    assert response.status_code == 202
    return response.json()["job_id"]


def test_queued_job_is_recorded(worker):
    client, settings, _ = worker

    job_id = submit(client)

    record = diagnostics(settings, job_id)
    assert record["status"] == "queued"
    assert record["user_id"] == "user"
    assert record["source"] == "url"
    assert record["source_url"] == "https://example.com/video"
    assert record["duration_seconds"] == 130.0
    assert record["cost_minutes"] == 2
    assert record["backend"] == "local"
    assert record["started_at"] is None
    assert record["finished_at"] is None


def test_failed_job_keeps_its_error_after_the_redis_job_expires(worker, monkeypatch):
    client, settings, _ = worker
    job_id = submit(client)

    def transcribe_source(**kwargs):
        raise RuntimeError("download failed")

    install_pipeline(monkeypatch, transcribe_source)
    tasks.transcribe_job.fn(job_id, "https://example.com/video")
    tasks.redis.delete(tasks.job_key(job_id))

    record = diagnostics(settings, job_id)
    assert record["status"] == "failed"
    # Diagnostics keep the full error for operators; users see a plain message.
    assert record["error"] == "RuntimeError: download failed"
    assert record["started_at"] is not None
    assert record["finished_at"] >= record["started_at"]


def test_completed_job_is_recorded(worker, monkeypatch):
    client, settings, _ = worker
    job_id = submit(client)

    def transcribe_source(output_dir, **kwargs):
        midi = output_dir / "transcription.mid"
        midi.write_bytes(b"MThd")
        return midi

    install_pipeline(monkeypatch, transcribe_source)
    tasks.transcribe_job.fn(job_id, "https://example.com/video")

    record = diagnostics(settings, job_id)
    assert record["status"] == "completed"
    assert record["error"] is None
    assert record["finished_at"] is not None


def test_cancelled_job_is_recorded(worker):
    client, settings, _ = worker
    job_id = submit(client)

    assert client.delete(f"/api/transcriptions/{job_id}", headers=auth("user")).status_code == 200

    record = diagnostics(settings, job_id)
    assert record["status"] == "cancelled"
    assert record["finished_at"] is not None


def test_restart_failure_is_recorded(worker):
    client, settings, _ = worker
    job_id = submit(client)
    tasks.redis.hset(tasks.job_key(job_id), "status", "processing")

    tasks.recover_active_jobs("Transcription interrupted because the server restarted")

    record = diagnostics(settings, job_id)
    assert record["status"] == "failed"
    assert record["error"] == "Transcription interrupted because the server restarted"


def test_recording_errors_do_not_fail_the_job(worker, monkeypatch):
    client, _, billing = worker

    def broken(*args):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(billing, "record_job", broken)

    submit(client)


def test_account_deletion_removes_diagnostics(worker):
    client, settings, billing = worker
    billing.credit_purchase("other", "starter", "other-token", "other-order")
    mine = submit(client, "user")
    theirs = submit(client, "other")

    assert client.delete("/api/account", headers=auth("user")).status_code == 204

    assert diagnostics(settings, mine) is None
    assert diagnostics(settings, theirs) is not None


def test_backup_prunes_old_diagnostics_and_contains_the_rest(worker):
    client, settings, billing = worker
    settings.billing.job_diagnostics_retention = "1d"
    old = submit(client)
    recent = submit(client)
    with sqlite3.connect(settings.billing.database_path) as connection:
        connection.execute(
            "UPDATE job_diagnostics SET created_at = ? WHERE job_id = ?",
            (int(time.time()) - 2 * 24 * 60 * 60, old),
        )

    main.run_billing_backup()

    assert diagnostics(settings, old) is None
    [backup] = list(Path(settings.billing.backup_dir).glob("billing-*.sqlite3"))
    connection = sqlite3.connect(backup)
    try:
        assert connection.execute("SELECT job_id FROM job_diagnostics").fetchall() == [(recent,)]
    finally:
        connection.close()


def test_diagnostics_retention_default(api_context):
    _, settings, _ = api_context

    assert settings.billing.job_diagnostics_retention == 30 * 24 * 60 * 60
