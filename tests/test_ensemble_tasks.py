"""Ensemble jobs in the job lifecycle: which actor takes a job, the flag that
reaches the pipeline, restarts, retries on another backend, and the worker's
heartbeat. Routing and the remote request are tested here too. Stored values
(mode "arrangement", queue "arrangement", the heartbeat field, the actor name
"arrange_job") kept their old spelling and are checked as such."""

import sys
import types

import dramatiq
import pytest

from app.outsource import routing
from app.outsource.handler_input import InvalidInput, requested_ensemble
from app.worker import tasks

URL = "https://example.com/video"


@pytest.fixture
def worker(api_context):
    _, settings, billing = api_context
    settings.billing.provider = "google_play"
    settings.billing.products = {"starter": 10}
    settings.worker.ensemble = True
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
    for name in ("transcribe_job", "ensemble_job", "transcribe_remote_job"):
        monkeypatch.setattr(getattr(tasks, name), "send", lambda *args, _name=name, **kwargs: calls.append((_name, args, kwargs)))
    return calls


# --- Which actor takes a job -----------------------------------------------------

@pytest.mark.parametrize(
    ("backend", "fields", "actor"),
    [
        ("local", {}, "transcribe_job"),
        ("local", {"mode": "transcription"}, "transcribe_job"),
        ("local", {"mode": "arrangement", "mode_name": "ensemble"}, "ensemble_job"),
        ("local", {"mode": "arrangement", "style": "level1"}, "ensemble_job"),
        ("remote", {}, "transcribe_remote_job"),
        ("remote", {"mode": "arrangement", "mode_name": "ensemble"}, "transcribe_remote_job"),
    ],
)
def test_send_job_picks_the_actor_from_the_backend_and_the_mode(worker, actors, backend, fields, actor):
    _, billing = worker
    queue_job(billing, **fields)

    tasks.send_job("job", backend, URL, extra=1)

    assert actors == [(actor, ("job", URL), {"extra": 1})]


def test_ensemble_jobs_have_a_queue_of_their_own():
    # Dramatiq's queue and actor names are stored in queued messages: unchanged.
    assert tasks.ensemble_job.queue_name == tasks.ENSEMBLE_QUEUE == "arrangement"
    assert tasks.ensemble_job.actor_name == "arrange_job"
    assert tasks.ENSEMBLE == "arrangement"
    assert tasks.transcribe_job.queue_name == tasks.LOCAL_QUEUE
    assert tasks.transcribe_remote_job.queue_name == tasks.REMOTE_QUEUE


@pytest.mark.parametrize(
    ("stored", "ensemble"),
    [
        ({}, False),
        ({"mode": "transcription"}, False),
        ({"mode": "transcription", "style": "level3"}, False),
        ({"mode": "arrangement"}, True),
        ({"mode": "arrangement", "mode_name": "ensemble"}, True),
        ({"mode": "arrangement", "style": "bogus"}, True),
    ],
)
def test_is_ensemble_reads_the_stored_mode(stored, ensemble):
    assert tasks.is_ensemble(stored) is ensemble


def install_pipeline(monkeypatch, seen):
    def transcribe_source(output_dir, ensemble, **kwargs):
        seen.append(ensemble)
        midi = output_dir / "transcription.mid"
        midi.write_bytes(b"MThd")
        return midi

    module = types.ModuleType("app.transcription.pipeline")
    module.transcribe_source = transcribe_source
    monkeypatch.setitem(sys.modules, "app.transcription.pipeline", module)


@pytest.mark.parametrize(
    ("actor", "fields", "ensemble"),
    [
        ("ensemble_job", {"mode": "arrangement", "mode_name": "ensemble"}, True),
        ("ensemble_job", {"mode": "arrangement"}, True),
        ("transcribe_job", {}, False),
        ("transcribe_job", {"mode": "transcription"}, False),
    ],
)
def test_the_ensemble_flag_reaches_the_pipeline_through_the_job_context(worker, monkeypatch, actor, fields, ensemble):
    _, billing = worker
    seen = []
    install_pipeline(monkeypatch, seen)
    queue_job(billing, **fields)

    getattr(tasks, actor).fn("job", URL)

    assert seen == [ensemble]
    assert job()["status"] == "completed"
    assert billing.get_balance("user") == 6


