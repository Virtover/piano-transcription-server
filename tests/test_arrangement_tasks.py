"""Piano arrangements in the job lifecycle: which actor takes a job, the style
that reaches the pipeline, restarts, retries on another backend, and the
worker's heartbeat. Routing and the remote request are tested here too."""

import sys
import types

import dramatiq
import pytest

from app.outsource import routing
from app.outsource.handler_input import InvalidInput, requested_style
from app.worker import tasks

URL = "https://example.com/video"


@pytest.fixture
def worker(api_context):
    _, settings, billing = api_context
    settings.billing.provider = "google_play"
    settings.billing.products = {"starter": 10}
    settings.worker.arrangement = True
    billing.credit_purchase("user", "starter", "token", "order")
    return settings, billing


def queue_job(billing, job_id="job", cost=4, **values):
    billing.reserve("user", job_id, cost)
    tasks.redis.hset(
        tasks.job_key(job_id),
        mapping={"status": "queued", "progress": "0.0", "user_id": "user", "full_cost": str(cost), **values},
    )


def job(job_id="job"):
    return tasks.redis.hgetall(tasks.job_key(job_id))


@pytest.fixture
def actors(monkeypatch):
    calls = []
    for name in ("transcribe_job", "arrange_job", "transcribe_remote_job"):
        monkeypatch.setattr(getattr(tasks, name), "send", lambda *args, _name=name, **kwargs: calls.append((_name, args, kwargs)))
    return calls


# --- Which actor takes a job -----------------------------------------------------

@pytest.mark.parametrize(
    ("backend", "fields", "actor"),
    [
        ("local", {}, "transcribe_job"),
        ("local", {"mode": "transcription"}, "transcribe_job"),
        ("local", {"mode": "arrangement", "style": "level1"}, "arrange_job"),
        ("remote", {}, "transcribe_remote_job"),
        ("remote", {"mode": "arrangement", "style": "level3"}, "transcribe_remote_job"),
    ],
)
def test_send_job_picks_the_actor_from_the_backend_and_the_mode(worker, actors, backend, fields, actor):
    _, billing = worker
    queue_job(billing, **fields)

    tasks.send_job("job", backend, URL, extra=1)

    assert actors == [(actor, ("job", URL), {"extra": 1})]


def test_arrangements_have_a_queue_of_their_own():
    assert tasks.arrange_job.queue_name == tasks.ARRANGEMENT_QUEUE == "arrangement"
    assert tasks.transcribe_job.queue_name == tasks.LOCAL_QUEUE
    assert tasks.transcribe_remote_job.queue_name == tasks.REMOTE_QUEUE


@pytest.mark.parametrize(
    ("stored", "style"),
    [
        ({}, None),
        ({"mode": "transcription", "style": "level3"}, None),
        ({"mode": "arrangement", "style": "level1"}, "level1"),
        ({"mode": "arrangement"}, "level2"),
        ({"mode": "arrangement", "style": "bogus"}, "level2"),
    ],
)
def test_arrangement_style_of_a_stored_job(stored, style):
    assert tasks.arrangement_style(stored) == style


def install_pipeline(monkeypatch, seen):
    def transcribe_source(output_dir, arrangement_style, **kwargs):
        seen.append(arrangement_style)
        midi = output_dir / "transcription.mid"
        midi.write_bytes(b"MThd")
        return midi

    module = types.ModuleType("app.transcription.pipeline")
    module.transcribe_source = transcribe_source
    monkeypatch.setitem(sys.modules, "app.transcription.pipeline", module)


@pytest.mark.parametrize(
    ("actor", "fields", "style"),
    [
        ("arrange_job", {"mode": "arrangement", "style": "level3"}, "level3"),
        ("arrange_job", {"mode": "arrangement"}, "level2"),
        ("transcribe_job", {}, None),
    ],
)
def test_the_style_reaches_the_pipeline_through_the_job_context(worker, monkeypatch, actor, fields, style):
    _, billing = worker
    seen = []
    install_pipeline(monkeypatch, seen)
    queue_job(billing, **fields)

    getattr(tasks, actor).fn("job", URL)

    assert seen == [style]
    assert job()["status"] == "completed"
    assert billing.get_balance("user") == 6


