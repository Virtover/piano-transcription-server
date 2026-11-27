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


# --- The host drive under Docker Desktop -------------------------------------------------

import importlib.util  # noqa: E402
import json  # noqa: E402
from pathlib import Path  # noqa: E402

_spec = importlib.util.spec_from_file_location("stacks_for_disk", Path(__file__).resolve().parent.parent / "scripts" / "stacks.py")
stacks = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(stacks)


def test_docker_desktops_disk_folder_comes_from_its_settings(tmp_path):
    appdata, local = tmp_path / "Roaming", tmp_path / "Local"
    (appdata / "Docker").mkdir(parents=True)
    (local / "Docker").mkdir(parents=True)
    environ = {"APPDATA": str(appdata), "LOCALAPPDATA": str(local)}
    assert stacks.docker_desktop_disk_folder("win32", environ) == local / "Docker" / "wsl"
    (appdata / "Docker" / "settings-store.json").write_text(json.dumps({"CustomWslDistroDir": "D:\DockerDisk\WSL"}))
    assert stacks.docker_desktop_disk_folder("win32", environ) == Path("D:\DockerDisk\WSL")
    assert stacks.docker_desktop_disk_folder("linux", environ) is None
    assert stacks.docker_desktop_disk_folder("win32", {**environ, "PIANO_NO_HOST_DISK_PROBE": "1"}) is None


def test_docker_desktop_on_macos(tmp_path):
    data = tmp_path / "Library" / "Containers" / "com.docker.docker" / "Data"
    data.mkdir(parents=True)
    assert stacks.docker_desktop_disk_folder("darwin", {}, home=tmp_path) == data


def test_the_probe_is_set_once_in_the_env_file(tmp_path):
    disk_folder = tmp_path / "DockerDisk" / "WSL"
    root = tmp_path / "checkout"
    root.mkdir()
    (root / ".env").write_text("COMPOSE_PROFILES=\n")
    where = {"platform": "win32", "environ": {"APPDATA": str(tmp_path / "none"), "LOCALAPPDATA": str(tmp_path / "none")}}
    assert stacks.prepare_disk_probe(root, ".env", **where) == []      # no Docker Desktop: nothing set
    assert (root / ".disk-probe").is_dir()
    settings = tmp_path / "Roaming" / "Docker"
    settings.mkdir(parents=True)
    (settings / "settings-store.json").write_text(json.dumps({"CustomWslDistroDir": str(disk_folder)}))
    where["environ"]["APPDATA"] = str(tmp_path / "Roaming")
    assert stacks.prepare_disk_probe(root, ".env", **where)
    probe = (tmp_path / "DockerDisk" / "piano-disk-probe")
    assert probe.is_dir()
    assert f"HOST_DISK_PROBE={probe.as_posix()}" in (root / ".env").read_text()
    assert "COMPOSE_PROFILES=" in (root / ".env").read_text()
    assert stacks.prepare_disk_probe(root, ".env", **where) == []      # already set: unchanged


def test_under_docker_desktop_the_host_drive_limits_the_free_space(monkeypatch, tmp_path):
    import shutil
    from collections import namedtuple

    usage = namedtuple("usage", "total used free")
    sizes = {str(tmp_path): 60 * GIB, str(disk.HOST_PROBE): 5 * GIB}
    monkeypatch.setattr(disk, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(shutil, "disk_usage", lambda path: usage(100 * GIB, 0, sizes[str(path)]))
    monkeypatch.setattr(disk.os.path, "ismount", lambda path: str(path) == str(disk.HOST_PROBE))
    monkeypatch.setattr(disk, "in_docker_desktop_vm", lambda: True)
    assert disk.free_bytes() == 5 * GIB
    monkeypatch.setattr(disk, "in_docker_desktop_vm", lambda: False)
    assert disk.free_bytes() == 60 * GIB