def test_a_failed_ensemble_gives_its_minutes_back(worker, monkeypatch):
    _, billing = worker

    def broken(**kwargs):
        raise RuntimeError("model crashed")

    module = types.ModuleType("app.transcription.pipeline")
    module.transcribe_source = broken
    monkeypatch.setitem(sys.modules, "app.transcription.pipeline", module)
    queue_job(billing, mode="arrangement", backend="local")
    routing.mark_active(tasks.redis, "local", "job")

    tasks.ensemble_job.fn("job", URL)

    assert job()["status"] == "failed"
    assert billing.get_balance("user") == 10
    assert billing.reserved_job_ids() == []
    assert tasks.redis.scard(routing.ACTIVE_KEYS["local"]) == 0


# --- Restarts --------------------------------------------------------------------

def test_a_queued_ensemble_is_failed_and_refunded_when_the_feature_is_off(worker, actors):
    settings, billing = worker
    settings.worker.ensemble = False
    queue_job(billing, mode="arrangement", source_url=URL, backend="local")
    queue_job(billing, "plain", cost=2, source_url=URL, backend="local")

    tasks.recover_active_jobs("restarted")

    failed = job()
    assert failed["status"] == "failed"
    assert failed["error"] == tasks.ENSEMBLE_OFF
    assert failed["error_code"] == tasks.INTERRUPTED
    assert billing.reserved_job_ids() == ["plain"]
    assert billing.get_balance("user") == 8
    assert tasks.redis.smembers(routing.ACTIVE_KEYS["local"]) == {"plain"}
    # The transcription is requeued as before, and nothing was sent for the ensemble job.
    assert actors == [("transcribe_job", ("plain", URL), {})]


def test_a_queued_ensemble_is_requeued_when_the_feature_is_on(worker, actors):
    _, billing = worker
    queue_job(billing, mode="arrangement", source_url=URL, backend="local")

    tasks.recover_active_jobs("restarted")

    assert job()["status"] == "queued"
    assert actors == [("ensemble_job", ("job", URL), {})]
    assert billing.reserved_job_ids() == ["job"]


def test_a_remote_ensemble_is_requeued_remote_whatever_the_feature_says(worker, actors):
    settings, billing = worker
    settings.worker.ensemble = False
    settings.outsource.provider = "runpod"
    queue_job(billing, mode="arrangement", source_url=URL, backend="remote")

    tasks.recover_active_jobs("restarted")

    # The server has no worker for it, but RunPod does not need one.
    assert job()["status"] == "failed"

    settings.worker.ensemble = True
    queue_job(billing, "again", mode="arrangement", source_url=URL, backend="remote")
    tasks.recover_active_jobs("restarted")

    assert actors == [("transcribe_remote_job", ("again", URL), {})]


# --- Retrying on the other backend -----------------------------------------------

def remote_failure():
    from app.outsource.provider import RemoteFailed

    return RemoteFailed("boom")


def test_a_failed_remote_ensemble_goes_back_locally_only_to_a_worker_that_takes_ensembles(worker):
    settings, billing = worker
    settings.outsource.fallback_local = True
    queue_job(billing, mode="arrangement")

    assert tasks.retry_backend("job", "remote", remote_failure()) is None
    routing.publish_heartbeat(tasks.redis, 2, 60.0, "plain", ensemble=False)
    assert tasks.retry_backend("job", "remote", remote_failure()) is None
    routing.publish_heartbeat(tasks.redis, 2, 60.0, "ensembling", ensemble=True)
    assert tasks.retry_backend("job", "remote", remote_failure()) == "local"


def test_a_failed_remote_transcription_goes_back_locally_as_before(worker):
    settings, billing = worker
    settings.outsource.fallback_local = True
    queue_job(billing)

    assert tasks.retry_backend("job", "remote", remote_failure()) == "local"


def test_a_failed_remote_ensemble_stays_failed_without_the_local_fallback(worker):
    settings, billing = worker
    settings.outsource.fallback_local = False
    queue_job(billing, mode="arrangement")
    routing.publish_heartbeat(tasks.redis, 2, 60.0, "ensembling", ensemble=True)

    assert tasks.retry_backend("job", "remote", remote_failure()) is None