def test_a_failed_arrangement_gives_its_minutes_back(worker, monkeypatch):
    _, billing = worker

    def broken(**kwargs):
        raise RuntimeError("model crashed")

    module = types.ModuleType("app.transcription.pipeline")
    module.transcribe_source = broken
    monkeypatch.setitem(sys.modules, "app.transcription.pipeline", module)
    queue_job(billing, mode="arrangement", style="level1", backend="local")
    routing.mark_active(tasks.redis, "local", "job")

    tasks.arrange_job.fn("job", URL)

    assert job()["status"] == "failed"
    assert billing.get_balance("user") == 10
    assert billing.reserved_job_ids() == []
    assert tasks.redis.scard(routing.ACTIVE_KEYS["local"]) == 0


# --- Restarts --------------------------------------------------------------------

def test_a_queued_arrangement_is_failed_and_refunded_when_the_feature_is_off(worker, actors):
    settings, billing = worker
    settings.worker.arrangement = False
    queue_job(billing, mode="arrangement", style="level2", source_url=URL, backend="local")
    queue_job(billing, "plain", cost=2, source_url=URL, backend="local")

    tasks.recover_active_jobs("restarted")

    failed = job()
    assert failed["status"] == "failed"
    assert failed["error"] == tasks.ARRANGEMENT_OFF
    assert failed["error_code"] == tasks.INTERRUPTED
    assert billing.reserved_job_ids() == ["plain"]
    assert billing.get_balance("user") == 8
    assert tasks.redis.smembers(routing.ACTIVE_KEYS["local"]) == {"plain"}
    # The transcription is requeued as before, and nothing was sent for the arrangement.
    assert actors == [("transcribe_job", ("plain", URL), {})]


def test_a_queued_arrangement_is_requeued_when_the_feature_is_on(worker, actors):
    _, billing = worker
    queue_job(billing, mode="arrangement", style="level2", source_url=URL, backend="local")

    tasks.recover_active_jobs("restarted")

    assert job()["status"] == "queued"
    assert actors == [("arrange_job", ("job", URL), {})]
    assert billing.reserved_job_ids() == ["job"]


def test_a_remote_arrangement_is_requeued_remote_whatever_the_feature_says(worker, actors):
    settings, billing = worker
    settings.worker.arrangement = False
    settings.outsource.provider = "runpod"
    queue_job(billing, mode="arrangement", style="level2", source_url=URL, backend="remote")

    tasks.recover_active_jobs("restarted")

    # The server has no worker for it, but RunPod does not need one.
    assert job()["status"] == "failed"

    settings.worker.arrangement = True
    queue_job(billing, "again", mode="arrangement", style="level2", source_url=URL, backend="remote")
    tasks.recover_active_jobs("restarted")

    assert actors == [("transcribe_remote_job", ("again", URL), {})]


# --- Retrying on the other backend -----------------------------------------------

def remote_failure():
    from app.outsource.provider import RemoteFailed

    return RemoteFailed("boom")


def test_a_failed_remote_arrangement_goes_back_locally_only_to_a_worker_that_arranges(worker):
    settings, billing = worker
    settings.outsource.fallback_local = True
    queue_job(billing, mode="arrangement", style="level2")

    assert tasks.retry_backend("job", "remote", remote_failure()) is None
    routing.publish_heartbeat(tasks.redis, 2, 60.0, "plain", arrangement=False)
    assert tasks.retry_backend("job", "remote", remote_failure()) is None
    routing.publish_heartbeat(tasks.redis, 2, 60.0, "arranging", arrangement=True)
    assert tasks.retry_backend("job", "remote", remote_failure()) == "local"


def test_a_failed_remote_transcription_goes_back_locally_as_before(worker):
    settings, billing = worker
    settings.outsource.fallback_local = True
    queue_job(billing)

    assert tasks.retry_backend("job", "remote", remote_failure()) == "local"


def test_a_failed_remote_arrangement_stays_failed_without_the_local_fallback(worker):
    settings, billing = worker
    settings.outsource.fallback_local = False
    queue_job(billing, mode="arrangement")
    routing.publish_heartbeat(tasks.redis, 2, 60.0, "arranging", arrangement=True)

    assert tasks.retry_backend("job", "remote", remote_failure()) is None


def test_a_local_arrangement_out_of_memory_goes_remote_once(worker):
    from app.transcription.batching import GpuOutOfMemory

    settings, billing = worker
    settings.outsource.provider = "runpod"
    queue_job(billing, mode="arrangement")

    assert tasks.retry_backend("job", "local", GpuOutOfMemory()) == "remote"


