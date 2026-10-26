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
    settings.billing.provider = "google_play"
    settings.billing.products = {"starter": 10}
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

    def transcribe_source(source_url, output_dir, progress_callback, metadata_callback, cancellation_callback, **kwargs):
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
    assert Path(result["result"]) == Path(settings.service.data_dir) / "jobs" / "job" / "transcription.mid"
    assert 0 < tasks.redis.ttl(tasks.job_key("job")) <= tasks.job_ttl()
    assert billing.get_balance("user") == 6


@pytest.mark.parametrize(("stored", "expected"), [({"duration": "130.5"}, 130.5), ({}, None), ({"duration": "x"}, None)])
def test_audio_is_cut_at_the_charged_duration(worker, monkeypatch, stored, expected):
    _, billing = worker
    queue_job(billing, **stored)
    seen = {}

    def transcribe_source(output_dir, max_seconds, **kwargs):
        seen["max_seconds"] = max_seconds
        midi = output_dir / "transcription.mid"
        midi.write_bytes(b"MThd")
        return midi

    install_pipeline(monkeypatch, transcribe_source)
    tasks.transcribe_job.fn("job", "https://example.com/video")

    assert seen["max_seconds"] == expected


def test_failed_job_releases_reservation(worker, monkeypatch):
    _, billing = worker
    queue_job(billing)

    def transcribe_source(**kwargs):
        raise RuntimeError("download failed")

    install_pipeline(monkeypatch, transcribe_source)
    tasks.transcribe_job.fn("job", "https://example.com/video")

    assert job()["status"] == "failed"
    assert job()["error"] == "Transcription failed"
    assert job()["error_detail"] == "RuntimeError: download failed"
    assert billing.get_balance("user") == 10


def test_cancelled_job_is_partially_charged_and_cleaned_up(worker, monkeypatch):
    settings, billing = worker
    queue_job(billing, cost=10)
    output_dir = Path(settings.service.data_dir) / "jobs" / "job"

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


def test_restart_fails_interrupted_jobs_and_keeps_queued_ones(worker, monkeypatch):
    settings, billing = worker
    sent = []
    monkeypatch.setattr(tasks, "send_job", lambda job_id, backend, *arguments: sent.append((job_id, backend, arguments)))
    queue_job(billing, "queued", cost=3, source_url="https://example.com/a", backend="local")
    queue_job(billing, "processing")
    tasks.redis.hset(tasks.job_key("processing"), "status", "processing")
    tasks.redis.hset(tasks.job_key("done"), mapping={"status": "completed", "user_id": "user"})
    interrupted_dir = Path(settings.service.data_dir) / "jobs" / "processing"
    queued_dir = Path(settings.service.data_dir) / "jobs" / "queued"
    interrupted_dir.mkdir(parents=True)
    queued_dir.mkdir(parents=True)

    tasks.recover_active_jobs("restarted")

    assert job("processing")["status"] == "failed"
    assert job("processing")["error"] == "restarted"
    assert not interrupted_dir.exists()
    assert job("queued")["status"] == "queued"
    assert queued_dir.exists()
    assert sent == [("queued", "local", ("https://example.com/a",))]
    assert job("done")["status"] == "completed"
    # The queued job keeps its reservation; the interrupted one is refunded.
    assert billing.reserved_job_ids() == ["queued"]
    assert billing.get_balance("user") == 7


def test_queued_upload_is_requeued_from_its_file(worker, monkeypatch):
    _, billing = worker
    sent = []
    monkeypatch.setattr(tasks, "send_job", lambda job_id, backend, *arguments: sent.append(arguments))
    queue_job(billing, "upload", source="upload", upload_title="Nocturne", backend="local")

    tasks.recover_active_jobs("restarted")

    assert sent == [(None, "Nocturne")]


def test_remote_job_runs_locally_when_outsourcing_was_turned_off(worker, monkeypatch):
    _, billing = worker
    sent = []
    monkeypatch.setattr(tasks, "send_job", lambda job_id, backend, *arguments: sent.append(backend))
    queue_job(billing, "remote", source_url="https://example.com/a", backend="remote")

    tasks.recover_active_jobs("restarted")

    assert sent == ["local"]
    assert job("remote")["backend"] == "local"
    assert tasks.redis.smembers(tasks.routing.ACTIVE_KEYS["local"]) == {"remote"}


