import sys
import types
from pathlib import Path

import pytest

from app.billing import cancelled_cost
from app.transcription.cancellation import TranscriptionCancelled
from app.worker import tasks


@pytest.fixture
def worker(api_context):
    _, settings, billing = api_context
    settings.billing_provider = "google_play"
    settings.billing_products = {"starter": 10}
    billing.credit_purchase("user", "starter", "token", "order")
    return settings, billing


def queue_job(billing, job_id="job", cost=4, **values):
    billing.reserve("user", job_id, cost)
    tasks.redis.hset(
        tasks.job_key(job_id),
        mapping={"status": "queued", "progress": "0.0", "user_id": "user", "full_cost": str(cost), **values},
    )


def install_pipeline(monkeypatch, transcribe_source):
    module = types.ModuleType("app.transcription.pipeline")
    module.transcribe_source = transcribe_source
    monkeypatch.setitem(sys.modules, "app.transcription.pipeline", module)


def job(job_id="job"):
    return tasks.redis.hgetall(tasks.job_key(job_id))


def test_successful_job_completes_and_charges(worker, monkeypatch):
    settings, billing = worker
    queue_job(billing)

    def transcribe_source(source_url, output_dir, progress_callback, metadata_callback, cancellation_callback):
        assert job()["status"] == "processing"
        metadata_callback({"title": "Nocturne"})
        progress_callback(0.5)
        midi = output_dir / "transcription.mid"
        midi.write_bytes(b"MThd")
        return midi

    install_pipeline(monkeypatch, transcribe_source)
    tasks.transcribe_job.fn("job", "https://example.com/video")

    result = job()
    assert result["status"] == "completed"
    assert result["progress"] == "1.0"
    assert result["title"] == "Nocturne"
    assert Path(result["result"]) == Path(settings.data_dir) / "jobs" / "job" / "transcription.mid"
    assert 0 < tasks.redis.ttl(tasks.job_key("job")) <= tasks.JOB_TTL
    assert billing.get_balance("user") == 6


def test_failed_job_releases_reservation(worker, monkeypatch):
    _, billing = worker
    queue_job(billing)

    def transcribe_source(**kwargs):
        raise RuntimeError("download failed")

    install_pipeline(monkeypatch, transcribe_source)
    tasks.transcribe_job.fn("job", "https://example.com/video")

    assert job()["status"] == "failed"
    assert job()["error"] == "download failed"
    assert billing.get_balance("user") == 10


def test_cancelled_job_is_partially_charged_and_cleaned_up(worker, monkeypatch):
    settings, billing = worker
    queue_job(billing, cost=10)
    output_dir = Path(settings.data_dir) / "jobs" / "job"

    def transcribe_source(output_dir, progress_callback, **kwargs):
        (output_dir / "audio.wav").write_bytes(b"audio")
        progress_callback(0.5)
        tasks.redis.hset(tasks.job_key("job"), "status", "cancelled")
        progress_callback(0.6)

    install_pipeline(monkeypatch, transcribe_source)
    tasks.transcribe_job.fn("job", "https://example.com/video")

    assert job()["status"] == "cancelled"
    assert not output_dir.exists()
    assert billing.get_balance("user") == 10 - cancelled_cost(10, 0.5)


def test_job_that_is_no_longer_queued_is_skipped(worker, monkeypatch):
    _, billing = worker
    queue_job(billing)
    tasks.redis.hset(tasks.job_key("job"), "status", "cancelled")
    install_pipeline(monkeypatch, lambda **kwargs: pytest.fail("job must not run"))

    tasks.transcribe_job.fn("job", "https://example.com/video")

    assert job()["status"] == "cancelled"


def test_claim_job_only_claims_queued_jobs(worker):
    tasks.redis.hset(tasks.job_key("job"), "status", "queued")

    assert tasks.claim_job("job") is True
    assert tasks.claim_job("job") is False
    assert tasks.claim_job("missing") is False


def test_complete_job_raises_when_cancelled(worker):
    tasks.redis.hset(tasks.job_key("job"), "status", "cancelled")

    with pytest.raises(TranscriptionCancelled):
        tasks.complete_job("job", Path("result.mid"))


def test_reconcile_billing_finishes_interrupted_transitions(worker):
    _, billing = worker
    queue_job(billing, "completed", cost=2)
    queue_job(billing, "failed", cost=2)
    queue_job(billing, "cancelled", cost=2, progress="1.0")
    queue_job(billing, "running", cost=2)
    billing.reserve("user", "vanished", 1)
    tasks.redis.hset(tasks.job_key("completed"), "status", "completed")
    tasks.redis.hset(tasks.job_key("failed"), "status", "failed")
    tasks.redis.hset(tasks.job_key("cancelled"), "status", "cancelled")
    tasks.redis.hset(tasks.job_key("running"), "status", "processing")

    tasks.reconcile_billing()

    assert billing.reserved_job_ids() == ["running"]
    # 10 - 2 (completed) - 2 (cancelled at full progress) - 2 (still reserved)
    assert billing.get_balance("user") == 4


def test_fail_active_jobs_marks_jobs_failed_and_releases(worker):
    settings, billing = worker
    queue_job(billing, "queued")
    queue_job(billing, "processing")
    tasks.redis.hset(tasks.job_key("processing"), "status", "processing")
    tasks.redis.hset(tasks.job_key("done"), mapping={"status": "completed", "user_id": "user"})
    job_dir = Path(settings.data_dir) / "jobs" / "processing"
    job_dir.mkdir(parents=True)

    tasks.fail_active_jobs("restarted")

    assert job("queued")["status"] == "failed"
    assert job("processing")["error"] == "restarted"
    assert job("done")["status"] == "completed"
    assert not job_dir.exists()
    assert billing.reserved_job_ids() == []
    assert billing.get_balance("user") == 10


def test_cleanup_removes_only_expired_job_directories(worker):
    settings, _ = worker
    jobs_dir = Path(settings.data_dir) / "jobs"
    (jobs_dir / "expired").mkdir(parents=True)
    (jobs_dir / "active").mkdir()
    (jobs_dir / "stray-file").write_text("")
    tasks.redis.hset(tasks.job_key("active"), "status", "completed")

    tasks.cleanup_expired_jobs()

    assert sorted(path.name for path in jobs_dir.iterdir()) == ["active", "stray-file"]


def test_cleanup_without_jobs_directory(worker):
    tasks.cleanup_expired_jobs()


def test_delete_user_jobs_keeps_other_users_jobs(worker):
    settings, _ = worker
    tasks.redis.hset(tasks.job_key("mine"), "user_id", "user")
    tasks.redis.hset(tasks.job_key("theirs"), "user_id", "someone-else")
    (Path(settings.data_dir) / "jobs" / "mine").mkdir(parents=True)

    tasks.delete_user_jobs("user")

    assert not tasks.redis.exists(tasks.job_key("mine"))
    assert tasks.redis.exists(tasks.job_key("theirs"))
    assert not (Path(settings.data_dir) / "jobs" / "mine").exists()