def test_a_moved_arrangement_is_sent_to_the_remote_actor_and_back_to_the_arrangement_actor(worker, actors):
    _, billing = worker
    queue_job(billing, mode="arrangement", style="level1", source_url=URL, backend="local")
    tasks.update_job("job", status="processing")
    routing.mark_active(tasks.redis, "local", "job")

    assert tasks.requeue_job("job", "local", "remote", "out of memory")
    assert tasks.requeue_job("job", "remote", "local", "failed") is False  # queued, not processing
    tasks.update_job("job", status="processing")
    assert tasks.requeue_job("job", "remote", "local", "failed")

    assert [name for name, _, _ in actors] == ["transcribe_remote_job", "arrange_job"]
    assert (job()["mode"], job()["style"]) == ("arrangement", "level1")


# --- The worker's heartbeat ------------------------------------------------------

def dramatiq_worker(queues):
    return dramatiq.Worker(tasks.broker, queues=queues, worker_threads=1)


@pytest.mark.parametrize(
    ("queues", "enabled", "published"),
    [
        ([tasks.LOCAL_QUEUE, tasks.ARRANGEMENT_QUEUE], True, True),
        (None, True, True),
        ([tasks.LOCAL_QUEUE], True, False),
        ([tasks.LOCAL_QUEUE, tasks.ARRANGEMENT_QUEUE], False, False),
    ],
)
def test_the_heartbeat_says_whether_the_worker_arranges(worker, monkeypatch, queues, enabled, published):
    settings, _ = worker
    settings.worker.arrangement = enabled
    beats = []
    middleware = tasks.LocalWorkerHeartbeat()

    def publish(redis, capacity, average, **options):
        beats.append(options)
        middleware._stop.set()

    monkeypatch.setattr(tasks.routing, "publish_heartbeat", publish)
    monkeypatch.setattr(tasks.threading, "Thread", lambda target, **options: types.SimpleNamespace(start=target))

    middleware.after_worker_boot(tasks.broker, dramatiq_worker(queues))

    assert beats == [{"arrangement": published}]


# --- Routing ---------------------------------------------------------------------

def test_the_heartbeat_field_round_trips(api_context):
    routing.publish_heartbeat(tasks.redis, 2, 60.0, "a", arrangement=True)
    routing.publish_heartbeat(tasks.redis, 3, 90.0, "b")

    assert tasks.redis.hget(routing.heartbeat_key("a"), "arrangement") == "1"
    assert tasks.redis.hget(routing.heartbeat_key("b"), "arrangement") == "0"
    assert [(worker["id"], worker["arrangement"]) for worker in routing.local_workers(tasks.redis)] == [("a", True), ("b", False)]
    assert routing.arrangement_workers(tasks.redis) == (2, 1)


def test_a_heartbeat_from_before_arrangements_does_not_arrange(api_context):
    routing.publish_heartbeat(tasks.redis, 2, 60.0, "old")
    tasks.redis.hdel(routing.heartbeat_key("old"), "arrangement")

    assert routing.local_workers(tasks.redis)[0]["arrangement"] is False


def test_local_capacity_counts_only_workers_that_arrange_when_asked(api_context):
    routing.publish_heartbeat(tasks.redis, 2, 100.0, "a", arrangement=True)
    routing.publish_heartbeat(tasks.redis, 6, 40.0, "b", arrangement=False)

    assert routing.local_capacity(tasks.redis) == (8, pytest.approx(55.0))
    assert routing.local_capacity(tasks.redis, arrangement=True) == (2, 100.0)
    assert routing.estimated_local_wait(tasks.redis) == 0
    # Busy for the arranging worker's capacity, though the other has room.
    for index in range(2):
        tasks.redis.sadd(routing.ACTIVE_KEYS["local"], f"busy-{index}")
    assert routing.estimated_local_wait(tasks.redis) == 0
    assert routing.estimated_local_wait(tasks.redis, arrangement=True) == 100.0


def test_no_arranging_worker_means_no_capacity_for_arrangements(api_context):
    routing.publish_heartbeat(tasks.redis, 4, 60.0, "a")

    assert routing.local_capacity(tasks.redis, arrangement=True)[0] == 0
    assert routing.estimated_local_wait(tasks.redis, arrangement=True) == float("inf")


def test_choose_backend_sends_an_arrangement_remote_when_no_live_worker_arranges(api_context):
    _, settings, _ = api_context
    settings.outsource.provider = "runpod"
    settings.outsource.max_concurrency = 4
    routing.publish_heartbeat(tasks.redis, 4, 60.0, "plain")

    assert routing.choose_backend(tasks.redis, "arranged", arrangement=True) == "remote"
    assert routing.choose_backend(tasks.redis, "plain-job") == "local"
    assert tasks.redis.smembers(routing.ACTIVE_KEYS["remote"]) == {"arranged"}
    assert tasks.redis.smembers(routing.ACTIVE_KEYS["local"]) == {"plain-job"}