def test_queued_job_that_cannot_be_requeued_fails_and_is_refunded(worker, monkeypatch):
    _, billing = worker

    def broken(*args):
        raise ConnectionError("broker down")

    monkeypatch.setattr(tasks, "send_job", broken)
    queue_job(billing, "queued", source_url="https://example.com/a", backend="local")

    tasks.recover_active_jobs("restarted")

    assert job("queued")["status"] == "failed"
    assert billing.reserved_job_ids() == []
    assert tasks.redis.scard(tasks.routing.ACTIVE_KEYS["local"]) == 0


def test_second_delivery_of_a_running_job_keeps_its_routing_entry(worker):
    _, billing = worker
    queue_job(billing)
    tasks.redis.hset(tasks.job_key("job"), "status", "processing")
    tasks.redis.sadd(tasks.routing.ACTIVE_KEYS["local"], "job")

    tasks.run_job("job", "local", lambda context: pytest.fail("ran twice"))

    assert tasks.redis.smembers(tasks.routing.ACTIVE_KEYS["local"]) == {"job"}


def test_delivery_of_a_cancelled_job_removes_its_routing_entry(worker):
    _, billing = worker
    queue_job(billing)
    tasks.redis.hset(tasks.job_key("job"), "status", "cancelled")
    tasks.redis.sadd(tasks.routing.ACTIVE_KEYS["local"], "job")

    tasks.run_job("job", "local", lambda context: pytest.fail("ran a cancelled job"))

    assert tasks.redis.scard(tasks.routing.ACTIVE_KEYS["local"]) == 0


def test_cleanup_removes_only_expired_job_directories(worker):
    settings, _ = worker
    jobs_dir = Path(settings.service.data_dir) / "jobs"
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
    (Path(settings.service.data_dir) / "jobs" / "mine").mkdir(parents=True)

    tasks.delete_user_jobs("user")

    assert not tasks.redis.exists(tasks.job_key("mine"))
    assert tasks.redis.exists(tasks.job_key("theirs"))
    assert not (Path(settings.service.data_dir) / "jobs" / "mine").exists()


def test_uploaded_file_is_transcribed_from_job_directory(worker, monkeypatch):
    settings, billing = worker
    queue_job(billing, source="upload", title="My Song")
    calls = []

    def transcribe_source(source_url, output_dir, upload_path, upload_title, metadata_callback, **kwargs):
        calls.append((source_url, upload_path, upload_title))
        metadata_callback({"title": upload_title, "duration": 130.0})
        midi = output_dir / "transcription.mid"
        midi.write_bytes(b"MThd")
        return midi

    install_pipeline(monkeypatch, transcribe_source)
    tasks.transcribe_job.fn("job", upload_title="My Song")

    job_dir = Path(settings.service.data_dir) / "jobs" / "job"
    assert calls == [(None, job_dir / tasks.UPLOAD_FILE_NAME, "My Song")]
    assert job()["status"] == "completed"
    assert job()["title"] == "My Song"
    assert billing.get_balance("user") == 6


def test_cleanup_service_exits_on_sigterm(monkeypatch):
    import signal

    from app.worker import cleanup

    class StopLoop(BaseException):
        pass

    handlers = {}
    monkeypatch.setattr(cleanup, "ensure_valid", lambda role: None)
    monkeypatch.setattr(cleanup.signal, "signal", lambda signum, handler: handlers.update({signum: handler}))
    monkeypatch.setattr(cleanup, "cleanup_expired_jobs", lambda: (_ for _ in ()).throw(StopLoop()))

    with pytest.raises(StopLoop):
        cleanup.main()

    with pytest.raises(SystemExit) as exited:
        handlers[signal.SIGTERM](signal.SIGTERM, None)
    assert exited.value.code == 0


def test_claim_sets_the_lease_and_the_end_of_the_job_removes_it(worker, monkeypatch):
    _, billing = worker
    queue_job(billing)
    leases = []

    def transcribe(context):
        leases.append(tasks.redis.get(tasks.lease_key("job")))
        assert 0 < tasks.redis.ttl(tasks.lease_key("job")) <= tasks.LEASE_TTL
        midi = context.output_dir / "transcription.mid"
        midi.write_bytes(b"MThd")
        return midi

    tasks.run_job("job", "local", transcribe)

    assert job()["status"] == "completed"
    assert leases[0] is not None
    assert not tasks.redis.exists(tasks.lease_key("job"))


