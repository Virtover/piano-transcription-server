import base64
import sqlite3
import sys
import types
from pathlib import Path

import pytest

from app.outsource import remote, routing
from app.outsource.provider import RemoteFailed, RemoteStatus
from app.transcription.audio import DownloadedAudio
from app.transcription.batching import GpuOutOfMemory
from app.worker import tasks

MIDI = b"MThd\x00\x00\x00\x06\x00\x01\x00\x01\x01\xe0MTrk\x00\x00\x00\x04\x00\xff\x2f\x00"
URL = "https://example.com/video"


class FakeProvider:
    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.submitted = []
        self.cancelled = []
        self.on_status = None

    def submit(self, payload):
        self.submitted.append(payload)
        return "remote-1"

    def status(self, remote_id, timeout):
        if self.on_status:
            self.on_status()
        item = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        if isinstance(item, Exception):
            raise item
        return item

    def cancel(self, remote_id):
        self.cancelled.append(remote_id)


class FakeStorage:
    def __init__(self, fail_upload=False):
        self.fail_upload = fail_upload
        self.objects = {}
        self.deleted = []

    def upload(self, path):
        if self.fail_upload:
            raise RemoteFailed("Could not upload audio to storage (EndpointConnectionError)")
        key = f"audio/{len(self.objects)}.flac"
        self.objects[key] = Path(path).read_bytes()
        return key

    def presigned_url(self, key, expires_seconds):
        return f"https://storage.example/bucket/{key}?X-Amz-Signature=secret"

    def delete(self, key):
        self.deleted.append(key)


def completed(midi=MIDI):
    return RemoteStatus("completed", output={"midi_base64": base64.b64encode(midi).decode(), "timing": {"total_seconds": 3}})