def test_choose_backend_keeps_an_arrangement_local_when_a_worker_arranges(api_context):
    _, settings, _ = api_context
    settings.outsource.provider = "runpod"
    routing.publish_heartbeat(tasks.redis, 4, 60.0, "arranging", arrangement=True)

    assert routing.choose_backend(tasks.redis, "job", arrangement=True) == "local"


def test_choose_backend_keeps_an_arrangement_local_without_outsourcing(api_context):
    routing.publish_heartbeat(tasks.redis, 4, 60.0, "plain")

    assert routing.choose_backend(tasks.redis, "job", arrangement=True) == "local"


def test_an_arrangement_waits_locally_when_the_remote_slots_are_full(api_context):
    _, settings, _ = api_context
    settings.outsource.provider = "runpod"
    settings.outsource.max_concurrency = 1
    routing.publish_heartbeat(tasks.redis, 4, 60.0, "plain")
    tasks.redis.sadd(routing.ACTIVE_KEYS["remote"], "other")

    assert routing.choose_backend(tasks.redis, "job", arrangement=True) == "local"


# --- The remote request ----------------------------------------------------------

def test_the_remote_request_has_mode_and_style_only_for_arrangements(api_context, monkeypatch, tmp_path):
    import base64

    from app.outsource import remote
    from app.outsource.provider import RemoteStatus
    from app.transcription.audio import DownloadedAudio

    class Provider:
        def __init__(self):
            self.submitted = []

        def submit(self, payload):
            self.submitted.append(payload)
            return "remote-1"

        def status(self, remote_id, timeout):
            midi = b"MThd\x00\x00\x00\x06\x00\x01\x00\x01\x01\xe0MTrk\x00\x00\x00\x04\x00\xff\x2f\x00"
            return RemoteStatus("completed", output={"midi_base64": base64.b64encode(midi).decode()})

        def cancel(self, remote_id):
            pass

    class Storage:
        def upload(self, path):
            return "audio/0.flac"

        def presigned_url(self, key, expires_seconds):
            return "https://storage.example/audio"

        def delete(self, key):
            pass

    def fetch(output_dir, progress_callback=None, metadata_callback=None, **kwargs):
        output_dir.mkdir(parents=True, exist_ok=True)
        wav = output_dir / "audio.wav"
        wav.write_bytes(b"RIFF")
        return DownloadedAudio(path=wav, metadata={})

    monkeypatch.setattr(remote, "fetch_audio", fetch)
    monkeypatch.setattr(remote, "convert_to_flac", lambda wav, output_dir, cancellation_callback=None: wav)
    monkeypatch.setattr(remote.time, "sleep", lambda seconds: None)
    provider = Provider()
    tasks.redis.hset(tasks.job_key("job"), mapping={"status": "processing", "progress": "0.0"})

    for style in (None, "level3"):
        context = tasks.JobContext("job", tmp_path / (style or "plain"), arrangement_style=style)
        remote.transcribe_remotely(
            context, source_url=URL, provider=provider, storage=Storage(), arrangement_style=context.arrangement_style
        )

    assert provider.submitted == [
        {"audio_url": "https://storage.example/audio", "job_id": "job"},
        {"audio_url": "https://storage.example/audio", "job_id": "job", "mode": "arrangement", "style": "level3"},
    ]


@pytest.mark.parametrize(
    ("job_input", "style"),
    [
        ({}, None),
        ({"mode": "transcription"}, None),
        ({"mode": "transcription", "style": "level9"}, None),
        ({"mode": "arrangement"}, "level2"),
        ({"mode": "arrangement", "style": "level1"}, "level1"),
        ({"mode": "arrangement", "style": "level3"}, "level3"),
    ],
)
def test_the_handler_reads_the_requested_style(job_input, style):
    assert requested_style(job_input) == style


@pytest.mark.parametrize(
    "job_input",
    [{"mode": "remix"}, {"mode": None}, {"mode": "arrangement", "style": "dense"}, {"mode": "arrangement", "style": None},
     {"mode": "arrangement", "style": ["level1"]}],
)
def test_the_handler_refuses_other_values(job_input):
    with pytest.raises(InvalidInput):
        requested_style(job_input)
