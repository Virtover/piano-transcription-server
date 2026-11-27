"""Running out of disk space (app/disk.py): jobs wait for room instead of filling the disk, a job whose disk filled up
is tried once more, submissions are refused while the disk is full, and the watcher warns."""

import errno

import pytest

from app import disk
from app.config import settings
from app.worker import tasks

GIB = disk.GIB


@pytest.fixture
def free(monkeypatch):
    space = {"bytes": 100 * GIB}
    monkeypatch.setattr(disk, "free_bytes", lambda path=None: space["bytes"])
    monkeypatch.setattr(settings.limits, "min_free_disk_gib", 2.0)
    return space


def test_a_jobs_files_are_estimated_from_its_length_and_mode():
    piano = disk.job_disk_bytes({"duration": "3600"})
    ensemble = disk.job_disk_bytes({"duration": "3600", "mode": "arrangement"})
    assert disk.JOB_BASE_BYTES < piano < ensemble
    assert disk.job_disk_bytes({"duration": "bad"}) == disk.JOB_BASE_BYTES


def test_room_keeps_the_minimum_free(free):
    free["bytes"] = 3 * GIB
    assert disk.room_for(int(0.9 * GIB))
    assert not disk.room_for(int(1.1 * GIB))
    assert disk.level() == "low"
    free["bytes"] = 1 * GIB
    assert disk.level() == "full"
    free["bytes"] = None
    assert disk.room_for(10**15) and disk.level() == "unknown"


def test_a_full_disk_is_recognised_through_its_causes():
    try:
        try:
            raise OSError(errno.ENOSPC, "No space left on device")
        except OSError as error:
            raise RuntimeError("could not write") from error
    except RuntimeError as wrapped:
        assert disk.is_disk_full(wrapped)
    assert disk.is_disk_full(disk.StorageFull())
    assert not disk.is_disk_full(OSError(errno.EACCES, "denied"))
    assert not disk.is_disk_full(None)


def queued(job_id="disk-job", **fields):
    tasks.redis.hset(tasks.job_key(job_id), mapping={"status": "queued", "duration": "600", **fields})
    return job_id


def test_a_job_without_room_waits_and_is_offered_again(api_context, free, monkeypatch):
    sent = []
    monkeypatch.setattr(tasks, "send_job", lambda job_id, backend, *args, **kw: sent.append((job_id, backend, kw)))
    job_id = queued()
    free["bytes"] = 2 * GIB
    assert tasks.waits_for_disk(job_id, "local")
    assert sent == [(job_id, "local", {"delay": int(tasks.DISK_WAIT_DELAY * 1000)})]
    assert tasks.redis.hget(tasks.job_key(job_id), "status") == "queued"
    free["bytes"] = 100 * GIB
    assert not tasks.waits_for_disk(job_id, "local")


def test_a_job_that_waited_too_long_fails_refunded(api_context, free, monkeypatch):
    monkeypatch.setattr(tasks, "send_job", lambda *args, **kw: None)
    failed = []
    monkeypatch.setattr(tasks, "fail_unqueued_job", lambda job_id, job, backend, reason, code: failed.append((reason, code)))
    job_id = queued(disk_waits=str(tasks.DISK_WAIT_LIMIT))
    free["bytes"] = 2 * GIB
    assert tasks.waits_for_disk(job_id, "local")
    assert failed == [(str(disk.StorageFull()), disk.STORAGE_FULL)]


def test_a_broken_check_never_loses_a_job(api_context, free, monkeypatch):
    monkeypatch.setattr(disk, "job_disk_bytes", lambda job: 1 / 0)
    assert not tasks.waits_for_disk(queued(), "local")


def test_a_job_whose_disk_filled_up_is_tried_once_more(api_context):
    job_id = queued()
    full = OSError(errno.ENOSPC, "No space left on device")
    assert tasks.retry_backend(job_id, "local", full) == "local"
    assert tasks.retry_backend(job_id, "local", full) is None


def test_submissions_are_refused_while_the_disk_is_full(api_context, free):
    from fastapi import HTTPException

    from app.api.routes import transcriptions

    free["bytes"] = 1 * GIB
    with pytest.raises(HTTPException) as refused:
        transcriptions.ensure_storage_room()
    assert refused.value.status_code == 503 and refused.value.detail["error_code"] == "storage_full"
    free["bytes"] = 3 * GIB
    transcriptions.ensure_storage_room()
    with pytest.raises(HTTPException):
        transcriptions.ensure_storage_room(2 * GIB)


def test_the_watcher_warns_when_low_and_reports_when_full(free, monkeypatch, caplog):
    reports = []
    from app import error_report

    monkeypatch.setattr(error_report, "report_exception", lambda error, context=None: reports.append(str(error)))
    rounds = iter([False, False, True])
    free["bytes"] = 3 * GIB
    disk.watch(stop=lambda: next(rounds), sleep=lambda seconds: free.update(bytes=1 * GIB))
    assert any("running low" in record.message for record in caplog.records)
    assert any("refused" in record.message for record in caplog.records)
    assert len(reports) == 1