@pytest.fixture
def remote_env(api_context, monkeypatch):
    _, settings, billing = api_context
    settings.billing.provider = "google_play"
    settings.billing.products = {"starter": 10}
    settings.outsource.provider = "runpod"
    settings.outsource.max_concurrency = 4
    settings.outsource.fallback_local = True
    billing.credit_purchase("user", "starter", "token", "order")

    state = {
        "provider": FakeProvider([completed()]),
        "storage": FakeStorage(),
        "progress": [],
        "local_sent": [],
        "remote_sent": [],
        "fetched": [],
    }

    def fake_fetch(output_dir, progress_callback=None, metadata_callback=None, **kwargs):
        state["fetched"].append(kwargs)
        output_dir.mkdir(parents=True, exist_ok=True)
        progress_callback(0.05)
        metadata_callback({"title": "Nocturne", "duration": 130.0})
        wav = output_dir / "audio.wav"
        wav.write_bytes(b"RIFF")
        return DownloadedAudio(path=wav, metadata={"title": "Nocturne"})

    def fake_flac(wav_path, output_dir, cancellation_callback=None):
        flac = output_dir / "audio.flac"
        flac.write_bytes(b"fLaC")
        return flac

    monkeypatch.setattr(remote, "fetch_audio", fake_fetch)
    monkeypatch.setattr(remote, "convert_to_flac", fake_flac)
    monkeypatch.setattr(remote, "get_provider", lambda: state["provider"])
    monkeypatch.setattr(remote.S3Storage, "from_settings", classmethod(lambda cls: state["storage"]))
    monkeypatch.setattr(remote.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(tasks.transcribe_job, "send", lambda *args, **kwargs: state["local_sent"].append(args))
    monkeypatch.setattr(tasks.transcribe_remote_job, "send", lambda *args, **kwargs: state["remote_sent"].append(args))

    original_update = tasks.update_job

    def recording_update(job_id, **values):
        if "progress" in values:
            state["progress"].append(float(values["progress"]))
        original_update(job_id, **values)

    monkeypatch.setattr(tasks, "update_job", recording_update)
    state["settings"] = settings
    state["billing"] = billing
    return state


def queue(billing, backend="remote", cost=4, **fields):
    billing.reserve("user", "job", cost)
    tasks.redis.hset(
        tasks.job_key("job"),
        mapping={"status": "queued", "progress": "0.0", "user_id": "user", "full_cost": str(cost), "source_url": URL, "backend": backend, **fields},
    )
    routing.mark_active(tasks.redis, backend, "job")


def job():
    return tasks.redis.hgetall(tasks.job_key("job"))


def reservation(settings):
    with sqlite3.connect(settings.database.path) as connection:
        return connection.execute(
            "SELECT status, charged_minutes FROM transcription_reservations WHERE job_id = 'job'"
        ).fetchone()


def run_remote():
    tasks.transcribe_remote_job.fn("job", URL)


def test_remote_job_audio_is_cut_at_the_charged_duration(remote_env):
    queue(remote_env["billing"], duration="245.0")

    run_remote()

    assert remote_env["fetched"][0]["max_seconds"] == 245.0


def test_successful_remote_job(remote_env):
    billing = remote_env["billing"]
    remote_env["provider"] = FakeProvider([
        RemoteStatus("queued"),
        RemoteStatus("running", progress=0.5),
        RemoteStatus("running", progress=1.0),
        completed(),
    ])
    queue(billing)

    run_remote()

    result = job()
    assert result["status"] == "completed"
    assert result["remote_id"] == "remote-1"
    assert Path(result["result"]).read_bytes() == MIDI
    assert remote_env["provider"].submitted == [
        {"audio_url": "https://storage.example/bucket/audio/0.flac?X-Amz-Signature=secret", "job_id": "job"}
    ]
    assert remote_env["storage"].deleted == ["audio/0.flac"]
    assert billing.get_balance("user") == 6
    assert reservation(remote_env["settings"]) == ("settled", 4)
    assert tasks.redis.scard(routing.ACTIVE_KEYS["remote"]) == 0
    assert sorted(path.name for path in Path(result["result"]).parent.iterdir()) == ["transcription.mid"]


def test_remote_progress_is_mapped_into_model_range(remote_env):
    remote_env["provider"] = FakeProvider([RemoteStatus("running", progress=0.0), RemoteStatus("running", progress=0.5), completed()])
    queue(remote_env["billing"])

    run_remote()

    assert remote_env["progress"] == [0.05, 0.07, 0.1, 0.15, 0.55, 0.96]
    assert remote.map_progress(-1) == 0.15
    assert remote.map_progress(2) == 0.95
    assert remote.map_progress(0.25) == pytest.approx(0.35)


def test_cancellation_cancels_remote_job(remote_env):
    billing = remote_env["billing"]
    provider = remote_env["provider"] = FakeProvider([RemoteStatus("running", progress=0.5)])
    queue(billing, cost=10)

    calls = {"count": 0}

    def on_status():
        calls["count"] += 1
        if calls["count"] == 2:
            tasks.redis.hset(tasks.job_key("job"), "status", "cancelled")

    provider.on_status = on_status
    run_remote()

    assert provider.cancelled == ["remote-1"]
    assert job()["status"] == "cancelled"
    assert remote_env["storage"].deleted == ["audio/0.flac"]
    # Cancelled at 55% of a 10-minute job: ceil(0.6 * 10 * 0.55) = 4 minutes.
    assert reservation(remote_env["settings"]) == ("settled", 4)
    assert tasks.redis.scard(routing.ACTIVE_KEYS["remote"]) == 0


@pytest.mark.parametrize(
    "provider_statuses",
    [
        [RemoteStatus("failed", error="CUDA error: device-side assert")],
        [RemoteStatus("cancelled")],
        [RemoteStatus("completed", output={"midi_base64": base64.b64encode(b"not midi").decode()})],
        [RemoteStatus("completed", output={"midi_base64": "%%%"})],
        [RemoteFailed("RunPod is unreachable (ConnectionError)")],
    ],
    ids=["failed", "cancelled-remotely", "not-midi", "bad-base64", "unreachable"],
)
def test_remote_failure_falls_back_to_local_once(remote_env, provider_statuses):
    billing = remote_env["billing"]
    remote_env["provider"] = FakeProvider(provider_statuses)
    queue(billing)

    run_remote()

    result = job()
    assert (result["status"], result["backend"], result["outsourced"]) == ("queued", "local", "1")
    assert remote_env["local_sent"] == [("job", URL)]
    assert reservation(remote_env["settings"]) == ("reserved", None)
    assert remote_env["storage"].deleted == ["audio/0.flac"]
    assert tasks.redis.smembers(routing.ACTIVE_KEYS["local"]) == {"job"}
    assert tasks.redis.scard(routing.ACTIVE_KEYS["remote"]) == 0


def test_fallback_job_completes_locally_and_is_charged_once(remote_env, monkeypatch):
    billing = remote_env["billing"]
    remote_env["provider"] = FakeProvider([RemoteStatus("running", progress=0.5), RemoteStatus("failed", error="boom")])
    queue(billing)
    run_remote()
    progress_after_remote = float(job()["progress"])

    def transcribe_source(output_dir, progress_callback, **kwargs):
        progress_callback(0.05)
        progress_callback(0.9)
        midi = output_dir / "transcription.mid"
        midi.write_bytes(MIDI)
        return midi

    module = types.ModuleType("app.transcription.pipeline")
    module.transcribe_source = transcribe_source
    monkeypatch.setitem(sys.modules, "app.transcription.pipeline", module)
    remote_env["progress"].clear()
    tasks.transcribe_job.fn("job", URL)

    assert job()["status"] == "completed"
    assert progress_after_remote == 0.55
    # Progress does not drop back to 0.05 after the fallback.
    assert remote_env["progress"] == [0.9]
    assert billing.get_balance("user") == 6
    assert reservation(remote_env["settings"]) == ("settled", 4)
    assert remote_env["remote_sent"] == []


def test_remote_failure_without_fallback_fails_and_releases(remote_env):
    remote_env["settings"].outsource.fallback_local = False
    remote_env["provider"] = FakeProvider([RemoteStatus("failed", error="CUDA error at /secret/path")])
    queue(remote_env["billing"])

    run_remote()

    result = job()
    assert result["status"] == "failed"
    assert result["error"] == "Remote transcription failed"
    assert remote_env["local_sent"] == []
    assert remote_env["billing"].get_balance("user") == 10
    assert reservation(remote_env["settings"]) == ("released", None)
    assert remote_env["storage"].deleted == ["audio/0.flac"]
    assert tasks.redis.scard(routing.ACTIVE_KEYS["remote"]) == 0


def test_remote_timeout_cancels_and_falls_back(remote_env, monkeypatch):
    remote_env["settings"].outsource.timeout = 5
    clock = {"now": 0.0}
    monkeypatch.setattr(remote.time, "monotonic", lambda: clock["now"])
    provider = remote_env["provider"] = FakeProvider([RemoteStatus("queued")])

    def advance():
        clock["now"] += 2

    provider.on_status = advance
    queue(remote_env["billing"])

    run_remote()

    assert provider.cancelled == ["remote-1"]
    assert job()["status"] == "queued"
    assert remote_env["local_sent"] == [("job", URL)]
    assert remote_env["storage"].deleted == ["audio/0.flac"]


def test_transient_status_errors_are_retried(remote_env):
    remote_env["provider"] = FakeProvider([
        RemoteFailed("RunPod is unreachable (ReadTimeout)"),
        RemoteFailed("RunPod status failed with HTTP 502"),
        completed(),
    ])
    queue(remote_env["billing"])

    run_remote()

    assert job()["status"] == "completed"
    assert remote_env["provider"].cancelled == []


def test_upload_failure_falls_back_without_storage_cleanup(remote_env):
    remote_env["storage"] = FakeStorage(fail_upload=True)
    queue(remote_env["billing"])

    run_remote()

    assert job()["status"] == "queued"
    assert remote_env["storage"].deleted == []
    assert remote_env["provider"].submitted == []
    assert remote_env["local_sent"] == [("job", URL)]


def test_download_failure_is_not_retried_locally(remote_env, monkeypatch):
    def broken_fetch(*args, **kwargs):
        raise RuntimeError("yt-dlp failed")

    monkeypatch.setattr(remote, "fetch_audio", broken_fetch)
    queue(remote_env["billing"])

    run_remote()

    assert job()["status"] == "failed"
    assert remote_env["local_sent"] == []
    assert reservation(remote_env["settings"]) == ("released", None)


def test_remote_errors_never_expose_presigned_urls(remote_env):
    remote_env["settings"].outsource.fallback_local = False
    remote_env["provider"] = FakeProvider([RemoteStatus("failed", error="could not GET https://storage.example/bucket/key?X-Amz-Signature=secret")])
    queue(remote_env["billing"])

    run_remote()

    assert "secret" not in str(job())


def test_cancelled_while_falling_back_stays_cancelled(remote_env):
    provider = remote_env["provider"] = FakeProvider([RemoteStatus("failed", error="boom")])
    provider.on_status = lambda: tasks.redis.hset(tasks.job_key("job"), "status", "cancelled")
    queue(remote_env["billing"])

    run_remote()

    assert job()["status"] == "cancelled"
    assert remote_env["local_sent"] == []


def test_upload_job_is_retried_from_the_kept_upload(remote_env):
    remote_env["provider"] = FakeProvider([RemoteStatus("failed", error="boom")])
    queue(remote_env["billing"], source="upload", title="My Song", upload_title="My Song")
    tasks.redis.hdel(tasks.job_key("job"), "source_url")
    upload = tasks.job_dir("job") / tasks.UPLOAD_FILE_NAME
    upload.parent.mkdir(parents=True)
    upload.write_bytes(b"ID3")

    tasks.transcribe_remote_job.fn("job", None, "My Song")

    assert remote_env["local_sent"] == [("job", None, "My Song")]
    assert upload.read_bytes() == b"ID3"


def test_local_out_of_memory_is_sent_remote_once(remote_env, monkeypatch):
    def out_of_memory(**kwargs):
        raise GpuOutOfMemory()

    module = types.ModuleType("app.transcription.pipeline")
    module.transcribe_source = out_of_memory
    monkeypatch.setitem(sys.modules, "app.transcription.pipeline", module)
    queue(remote_env["billing"], backend="local")

    tasks.transcribe_job.fn("job", URL)

    assert (job()["status"], job()["backend"]) == ("queued", "remote")
    assert remote_env["remote_sent"] == [("job", URL)]
    assert tasks.redis.smembers(routing.ACTIVE_KEYS["remote"]) == {"job"}

    # The remote attempt fails and falls back; a second local OOM fails for good.
    remote_env["provider"] = FakeProvider([RemoteStatus("failed", error="boom")])
    run_remote()
    tasks.transcribe_job.fn("job", URL)

    assert job()["status"] == "failed"
    assert job()["error"] == "The GPU ran out of memory for this transcription"
    assert remote_env["remote_sent"] == [("job", URL)]
    assert reservation(remote_env["settings"]) == ("released", None)


def test_local_out_of_memory_without_outsourcing_fails(remote_env, monkeypatch):
    remote_env["settings"].outsource.provider = "none"

    def out_of_memory(**kwargs):
        raise GpuOutOfMemory()

    module = types.ModuleType("app.transcription.pipeline")
    module.transcribe_source = out_of_memory
    monkeypatch.setitem(sys.modules, "app.transcription.pipeline", module)
    queue(remote_env["billing"], backend="local")

    tasks.transcribe_job.fn("job", URL)

    assert job()["status"] == "failed"
    assert remote_env["remote_sent"] == []


def test_successful_job_records_worker_capacity(remote_env):
    from app.outsource import capacity

    output = completed().output | {"worker_capacity": 12}
    remote_env["provider"] = FakeProvider([RemoteStatus("completed", output=output)])
    queue(remote_env["billing"])

    run_remote()

    assert tasks.redis.hget(capacity.CAPACITY_KEY, "jobs_per_worker") == "12"