def test_a_local_ensemble_out_of_memory_goes_remote_once(worker):
    from app.transcription.batching import GpuOutOfMemory

    settings, billing = worker
    settings.outsource.provider = "runpod"
    queue_job(billing, mode="arrangement")

    assert tasks.retry_backend("job", "local", GpuOutOfMemory()) == "remote"


def test_a_moved_ensemble_is_sent_to_the_remote_actor_and_back_to_the_ensemble_actor(worker, actors):
    _, billing = worker
    queue_job(billing, mode="arrangement", mode_name="ensemble", source_url=URL, backend="local")
    tasks.update_job("job", status="processing")
    routing.mark_active(tasks.redis, "local", "job")

    assert tasks.requeue_job("job", "local", "remote", "out of memory")
    assert tasks.requeue_job("job", "remote", "local", "failed") is False  # queued, not processing
    tasks.update_job("job", status="processing")
    assert tasks.requeue_job("job", "remote", "local", "failed")

    assert [name for name, _, _ in actors] == ["transcribe_remote_job", "ensemble_job"]
    assert (job()["mode"], job()["mode_name"]) == ("arrangement", "ensemble")


# --- The worker's heartbeat ------------------------------------------------------

def dramatiq_worker(queues):
    return dramatiq.Worker(tasks.broker, queues=queues, worker_threads=1)


@pytest.mark.parametrize(
    ("queues", "enabled", "published"),
    [
        ([tasks.LOCAL_QUEUE, tasks.ENSEMBLE_QUEUE], True, True),
        (None, True, True),
        ([tasks.LOCAL_QUEUE], True, False),
        ([tasks.LOCAL_QUEUE, tasks.ENSEMBLE_QUEUE], False, False),
    ],
)
def test_the_heartbeat_says_whether_the_worker_takes_ensembles(worker, monkeypatch, queues, enabled, published):
    settings, _ = worker
    settings.worker.ensemble = enabled
    beats = []
    middleware = tasks.LocalWorkerHeartbeat()

    def publish(redis, capacity, average, **options):
        beats.append(options)
        middleware._stop.set()

    monkeypatch.setattr(tasks.routing, "publish_heartbeat", publish)
    monkeypatch.setattr(tasks.threading, "Thread", lambda target, **options: types.SimpleNamespace(start=target))
    monkeypatch.setattr("app.worker.load.LoadMonitor.start", lambda self: None)
    monkeypatch.setattr("app.worker.load.sample_gpus", lambda: None)

    middleware.after_worker_boot(tasks.broker, dramatiq_worker(queues))

    assert [{name: beat[name] for name in ("ensemble", "transcription")} for beat in beats] == [
        {"ensemble": published, "transcription": queues is None or tasks.LOCAL_QUEUE in queues}]


def test_a_worker_kept_to_ensembles_beats_without_transcriptions(worker, monkeypatch):
    settings, _ = worker
    settings.worker.ensemble = True
    beats = []
    middleware = tasks.LocalWorkerHeartbeat()

    def publish(redis, capacity, average, **options):
        beats.append(options)
        middleware._stop.set()

    monkeypatch.setattr(tasks.routing, "publish_heartbeat", publish)
    monkeypatch.setattr(tasks.threading, "Thread", lambda target, **options: types.SimpleNamespace(start=target))
    monkeypatch.setattr("app.worker.load.LoadMonitor.start", lambda self: None)
    monkeypatch.setattr("app.worker.load.sample_gpus", lambda: None)

    middleware.after_worker_boot(tasks.broker, dramatiq_worker([tasks.ENSEMBLE_QUEUE]))

    assert [{name: beat[name] for name in ("ensemble", "transcription", "node", "running")} for beat in beats] == [
        {"ensemble": True, "transcription": False, "node": "hub", "running": 0}]


# --- Routing ---------------------------------------------------------------------