def test_restart_of_only_the_api_keeps_running_jobs(worker, monkeypatch):
    settings, billing = worker
    monkeypatch.setattr(tasks, "send_job", lambda *args: None)
    queue_job(billing, backend="remote")
    assert tasks.claim_job("job")
    running_dir = Path(settings.service.data_dir) / "jobs" / "job"
    running_dir.mkdir(parents=True)

    tasks.recover_active_jobs("restarted")

    assert job()["status"] == "processing"
    assert running_dir.exists()
    assert billing.reserved_job_ids() == ["job"]
    # The routing count is rebuilt with it.
    assert tasks.redis.smembers(tasks.routing.ACTIVE_KEYS["remote"]) == {"job"}


def test_job_whose_worker_stopped_fails_when_its_lease_expires(worker):
    settings, billing = worker
    queue_job(billing, backend="local")
    assert tasks.claim_job("job")
    tasks.redis.sadd(tasks.routing.ACTIVE_KEYS["local"], "job")
    job_files = Path(settings.service.data_dir) / "jobs" / "job"
    job_files.mkdir(parents=True)

    assert tasks.fail_abandoned_jobs() == 0
    assert job()["status"] == "processing"

    tasks.redis.delete(tasks.lease_key("job"))
    assert tasks.fail_abandoned_jobs() == 1

    assert job()["status"] == "failed"
    assert job()["error"] == tasks.WORKER_STOPPED
    assert billing.reserved_job_ids() == []
    assert billing.get_balance("user") == 10
    assert not job_files.exists()
    assert tasks.redis.scard(tasks.routing.ACTIVE_KEYS["local"]) == 0


def test_moved_job_keeps_the_lease_of_the_worker_it_moved_to(worker):
    _, billing = worker
    queue_job(billing)
    first = tasks.JobLease("job")
    assert tasks.claim_job("job", first.token)
    # The job moved to another backend, whose worker claimed it before the
    # first worker stopped its lease.
    tasks.redis.hset(tasks.job_key("job"), "status", "queued")
    second = tasks.JobLease("job")
    assert tasks.claim_job("job", second.token)

    first.stop()

    assert tasks.redis.get(tasks.lease_key("job")) == second.token
    second.stop()
    assert not tasks.redis.exists(tasks.lease_key("job"))


def test_result_is_retrievable_for_the_retention_period(api_context, monkeypatch):
    client, settings, _ = api_context
    settings.limits.result_retention = "2d"
    job_id = client.post("/api/transcriptions", json={"source_url": "https://example.com/video"}).json()["job_id"]

    def transcribe_source(output_dir, **kwargs):
        midi = output_dir / "transcription.mid"
        midi.write_bytes(b"MThd")
        return midi

    install_pipeline(monkeypatch, transcribe_source)
    tasks.transcribe_job.fn(job_id, "https://example.com/video")

    assert 24 * 60 * 60 < tasks.redis.ttl(tasks.job_key(job_id)) <= 2 * 24 * 60 * 60
    assert client.get(f"/api/transcriptions/{job_id}/midi").content == b"MThd"
    # The cleanup service keeps the file while the job is retrievable.
    tasks.cleanup_expired_jobs()
    assert client.get(f"/api/transcriptions/{job_id}/midi").status_code == 200

    # Once the retention period ends, the job expires and its file is deleted.
    tasks.redis.delete(tasks.job_key(job_id))
    assert client.get(f"/api/transcriptions/{job_id}/midi").status_code == 404
    tasks.cleanup_expired_jobs()
    assert not (Path(settings.service.data_dir) / "jobs" / job_id).exists()


@pytest.mark.parametrize("charge", [True, False])
def test_cancelled_job_is_kept_for_the_retention_period(worker, charge):
    settings, billing = worker
    settings.limits.result_retention = "1d"
    queue_job(billing)

    tasks.cancel_job("job", charge=charge)

    assert 60 * 60 < tasks.redis.ttl(tasks.job_key("job")) <= 24 * 60 * 60