def test_the_heartbeat_field_round_trips(api_context):
    routing.publish_heartbeat(tasks.redis, 2, 60.0, "a", ensemble=True)
    routing.publish_heartbeat(tasks.redis, 3, 90.0, "b")

    assert tasks.redis.hget(routing.heartbeat_key("a"), "arrangement") == "1"
    assert tasks.redis.hget(routing.heartbeat_key("b"), "arrangement") == "0"
    assert [(worker["id"], worker["ensemble"]) for worker in routing.local_workers(tasks.redis)] == [("a", True), ("b", False)]
    assert routing.ensemble_workers(tasks.redis) == (2, 1)


def test_a_heartbeat_from_before_ensembles_does_not_take_them(api_context):
    routing.publish_heartbeat(tasks.redis, 2, 60.0, "old")
    tasks.redis.hdel(routing.heartbeat_key("old"), "arrangement")

    assert routing.local_workers(tasks.redis)[0]["ensemble"] is False


def test_local_capacity_counts_only_workers_that_take_ensembles_when_asked(api_context):
    routing.publish_heartbeat(tasks.redis, 2, 100.0, "a", ensemble=True)
    routing.publish_heartbeat(tasks.redis, 6, 40.0, "b", ensemble=False)

    assert routing.local_capacity(tasks.redis) == (8, pytest.approx(55.0))
    assert routing.local_capacity(tasks.redis, ensemble=True) == (2, 100.0)
    assert routing.estimated_local_wait(tasks.redis) == 0
    # Busy for the ensemble worker's capacity, though the other has room.
    for index in range(2):
        tasks.redis.sadd(routing.ACTIVE_KEYS["local"], f"busy-{index}")
    assert routing.estimated_local_wait(tasks.redis) == 0
    assert routing.estimated_local_wait(tasks.redis, ensemble=True) == 100.0


def test_no_ensemble_worker_means_no_capacity_for_ensembles(api_context):
    routing.publish_heartbeat(tasks.redis, 4, 60.0, "a")

    assert routing.local_capacity(tasks.redis, ensemble=True)[0] == 0
    assert routing.estimated_local_wait(tasks.redis, ensemble=True) == float("inf")


def test_choose_backend_sends_an_ensemble_remote_when_no_live_worker_takes_ensembles(api_context):
    _, settings, _ = api_context
    settings.outsource.provider = "runpod"
    settings.outsource.max_concurrency = 4
    routing.publish_heartbeat(tasks.redis, 4, 60.0, "plain")

    assert routing.choose_backend(tasks.redis, "ensembled", ensemble=True) == "remote"
    assert routing.choose_backend(tasks.redis, "plain-job") == "local"
    assert tasks.redis.smembers(routing.ACTIVE_KEYS["remote"]) == {"ensembled"}
    assert tasks.redis.smembers(routing.ACTIVE_KEYS["local"]) == {"plain-job"}


def test_choose_backend_keeps_an_ensemble_local_when_a_worker_takes_ensembles(api_context):
    _, settings, _ = api_context
    settings.outsource.provider = "runpod"
    routing.publish_heartbeat(tasks.redis, 4, 60.0, "ensembling", ensemble=True)

    assert routing.choose_backend(tasks.redis, "job", ensemble=True) == "local"


def test_choose_backend_keeps_an_ensemble_local_without_outsourcing(api_context):
    routing.publish_heartbeat(tasks.redis, 4, 60.0, "plain")

    assert routing.choose_backend(tasks.redis, "job", ensemble=True) == "local"


def test_an_ensemble_waits_locally_when_the_remote_slots_are_full(api_context):
    _, settings, _ = api_context
    settings.outsource.provider = "runpod"
    settings.outsource.max_concurrency = 1
    routing.publish_heartbeat(tasks.redis, 4, 60.0, "plain")
    tasks.redis.sadd(routing.ACTIVE_KEYS["remote"], "other")

    assert routing.choose_backend(tasks.redis, "job", ensemble=True) == "local"


# --- The remote request ----------------------------------------------------------

def test_the_remote_request_has_mode_and_source_rate_only_for_ensembles(api_context, monkeypatch, tmp_path):
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
        return DownloadedAudio(path=wav, metadata={}, source_rate=48000)

    monkeypatch.setattr(remote, "fetch_audio", fetch)
    monkeypatch.setattr(remote, "convert_to_flac", lambda wav, output_dir, cancellation_callback=None: wav)
    monkeypatch.setattr(remote.time, "sleep", lambda seconds: None)
    provider = Provider()
    tasks.redis.hset(tasks.job_key("job"), mapping={"status": "processing", "progress": "0.0"})

    for ensemble in (False, True):
        context = tasks.JobContext("job", tmp_path / str(ensemble), ensemble=ensemble)
        remote.transcribe_remotely(
            context, source_url=URL, provider=provider, storage=Storage(), ensemble=context.ensemble
        )

    assert provider.submitted == [
        {"audio_url": "https://storage.example/audio", "job_id": "job"},
        {"audio_url": "https://storage.example/audio", "job_id": "job", "mode": "ensemble", "source_rate": 48000},
    ]


@pytest.mark.parametrize(
    ("job_input", "expected"),
    [
        ({}, (False, None)),
        ({"mode": "transcription"}, (False, None)),
        ({"mode": "transcription", "source_rate": 44100}, (False, None)),
        ({"mode": "ensemble"}, (True, None)),
        ({"mode": "ensemble", "source_rate": None}, (True, None)),
        ({"mode": "ensemble", "source_rate": 48000}, (True, 48000)),
        ({"mode": "ensemble", "source_rate": 1_000_000}, (True, 1_000_000)),
        # Servers from before ensembles sent "arrangement" and a style.
        ({"mode": "arrangement"}, (True, None)),
        ({"mode": "arrangement", "style": "level1"}, (True, None)),
        ({"mode": "arrangement", "style": "dense"}, (True, None)),
        ({"mode": "arrangement", "style": "level3", "source_rate": 22050}, (True, 22050)),
    ],
)
def test_the_handler_reads_the_requested_ensemble(job_input, expected):
    assert requested_ensemble(job_input) == expected


@pytest.mark.parametrize(
    "job_input",
    [
        {"mode": "remix"},
        {"mode": None},
        {"mode": "piano"},
        {"mode": ["ensemble"]},
        {"mode": "ensemble", "source_rate": 0},
        {"mode": "ensemble", "source_rate": -1},
        {"mode": "ensemble", "source_rate": 1_000_001},
        {"mode": "ensemble", "source_rate": True},
        {"mode": "ensemble", "source_rate": "48000"},
        {"mode": "ensemble", "source_rate": 44100.0},
    ],
)
def test_the_handler_refuses_other_values(job_input):
    with pytest.raises(InvalidInput):
        requested_ensemble(job_input)


def test_the_handler_names_what_it_refuses():
    with pytest.raises(InvalidInput, match="mode must be transcription or ensemble"):
        requested_ensemble({"mode": "remix"})
    with pytest.raises(InvalidInput, match="source_rate must be a sample rate in Hz"):
        requested_ensemble({"mode": "ensemble", "source_rate": 0})


def test_a_local_jobs_time_limit_follows_its_length():
    """A local job's Dramatiq time limit is a base plus a share of its audio's length, so a long job on a slow device
    (an ensemble job on a CPU runs at about 1.2 times its length) is never ended by Dramatiq's 10-minute default."""
    base = tasks.LOCAL_TIME_LIMIT_BASE * 1000
    assert tasks.local_time_limit_ms({}) == base
    assert tasks.local_time_limit_ms({"duration": "bad"}) == base
    an_hour = tasks.local_time_limit_ms({"duration": "3600"})
    assert an_hour == base + int(tasks.LOCAL_TIME_LIMIT_PER_SECOND * 3600 * 1000)
    # Ten hours on a CPU (about 12 hours of work) still fit.
    assert tasks.local_time_limit_ms({"duration": str(10 * 3600)}) > 1.2 * 10 * 3600 * 1000


def test_restarting_the_time_limit_uses_the_jobs_limit(monkeypatch):
    from dramatiq.middleware import TimeLimit

    class Manager:
        def __init__(self):
            self.timeouts = []

        def add_timeout(self, thread, limit):
            self.timeouts.append(limit)

    middleware = TimeLimit()
    middleware.manager = Manager()
    monkeypatch.setattr(tasks.broker, "middleware", [middleware])
    tasks.restart_time_limit({"duration": "600"})
    tasks.restart_time_limit()
    assert middleware.manager.timeouts == [tasks.local_time_limit_ms({"duration": "600"}), middleware.time_limit]
