"""A home cluster (app/cluster.py): what nodes and the hub share in Redis,
attempts that cannot overwrite each other, jobs that move between devices,
billing that only the hub writes, and rate limits per public address."""

import json
import re
import signal
import sys
import time
import types
from pathlib import Path

import fakeredis
import pytest

from app import cluster, video_check, video_sites
from app.errors import SourceTemporarilyUnavailable, SourceVideoUnavailable
from app.transcription.cancellation import TranscriptionCancelled
from app.worker import launcher, supervisor, tasks
from app.worker.places import PLACES_VARIABLE


@pytest.fixture(autouse=True)
def fresh_caches():
    cluster.forget_egress_cache()
    yield
    cluster.forget_egress_cache()


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


def job(job_id="job"):
    return tasks.redis.hgetall(tasks.job_key(job_id))


def install_pipeline(monkeypatch, transcribe_source):
    module = types.ModuleType("app.transcription.pipeline")
    module.transcribe_source = transcribe_source
    monkeypatch.setitem(sys.modules, "app.transcription.pipeline", module)


def lose_worker(job_id="job"):
    """The hub sees the lease expire, as when a device lost its network,
    queues the job again, and another device claims it."""
    tasks.redis.delete(tasks.lease_key(job_id))
    assert tasks.fail_abandoned_job(job_id, tasks.WORKER_STOPPED)
    assert tasks.claim_job(job_id, "other-device")


def with_nodes(redis, *names):
    for name in names:
        cluster.register(redis, name, cluster.TASKS)
    cluster.forget_egress_cache()


def publish(redis, node, running, egress, service="worker"):
    cluster.publish_status(redis, node, service, {"running": running, "egress": egress, "state": "running"})


# --- An attempt that lost its job writes nothing ------------------------------------------

def test_a_worker_cut_off_while_its_job_moved_writes_no_result_and_settles_nothing(worker, monkeypatch):
    settings, billing = worker
    monkeypatch.setattr(tasks, "send_job", lambda *arguments, **options: None)
    queue_job(billing, source_url="https://example.com/v")

    def transcribe_source(output_dir, **kwargs):
        lose_worker()
        midi = output_dir / "transcription.mid"
        midi.write_bytes(b"MThd")
        return midi

    install_pipeline(monkeypatch, transcribe_source)
    tasks.transcribe_job.fn("job", "https://example.com/v")

    # The other device's attempt owns the job: still processing, no result.
    assert job()["status"] == "processing"
    assert job()["attempt_token"] == "other-device"
    assert "result" not in job()
    assert billing.reserved_job_ids() == ["job"]
    assert tasks.redis.sismember(tasks.routing.ACTIVE_KEYS["local"], "job")


def test_a_worker_cut_off_does_not_fail_the_job_its_successor_runs(worker, monkeypatch):
    _, billing = worker
    monkeypatch.setattr(tasks, "send_job", lambda *arguments, **options: None)
    queue_job(billing, source_url="https://example.com/v")
    job_files = Path(tasks.settings.service.data_dir) / "jobs" / "job"

    def transcribe_source(output_dir, **kwargs):
        lose_worker()
        job_files.mkdir(parents=True, exist_ok=True)
        (job_files / "upload.input").write_bytes(b"kept")
        raise RuntimeError("the connection broke")

    install_pipeline(monkeypatch, transcribe_source)
    tasks.transcribe_job.fn("job", "https://example.com/v")

    assert job()["status"] == "processing"
    assert billing.reserved_job_ids() == ["job"]
    assert (job_files / "upload.input").exists()


def test_a_worker_cut_off_treats_the_takeover_as_no_cancellation(worker, monkeypatch):
    _, billing = worker
    monkeypatch.setattr(tasks, "send_job", lambda *arguments, **options: None)
    queue_job(billing, source_url="https://example.com/v")

    def transcribe_source(output_dir, cancellation_callback, **kwargs):
        lose_worker()
        assert cancellation_callback()
        raise TranscriptionCancelled()

    install_pipeline(monkeypatch, transcribe_source)
    tasks.transcribe_job.fn("job", "https://example.com/v")

    # Not settled at the cancellation price, not marked cancelled.
    assert job()["status"] == "processing"
    assert billing.reserved_job_ids() == ["job"]
    assert billing.get_balance("user") == 6


def test_complete_job_refuses_an_attempt_that_no_longer_owns_the_job(worker, tmp_path):
    _, billing = worker
    queue_job(billing)
    assert tasks.claim_job("job", "second")
    midi = tmp_path / "transcription.mid"
    midi.write_bytes(b"MThd")

    assert not tasks.complete_job("job", midi, token="first")
    assert job()["status"] == "processing"
    assert tasks.complete_job("job", midi, token="second")


def test_the_lease_of_a_superseded_attempt_stops_renewing(worker):
    _, billing = worker
    queue_job(billing)
    lease = tasks.JobLease("job")
    assert tasks.claim_job("job", lease.token)
    tasks.redis.hset(tasks.job_key("job"), "attempt_token", tasks.ABANDONED_TOKEN)
    tasks.redis.delete(tasks.lease_key("job"))

    lease._update(renew=True)

    assert lease._stop.is_set()
    assert not tasks.redis.exists(tasks.lease_key("job"))


def test_a_job_lost_on_every_attempt_fails_once_and_is_refunded_once(worker, monkeypatch):
    settings, billing = worker
    sent = []
    monkeypatch.setattr(tasks, "send_job", lambda job_id, backend, *arguments, **options: sent.append(job_id))
    queue_job(billing, source_url="https://example.com/v")
    for attempt in range(settings.jobs.max_attempts):
        assert tasks.claim_job("job", f"attempt-{attempt}")
        tasks.redis.delete(tasks.lease_key("job"))
        assert tasks.fail_abandoned_jobs() == 1

    assert job()["status"] == "failed"
    assert job()["error_code"] == tasks.INTERRUPTED
    assert len(sent) == settings.jobs.max_attempts - 1
    assert billing.reserved_job_ids() == []
    assert billing.get_balance("user") == 10
    # Nothing is left to settle twice.
    tasks.reconcile_billing()
    assert billing.get_balance("user") == 10


@pytest.mark.parametrize(
    ("fields", "actor", "queue"),
    [
        ({}, "transcribe_job", tasks.LOCAL_QUEUE),
        ({"mode": "arrangement", "mode_name": "ensemble"}, "arrange_job", "arrangement"),
        # A job queued before ensemble jobs replaced piano covers kept its style.
        ({"mode": "arrangement", "style": "level2"}, "arrange_job", "arrangement"),
    ],
)
def test_a_job_whose_node_dropped_is_queued_again_locally_on_its_own_queue(worker, monkeypatch, fields, actor, queue):
    # The real send_job and the actors' own messages: the lost job's message
    # must reach the queue of its kind, with the stored mode kept.
    _, billing = worker
    messages = []
    for actor_name in ("transcribe_job", "ensemble_job", "transcribe_remote_job"):
        actor_object = getattr(tasks, actor_name)
        # The message Dramatiq would enqueue, with its queue and actor names.
        monkeypatch.setattr(actor_object, "send",
                            lambda *args, _actor=actor_object, **kwargs: messages.append(_actor.message(*args, **kwargs)))
    queue_job(billing, source_url="https://example.com/v", backend="local", node="laptop", **fields)
    assert tasks.claim_job("job", "attempt-1")
    # The node's worker stopped, or its network dropped: the lease lapses.
    tasks.redis.delete(tasks.lease_key("job"))

    assert tasks.fail_abandoned_jobs() == 1

    assert job()["status"] == "queued" and job()["requeue_reason"] == tasks.WORKER_STOPPED
    assert [(message.actor_name, message.queue_name, message.args) for message in messages] == [
        (actor, queue, ("job", "https://example.com/v"))]
    assert {name: job().get(name) for name in fields} == fields
    assert tasks.redis.sismember(tasks.routing.ACTIVE_KEYS["local"], "job")
    assert billing.reserved_job_ids() == ["job"]


def test_one_attempt_fails_at_the_first_interruption(worker, monkeypatch):
    settings, billing = worker
    settings.jobs.max_attempts = 1
    monkeypatch.setattr(tasks, "send_job", lambda *arguments, **options: pytest.fail("queued again"))
    queue_job(billing, source_url="https://example.com/v")
    assert tasks.claim_job("job")
    tasks.redis.delete(tasks.lease_key("job"))

    assert tasks.fail_abandoned_jobs() == 1
    assert job()["status"] == "failed"


def test_the_devices_that_ran_a_job_are_kept(worker, monkeypatch, default_settings):
    _, billing = worker
    queue_job(billing)
    default_settings.node.name = "laptop"
    assert tasks.claim_job("job", "a")
    tasks.redis.hset(tasks.job_key("job"), "status", "queued")
    default_settings.node.name = "desktop"
    assert tasks.claim_job("job", "b")

    assert job()["nodes"] == "laptop,desktop"
    assert job()["node"] == "desktop"
    assert job()["attempt"] == "2"


def test_a_job_queued_long_gets_its_message_again_once_a_while(worker, monkeypatch):
    _, billing = worker
    sent = []
    monkeypatch.setattr(tasks, "send_job", lambda job_id, backend, *arguments, **options: sent.append(job_id))
    queue_job(billing, "old", queued_at="1000", source_url="https://example.com/a")
    queue_job(billing, "new", queued_at="1550", source_url="https://example.com/b")
    tasks.redis.hset(tasks.job_key("running"), mapping={"status": "processing", "queued_at": "0"})

    assert tasks.resend_stale_queued(now=1000 + tasks.STALE_QUEUED + 1) == 1
    assert sent == ["old"]
    # Not again until another while has passed.
    assert tasks.resend_stale_queued(now=1000 + tasks.STALE_QUEUED + 60) == 0
    assert tasks.resend_stale_queued(now=1000 + 2 * tasks.STALE_QUEUED + 2) == 2


# --- Billing that only the hub writes ------------------------------------------------------------

def test_a_node_only_names_the_jobs_it_ended():
    redis = fakeredis.FakeRedis(decode_responses=True)
    hub_billing = cluster.HubBilling(redis)

    hub_billing.settle_success("a")
    hub_billing.settle_cancellation("b", 99999)
    hub_billing.release("c")
    hub_billing.record_job("a", {"status": "completed"})

    assert sorted(redis.zrange(cluster.SETTLE_KEY, 0, -1)) == ["a", "b", "c"]
    assert redis.zrange(cluster.RECORD_KEY, 0, -1) == ["a"]


def test_the_hub_settles_node_jobs_by_their_state_not_by_what_the_node_said(worker, monkeypatch):
    _, billing = worker
    monkeypatch.setattr(tasks, "Billing", lambda: billing)
    node = cluster.HubBilling(tasks.redis)
    queue_job(billing, "done", cost=3)
    queue_job(billing, "broken", cost=2)
    queue_job(billing, "cancelled", cost=4)
    queue_job(billing, "running", cost=1)
    tasks.redis.hset(tasks.job_key("done"), "status", "completed")
    tasks.redis.hset(tasks.job_key("broken"), "status", "failed")
    tasks.redis.hset(tasks.job_key("cancelled"), mapping={"status": "cancelled", "progress": "0.5"})
    tasks.redis.hset(tasks.job_key("running"), "status", "processing")
    # A node that claims a huge cancellation price is ignored: the hub
    # works the price out from the job.
    node.settle_cancellation("cancelled", 1000)
    for job_id in ("done", "broken", "running"):
        node.settle_success(job_id)

    tasks.settle_node_jobs()

    assert billing.reserved_job_ids() == ["running"]
    assert billing.get_balance("user") == 10 - 3 - tasks.cancelled_cost(4, 0.5) - 1
    # The running job is looked at again later, not dropped.
    assert tasks.redis.zscore(cluster.SETTLE_KEY, "running") is not None

    tasks.redis.hset(tasks.job_key("running"), "status", "completed")
    cluster.process_node_requests(tasks.redis, lambda job_id, stored: tasks.settle_ended_job(job_id, stored, billing),
                                  lambda job_id: None, now=10**10)
    assert billing.reserved_job_ids() == []
    assert tasks.redis.zcard(cluster.SETTLE_KEY) == 0


def test_the_hub_releases_a_job_a_node_named_that_no_longer_exists(worker, monkeypatch):
    _, billing = worker
    monkeypatch.setattr(tasks, "Billing", lambda: billing)
    billing.reserve("user", "gone", 3)
    cluster.HubBilling(tasks.redis).release("gone")

    tasks.settle_node_jobs()

    assert billing.reserved_job_ids() == []
    assert billing.get_balance("user") == 10


def test_the_hub_records_node_jobs(worker, monkeypatch):
    _, billing = worker
    monkeypatch.setattr(tasks, "Billing", lambda: billing)
    recorded = []
    monkeypatch.setattr(billing, "record_job", lambda job_id, stored: recorded.append((job_id, stored["status"])))
    queue_job(billing)
    cluster.HubBilling(tasks.redis).record_job("job", {})

    tasks.settle_node_jobs()

    assert recorded == [("job", "queued")]
    assert tasks.redis.zcard(cluster.RECORD_KEY) == 0


def test_a_node_worker_never_builds_a_billing_database(monkeypatch):
    monkeypatch.setattr(cluster.settings.service, "deployment", "node")
    assert cluster.is_node()
    assert isinstance(cluster.HubBilling(fakeredis.FakeRedis()), cluster.HubBilling)


# --- Jobs that move to another device ------------------------------------------------------------

def test_a_device_whose_address_a_site_refuses_leaves_the_job_to_another(worker, monkeypatch, default_settings):
    _, billing = worker
    sent = []
    monkeypatch.setattr(tasks, "send_job", lambda job_id, backend, *arguments, **options: sent.append(options))
    with_nodes(tasks.redis, "laptop")
    default_settings.node.egress = "home"
    queue_job(billing, source="url", source_url="https://www.youtube.com/watch?v=1")
    video_sites.open_breaker("youtube.com", video_sites.LIMITED, "rate limited")
    publish(tasks.redis, "laptop", ["transcribe"], cluster.egress_label("label:office"))

    assert tasks.pass_job("job", "local")

    assert sent == [{"delay": tasks.PASS_DELAY_MS}]
    assert job()["passes"] == "1"
    assert job()["status"] == "queued"


def test_a_device_runs_the_job_itself_when_no_other_address_is_free(worker, monkeypatch, default_settings):
    _, billing = worker
    monkeypatch.setattr(tasks, "send_job", lambda *arguments, **options: pytest.fail("passed"))
    with_nodes(tasks.redis, "laptop")
    default_settings.node.egress = "home"
    queue_job(billing, source="url", source_url="https://www.youtube.com/watch?v=1")
    video_sites.open_breaker("youtube.com", video_sites.LIMITED, "rate limited")
    # The other device is behind the same address, which the site refuses too.
    publish(tasks.redis, "laptop", ["transcribe"], cluster.egress(tasks.redis))

    assert not tasks.pass_job("job", "local")


def test_a_job_is_handed_on_at_most_max_passes_times(worker, monkeypatch, default_settings):
    _, billing = worker
    monkeypatch.setattr(tasks, "send_job", lambda *arguments, **options: pytest.fail("passed"))
    with_nodes(tasks.redis, "laptop")
    default_settings.node.egress = "home"
    queue_job(billing, source="url", source_url="https://www.youtube.com/watch?v=1", passes=str(tasks.MAX_PASSES))
    video_sites.open_breaker("youtube.com", video_sites.LIMITED, "rate limited")
    publish(tasks.redis, "laptop", ["transcribe"], cluster.egress_label("label:office"))

    assert not tasks.pass_job("job", "local")


def test_without_nodes_nothing_is_handed_on(worker, monkeypatch):
    _, billing = worker
    monkeypatch.setattr(tasks, "send_job", lambda *arguments, **options: pytest.fail("passed"))
    queue_job(billing, source="url", source_url="https://www.youtube.com/watch?v=1", interruptions="1", nodes="hub")
    video_sites.open_breaker("youtube.com", video_sites.LIMITED, "rate limited")

    assert not tasks.pass_job("job", "local")


def test_a_job_interrupted_here_prefers_another_device(worker, monkeypatch, default_settings):
    _, billing = worker
    sent = []
    monkeypatch.setattr(tasks, "send_job", lambda job_id, backend, *arguments, **options: sent.append(job_id))
    with_nodes(tasks.redis, "laptop")
    queue_job(billing, source="upload", interruptions="1", nodes="hub")
    publish(tasks.redis, "laptop", ["transcribe"], "")

    assert tasks.pass_job("job", "local")
    assert sent == ["job"]

    # An ensemble job needs a device that takes ensemble jobs.
    tasks.redis.hset(tasks.job_key("job"), "mode", tasks.ENSEMBLE)
    assert not tasks.pass_job("job", "local")


def test_a_rate_limited_job_moves_to_a_device_with_another_address(worker, monkeypatch, default_settings):
    _, billing = worker
    sent = []
    monkeypatch.setattr(tasks, "send_job", lambda job_id, backend, *arguments, **options: sent.append(backend))
    with_nodes(tasks.redis, "laptop")
    default_settings.node.egress = "home"
    queue_job(billing, source="url", source_url="https://www.youtube.com/watch?v=1")
    publish(tasks.redis, "laptop", ["transcribe"], cluster.egress_label("label:office"))

    def transcribe_source(**kwargs):
        raise video_sites.failure("youtube.com", __import__("subprocess").CalledProcessError(
            1, ["yt-dlp"], stderr="ERROR: HTTP Error 429: Too Many Requests"))

    install_pipeline(monkeypatch, transcribe_source)
    tasks.transcribe_job.fn("job", "https://www.youtube.com/watch?v=1")

    assert job()["status"] == "queued"
    assert job()["address_moves"] == "1"
    assert "outsourced" not in job()
    assert sent == ["local"]
    assert billing.reserved_job_ids() == ["job"]


def test_a_rate_limited_job_fails_when_every_address_is_refused(worker, monkeypatch, default_settings):
    _, billing = worker
    monkeypatch.setattr(tasks, "send_job", lambda *arguments, **options: pytest.fail("moved"))
    with_nodes(tasks.redis, "laptop")
    default_settings.node.egress = "home"
    queue_job(billing, source="url", source_url="https://www.youtube.com/watch?v=1")
    office = cluster.egress_label("label:office")
    publish(tasks.redis, "laptop", ["transcribe"], office)
    video_sites.redis.set(video_sites._key("youtube.com", video_sites.BLOCKED, office),
                          json.dumps({"kind": video_sites.LIMITED, "reason": "429"}), ex=600)

    def transcribe_source(**kwargs):
        raise SourceTemporarilyUnavailable("Links to youtube.com cannot be read", 60)

    install_pipeline(monkeypatch, transcribe_source)
    tasks.transcribe_job.fn("job", "https://www.youtube.com/watch?v=1")

    assert job()["status"] == "failed"
    assert billing.reserved_job_ids() == []


# --- Rate limits per public address --------------------------------------------------------------

def test_without_nodes_the_keys_are_those_of_one_machine(default_settings):
    redis = video_sites.redis
    video_sites.open_breaker("youtube.com", video_sites.LIMITED, "429")

    assert redis.exists("video_sites:youtube.com:blocked")
    assert [state["address"] for state in video_sites.states()] == [None]


def test_each_address_has_its_own_breaker(default_settings):
    redis = video_sites.redis
    with_nodes(redis, "laptop")
    default_settings.node.egress = "home"
    home = cluster.egress(redis)
    office = cluster.egress_label("label:office")

    video_sites.open_breaker("youtube.com", video_sites.LIMITED, "429")

    assert video_sites.refusal("youtube.com") is not None
    assert video_sites.refusal("youtube.com", home) is not None
    assert video_sites.refusal("youtube.com", office) is None
    assert {(state["site"], state["address"]) for state in video_sites.states()} >= {("youtube.com", home)}


def test_reset_closes_a_site_for_every_address(default_settings):
    redis = video_sites.redis
    with_nodes(redis, "laptop")
    office = cluster.egress_label("label:office")
    redis.set(video_sites._key("youtube.com", video_sites.BLOCKED, office), json.dumps({"kind": "limited", "reason": "x"}))
    redis.set(video_sites._key("youtube.com", video_sites.BLOCKED, ""), json.dumps({"kind": "limited", "reason": "x"}))
    redis.set(video_sites._key("vimeo.com", video_sites.BLOCKED, office), json.dumps({"kind": "limited", "reason": "x"}))

    assert video_sites.reset("youtube.com") == ["youtube.com"]

    assert video_sites.refusal("youtube.com", office) is None
    assert video_sites.refusal("youtube.com", "") is None
    assert video_sites.refusal("vimeo.com", office) is not None


def test_the_public_address_is_learned_once_and_shared(default_settings, monkeypatch):
    redis = fakeredis.FakeRedis(decode_responses=True)
    with_nodes(redis, "laptop")
    calls = []
    monkeypatch.setattr(cluster, "detect_public_address", lambda: calls.append(1) or "203.0.113.7")

    first = cluster.egress(redis)
    cluster.forget_egress_cache()
    second = cluster.egress(redis)

    assert first == second == cluster.egress_label("203.0.113.7")
    assert calls == [1]
    assert redis.get(cluster.egress_key("hub")) == "203.0.113.7"
    # The label does not show the address in key names.
    assert "203.0.113.7" not in first


def test_an_unknown_address_counts_as_the_devices_own(default_settings, monkeypatch):
    redis = fakeredis.FakeRedis(decode_responses=True)
    with_nodes(redis, "laptop")
    monkeypatch.setattr(cluster, "detect_public_address", lambda: None)

    assert cluster.egress(redis) == cluster.egress_label("node:hub")


def test_the_trace_page_answer_is_read_for_its_address_alone(monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *arguments):
            return False

        def read(self, size):
            return b"fl=1\nh=www.cloudflare.com\nip=198.51.100.4\nts=1\n"

    monkeypatch.setattr(cluster.urllib.request, "urlopen", lambda url, timeout: Response())
    assert cluster.detect_public_address() == "198.51.100.4"


# --- Link checks by a node ----------------------------------------------------------------------

def test_a_nodes_answer_keeps_only_the_length_and_metadata():
    info = video_check.parse_answer(json.dumps({"info": {
        "duration": 95.5, "title": "x" * 5000, "uploader": "Chopin", "view_count": 7,
        "formats": [{"url": "http://169.254.169.254/latest"}], "webpage_url": "https://youtu.be/1",
        "like_count": True,
    }}))

    assert info["duration"] == 95.5
    assert len(info["title"]) == video_check.TEXT_LIMIT
    assert "formats" not in info and "like_count" not in info
    assert info["uploader"] == "Chopin"


@pytest.mark.parametrize("answer", [
    "not json", "[]", json.dumps({"info": {"duration": 0}}), json.dumps({"info": {"duration": "90"}}),
    json.dumps({"info": {"duration": float("inf")}}.__repr__()), json.dumps({}),
])
def test_a_broken_answer_is_no_info(answer):
    with pytest.raises(video_check.SourceError):
        video_check.parse_answer(answer)


def test_a_nodes_refusal_becomes_the_same_error():
    with pytest.raises(SourceVideoUnavailable) as error:
        video_check.parse_answer(json.dumps({"error": {"kind": "SourceVideoUnavailable", "message": "Private video",
                                                      "retry_after": None}}))
    assert str(error.value) == "Private video"
    with pytest.raises(SourceTemporarilyUnavailable) as error:
        video_check.parse_answer(json.dumps({"error": {"kind": "SourceTemporarilyUnavailable", "message": "later",
                                                      "retry_after": 10**9}}))
    assert error.value.retry_after is None
    # An unknown kind is a plain failure, not some other class.
    with pytest.raises(video_check.SourceError) as error:
        video_check.parse_answer(json.dumps({"error": {"kind": "SystemExit", "message": "x"}}))
    assert type(error.value) is video_check.SourceError


def test_the_api_refuses_a_link_only_when_no_node_can_check_it(default_settings):
    redis = video_sites.redis
    with_nodes(redis, "laptop")
    default_settings.node.egress = "home"
    video_sites.open_breaker("youtube.com", video_sites.LIMITED, "429")

    assert video_check.refusal_everywhere(redis, "youtube.com") is not None

    publish(redis, "laptop", ["check", "transcribe"], cluster.egress_label("label:office"))
    assert video_check.refusal_everywhere(redis, "youtube.com") is None
    assert video_check.should_delegate(redis, "https://www.youtube.com/watch?v=1")


def test_a_delegated_check_is_answered_through_redis(default_settings, monkeypatch):
    redis = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(video_check, "read_video_info",
                        lambda client, url, take_slot=True: {"duration": 61, "title": "Etude", "formats": ["x"]})

    def send(request, url):
        assert url == "https://youtu.be/1"
        video_check.answer(redis, request, url)

    info = video_check.delegate(redis, "https://youtu.be/1", send)

    assert info == {"duration": 61.0, "title": "Etude"}
    assert not list(redis.scan_iter(match=cluster.CHECK_REPLY_PREFIX + "*"))


def test_a_link_checked_by_a_node_saves_no_info_for_the_download(api_context, monkeypatch):
    client, settings, _ = api_context
    from app.api.routes import billing as billing_routes
    from app.api.routes import transcriptions as transcription_routes

    monkeypatch.setattr(video_check, "should_delegate", lambda redis, url: True)
    monkeypatch.setattr(video_check, "delegate", lambda redis, url, send: {"duration": 60.0, "title": "Etude"})
    monkeypatch.setattr(transcription_routes, "check_video", billing_routes.check_video)

    response = client.post("/api/transcriptions", json={"source_url": "https://www.youtube.com/watch?v=abc"})

    assert response.status_code in (200, 201, 202), response.text
    job_id = response.json()["job_id"]
    assert tasks.redis.hget(tasks.job_key(job_id), "title") == "Etude"
    assert not (Path(settings.service.data_dir) / "jobs" / job_id / "video.info.json").exists()


# --- Settings the hub publishes -----------------------------------------------------------------

def test_a_node_takes_the_hubs_settings_but_keeps_its_own(default_settings):
    environment = {"WORKER_ENSEMBLE": "false", "NODE_NAME": "laptop"}
    hub = {"RESULT_RETENTION": "1d", "WORKER_ENSEMBLE": "true", "MAX_ACTIVE_JOBS": "100"}

    applied = cluster.apply_hub_settings(environment, hub)

    assert applied["RESULT_RETENTION"] == "1d"
    assert applied["WORKER_ENSEMBLE"] == "false"
    assert applied[cluster.SYNCED_NAMES_VARIABLE] == "MAX_ACTIVE_JOBS,RESULT_RETENTION"

    # A setting removed on the hub goes; the node's own stays.
    again = cluster.apply_hub_settings(applied, {"RESULT_RETENTION": "2d"})
    assert "MAX_ACTIVE_JOBS" not in again
    assert again["RESULT_RETENTION"] == "2d"
    assert again["WORKER_ENSEMBLE"] == "false"


def test_wiring_the_database_and_device_settings_are_never_taken_from_the_hub():
    names = cluster.synced_names()

    assert "RESULT_RETENTION" in names and "JOB_STORAGE" in names
    assert "CLUSTER_STORAGE_ENDPOINT" not in names
    for name in ("REDIS_URL", "DATA_DIR", "DEPLOYMENT", "DATABASE_URL", "DATABASE_BACKUPS", "NODE_NAME",
                 "NODE_EGRESS", "WORKER_THREADS", "WORKER_ENFORCE_CPU", "WORKER_MAX_CONCURRENCY", "COMPOSE_PROFILES",
                 # Only the API's: never shown to a node.
                 "ADMIN_WEB_EMAILS", "CLOUDFLARE_ACCESS_AUD", "GOOGLE_OAUTH_CLIENT_IDS", "BILLING_PRODUCTS",
                 "GOOGLE_PLAY_PACKAGE_NAME", "OFFSITE_BACKUP_RECIPIENT", "CLOUDFLARE_TUNNEL_HOSTNAME"):
        assert name not in names, name
    # What a worker needs.
    for name in ("WORKER_ENSEMBLE", "JOB_STORAGE", "STORAGE_BUCKET", "OUTSOURCE_PROVIDER", "ERROR_REPORT_URL",
                 "VIDEO_SITE_INTERVAL", "WORKER_BUSY_UTILIZATION", "JOB_MAX_ATTEMPTS"):
        assert name in names, name
    applied = cluster.apply_hub_settings({}, {"REDIS_URL": "redis://evil:6379/0", "DATA_DIR": "/"})
    assert "REDIS_URL" not in applied and "DATA_DIR" not in applied


def test_the_hub_publishes_no_secret(monkeypatch):
    redis = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setenv("RESULT_RETENTION", "1d")
    monkeypatch.setenv("REDIS_PASSWORD_FILE", "/run/redis/password")
    monkeypatch.setenv("storage_secret_key", "s3cr3t")

    cluster.publish_hub(redis)

    info = cluster.hub_info(redis)
    assert info["protocol"] == cluster.PROTOCOL
    assert info["settings"]["RESULT_RETENTION"] == "1d"
    assert "s3cr3t" not in json.dumps(info)
    assert "REDIS_PASSWORD_FILE" not in info["settings"]


# --- The nodes' Redis rules -------------------------------------------------------------------------

def node_rules() -> list[str]:
    template = Path(__file__).resolve().parent.parent / "redis" / "node.acl"
    [line] = [line for line in template.read_text(encoding="utf-8").splitlines() if line.startswith("user ")]
    return line.split()


def test_every_rule_of_a_node_is_one_well_formed_token():
    # Two rules run together would make a pattern that matches nothing; see
    # tests/test_node_redis_rules.py for the rules on a real Redis.
    tokens = node_rules()
    assert tokens[:5] == ["user", "node-{name}", "on", "#{hash}", "resetchannels"]
    for token in tokens[5:]:
        assert re.fullmatch(r"(%R~|%RW~|~)[a-z_:*{}.-]+|[+-]@?[a-z_|]+", token), token
        assert token.count("~") <= 1, token


def test_a_nodes_rules_name_no_other_node_and_no_control():
    patterns = [token.split("~", 1)[1] for token in node_rules() if "~" in token]
    writable = [token.split("~", 1)[1] for token in node_rules() if token.startswith(("%RW~", "~"))]

    assert not any(pattern.startswith(("controls:", "maintenance:", "admin_web:", "workers:api")) for pattern in patterns)
    assert not any(pattern == "*" or pattern.startswith("cluster:node:*") and "status" not in pattern for pattern in patterns)
    # Its own switches are read-only to it; the hub's publications too.
    assert not any(pattern.endswith(":config") or pattern in ("cluster:hub", "cluster:images", "cluster:nodes")
                   for pattern in writable)
    assert all("{name}" in pattern for pattern in writable if pattern.startswith(("cluster:node:", "cluster:egress:")))


def test_the_launchers_own_variable_is_no_unknown_setting():
    from app import config, error_report

    assert error_report.SERVICE_ENVIRONMENT in config.INTERNAL_VARIABLES
    assert config.unknown_variable_warnings({error_report.SERVICE_ENVIRONMENT: "worker"}) == []


def test_the_code_version_ignores_line_endings(tmp_path, monkeypatch):
    def version(text: bytes) -> str:
        (tmp_path / "app").mkdir(exist_ok=True)
        (tmp_path / "app" / "cluster.py").write_bytes(text)
        monkeypatch.setattr(cluster, "_code_version", None)
        monkeypatch.setattr(cluster, "__file__", str(tmp_path / "app" / "cluster.py"))
        return cluster.code_version()

    assert version(b"a = 1\nb = 2\n") == version(b"a = 1\r\nb = 2\r\n")
    assert version(b"a = 1\n") != version(b"a = 2\n")


# --- Switches and status ---------------------------------------------------------------------------

def test_switches_allow_what_a_device_offers():
    redis = fakeredis.FakeRedis(decode_responses=True)
    offered = ["transcribe", "arrange", "check"]

    assert cluster.allowed_tasks(redis, "hub", offered) == offered
    cluster.register(redis, "laptop", ["transcribe", "dispatch"])
    assert cluster.allowed_tasks(redis, "laptop", offered) == ["transcribe"]
    cluster.set_enabled(redis, "laptop", False)
    assert cluster.allowed_tasks(redis, "laptop", offered) == []
    cluster.set_enabled(redis, "laptop", True)
    cluster.set_tasks(redis, "laptop", ["arrange"])
    assert cluster.allowed_tasks(redis, "laptop", offered) == ["arrange"]


def test_tasks_are_taken_by_their_stored_values_and_their_names_for_people():
    assert cluster.parse_tasks("transcribe, arrange") == ["transcribe", "arrange"]
    assert cluster.parse_tasks("piano,ensemble,check") == ["transcribe", "arrange", "check"]
    assert cluster.parse_tasks("ensemble") == ["arrange"]
    assert cluster.parse_tasks("all") == list(cluster.TASKS)
    assert cluster.TASK_TITLES["transcribe"] == "piano transcriptions"
    assert cluster.TASK_TITLES["arrange"] == "ensemble transcriptions"
    with pytest.raises(ValueError, match="Unknown task"):
        cluster.parse_tasks("remix")


def test_a_stored_task_list_from_a_newer_hub_ignores_unknown_tasks():
    redis = fakeredis.FakeRedis(decode_responses=True)
    redis.hset(cluster.config_key("laptop"), "tasks", "transcribe,teleport")

    assert cluster.node_config(redis, "laptop")["tasks"] == ["transcribe"]


@pytest.mark.parametrize("name", ["", "hub", "Laptop!", "-x", "a" * 32, "a:b", "a*"])
def test_node_names_are_safe_in_redis_rules(name):
    with pytest.raises(ValueError):
        cluster.check_node_name(name)


def test_forgetting_a_node_removes_its_keys():
    redis = fakeredis.FakeRedis(decode_responses=True)
    cluster.register(redis, "laptop", ["transcribe"])
    publish(redis, "laptop", ["transcribe"], "x")
    redis.set(cluster.egress_key("laptop"), "203.0.113.7")

    cluster.forget(redis, "laptop")

    assert cluster.known_nodes(redis) == []
    assert cluster.statuses(redis) == []
    assert not redis.exists(cluster.config_key("laptop"), cluster.egress_key("laptop"))


def test_live_addresses_by_task():
    redis = fakeredis.FakeRedis(decode_responses=True)
    publish(redis, "laptop", ["transcribe", "check"], "a")
    publish(redis, "desktop", ["transcribe"], "b")
    publish(redis, "hub", ["dispatch"], "a", service="remote-worker")

    assert cluster.live_egresses(redis, "transcribe") == {"a": ["laptop"], "b": ["desktop"]}
    assert cluster.live_egresses(redis, "check") == {"a": ["laptop"]}
    assert cluster.running_tasks(redis)["hub"] == {"dispatch"}


def test_a_worker_kept_to_ensembles_does_not_count_for_transcriptions():
    redis = fakeredis.FakeRedis(decode_responses=True)
    tasks.routing.publish_heartbeat(redis, 4, 30.0, worker="a", ensemble=True, transcription=False)
    tasks.routing.publish_heartbeat(redis, 2, 30.0, worker="b", ensemble=False)

    assert tasks.routing.local_capacity(redis)[0] == 2
    assert tasks.routing.local_capacity(redis, ensemble=True)[0] == 4


# --- The supervisor ----------------------------------------------------------------------------------

class FakeProcess:
    def __init__(self, command, env):
        self.command = command
        self.env = env
        self.signals = []
        self.code = None

    def poll(self):
        return self.code

    def send_signal(self, signum):
        self.signals.append(signum)


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def make_supervisor(monkeypatch, role="local", offered=("transcribe", "arrange"), node=False):
    redis = fakeredis.FakeRedis(decode_responses=True)
    started = []

    def popen(command, env):
        process = FakeProcess(command, env)
        started.append(process)
        return process

    plan = launcher.WorkerPlan(processes=1, threads=6, cpu_threads=None, summary="1 GPU(s)", ensemble=True,
                               places=3)
    monkeypatch.setattr(supervisor.Supervisor, "capabilities", lambda self: list(offered))
    monkeypatch.setattr(supervisor.shutil, "which", lambda name: "/usr/bin/dramatiq")
    monkeypatch.setattr(supervisor, "DRAIN_FILE", Path(supervisor.__file__).parent / "no-such-drain-file")
    monkeypatch.setattr(cluster, "egress", lambda client: "")
    clock = Clock()
    instance = supervisor.Supervisor(role, redis, popen=popen, clock=clock)
    instance.local_plan = plan
    instance.remote_plan = types.SimpleNamespace(threads=8, summary="remote")
    return instance, redis, started, clock


def queues(process):
    return process.command[process.command.index("--queues") + 1:]


def test_the_supervisor_runs_what_the_switches_allow(monkeypatch):
    instance, redis, started, _ = make_supervisor(monkeypatch)

    instance.step()

    assert [queues(process) for process in started] == [["default", "arrangement"]]
    assert started[0].env[supervisor.SERVICE_ENVIRONMENT] == "worker"
    # Its places on the models, and no more prefetched jobs than threads.
    assert started[0].env[PLACES_VARIABLE] == "3"
    assert started[0].env["dramatiq_queue_prefetch"] == "6"
    status = cluster.statuses(redis)[0]
    assert status["running"] == ["transcribe", "arrange"]
    assert status["node"] == "hub" and status["service"] == "worker"


def test_switching_a_task_off_lets_the_running_jobs_finish_first(monkeypatch):
    instance, redis, started, _ = make_supervisor(monkeypatch)
    instance.step()
    cluster.set_tasks(redis, "hub", ["transcribe"])

    instance.step()

    # SIGTERM: Dramatiq finishes its jobs; nothing is killed.
    assert started[0].signals == [signal.SIGTERM]
    assert len(started) == 1
    started[0].code = 0
    instance.step()
    assert [queues(process) for process in started[1:]] == [["default"]]


def test_switching_a_device_off_stops_all_its_work(monkeypatch):
    instance, redis, started, _ = make_supervisor(monkeypatch)
    instance.step()
    cluster.set_enabled(redis, "hub", False)

    instance.step()
    started[0].code = 0
    instance.step()

    assert len(started) == 1
    assert cluster.statuses(redis)[0]["state"] == "off"
    assert cluster.statuses(redis)[0]["running"] == []


def test_a_child_that_dies_is_started_again_later(monkeypatch):
    instance, _, started, clock = make_supervisor(monkeypatch)
    instance.step()
    started[0].code = -9

    instance.step()
    assert len(started) == 1
    clock.now += supervisor.RESTART_BACKOFF[0] + 1
    instance.step()

    assert len(started) == 2


def test_while_redis_is_away_the_supervisor_keeps_what_ran(monkeypatch):
    instance, redis, started, _ = make_supervisor(monkeypatch)
    instance.step()
    monkeypatch.setattr(cluster, "allowed_tasks", lambda *arguments: (_ for _ in ()).throw(ConnectionError("down")))

    instance.step()

    assert started[0].signals == []
    assert "cannot reach Redis" in instance.state


def test_a_drained_worker_takes_no_more_jobs(monkeypatch, tmp_path):
    instance, redis, started, _ = make_supervisor(monkeypatch)
    drain = tmp_path / "drain"
    monkeypatch.setattr(supervisor, "DRAIN_FILE", drain)
    instance.step()
    drain.touch()

    instance.step()

    assert started[0].signals == [signal.SIGTERM]
    assert cluster.statuses(redis)[0]["state"] == "drained"


def test_a_drain_that_nothing_ended_lapses_and_the_worker_takes_work_again(monkeypatch, tmp_path):
    # A restart cut off after the drain (Ctrl+C, a lost connection, `up` failing) left the mark behind.
    instance, redis, started, _ = make_supervisor(monkeypatch)
    drain = tmp_path / "drain"
    monkeypatch.setattr(supervisor, "DRAIN_FILE", drain)
    drain.write_text(f"{time.time() + 60:.0f}\n")
    instance.step()
    assert started == [] and cluster.statuses(redis)[0]["state"] == "drained"

    drain.write_text(f"{time.time() - 1:.0f}\n")
    instance.step()

    assert started and cluster.statuses(redis)[0]["state"] == "running"
    assert not drain.exists()


def test_an_empty_drain_mark_from_before_never_lapses(monkeypatch, tmp_path):
    drain = tmp_path / "drain"
    monkeypatch.setattr(supervisor, "DRAIN_FILE", drain)
    drain.touch()

    assert supervisor.draining(time.time() + 10 ** 9)
    assert drain.exists()


def test_the_drain_command_writes_when_it_lapses(monkeypatch, tmp_path):
    drain = tmp_path / "drain"
    monkeypatch.setattr(supervisor, "DRAIN_FILE", drain)
    monkeypatch.setattr(supervisor, "redis_client", lambda: fakeredis.FakeRedis(decode_responses=True))

    # No time to wait: it gives up at once, and the mark stays until it lapses.
    assert supervisor.drain(0, sleep=lambda seconds: None, clock=lambda: 1000.0) == 1

    assert float(drain.read_text()) == 1000 + supervisor.DRAIN_HOLD


def test_a_node_of_another_protocol_waits_for_an_update(monkeypatch):
    instance, redis, started, _ = make_supervisor(monkeypatch, role="node", offered=cluster.TASKS)
    monkeypatch.setattr(cluster.settings.service, "deployment", "node")
    redis.hset(cluster.HUB_KEY, mapping={"protocol": str(cluster.PROTOCOL + 1), "settings": "{}"})

    instance.step()

    assert started == []
    assert "waiting for an update" in instance.state


def test_a_node_starts_again_when_the_hubs_settings_change(monkeypatch):
    instance, redis, started, _ = make_supervisor(monkeypatch, role="node", offered=cluster.TASKS)
    monkeypatch.setattr(cluster.settings.service, "deployment", "node")
    monkeypatch.setenv(cluster.SYNCED_HASH_VARIABLE, cluster.settings_hash({}))
    redis.hset(cluster.HUB_KEY, mapping={"protocol": str(cluster.PROTOCOL), "settings": "{}"})
    instance.step()
    assert started

    redis.hset(cluster.HUB_KEY, "settings", json.dumps({"RESULT_RETENTION": "1d"}))
    instance.step()

    assert instance.restart_for_settings
    assert all(process.signals == [signal.SIGTERM] for process in started)


def test_a_node_runs_checks_and_dispatch_in_children_of_their_own(monkeypatch):
    instance, _, started, _ = make_supervisor(monkeypatch, role="node", offered=cluster.TASKS)

    instance.step()

    by_queue = {tuple(queues(process)): process for process in started}
    assert set(by_queue) == {("default", "arrangement"), ("checks",), ("remote",)}
    assert by_queue[("remote",)].env[supervisor.SERVICE_ENVIRONMENT] == "remote-worker"


def test_a_node_takes_the_hubs_settings_by_starting_again(monkeypatch):
    redis = fakeredis.FakeRedis(decode_responses=True)
    redis.hset(cluster.HUB_KEY, mapping={"protocol": str(cluster.PROTOCOL),
                                         "settings": json.dumps({"RESULT_RETENTION": "1d"})})
    monkeypatch.delenv(cluster.SYNCED_HASH_VARIABLE, raising=False)
    calls = []

    supervisor.take_hub_settings(redis, ["node"], execve=lambda path, args, env: calls.append((args, env)))

    [(args, env)] = calls
    assert args[1:] == ["-m", "app.worker.launcher", "node"]
    assert env["RESULT_RETENTION"] == "1d"
    assert env[cluster.SYNCED_HASH_VARIABLE] == cluster.settings_hash({"RESULT_RETENTION": "1d"})

    # Already running with them: nothing starts again.
    monkeypatch.setenv(cluster.SYNCED_HASH_VARIABLE, cluster.settings_hash({"RESULT_RETENTION": "1d"}))
    supervisor.take_hub_settings(redis, ["node"], execve=lambda *arguments: pytest.fail("started again"))


def test_the_hub_does_not_check_links_for_itself(monkeypatch, default_settings):
    from app.worker import launcher

    monkeypatch.setattr(launcher, "cuda_job_capacities", lambda: None)
    monkeypatch.setattr(supervisor, "ensure_valid", lambda role: None)
    instance = supervisor.Supervisor.__new__(supervisor.Supervisor)
    instance.role, instance.problem, instance.local_plan, instance.remote_plan = "node", "", None, None

    assert "check" not in supervisor.Supervisor.capabilities(instance)
    assert "dispatch" not in supervisor.Supervisor.capabilities(instance)
    assert instance.problem.startswith("outsourcing is off")


def test_a_service_is_not_called_other_code_before_the_hub_published_its_own(api_context):
    from app.admin import nodes

    # As before the hub's API published its own; the test's API may have.
    tasks.redis.delete(cluster.HUB_KEY)
    cluster.publish_status(tasks.redis, "hub", "worker", {"running": ["transcribe"], "code": "abc"})
    assert nodes.overview()["devices"][0]["services"][0]["same_code"]

    tasks.redis.hset(cluster.HUB_KEY, mapping={"protocol": str(cluster.PROTOCOL), "code": "def", "settings": "{}"})
    assert not nodes.overview()["devices"][0]["services"][0]["same_code"]


# --- Cancelling a job the server had to retry -----------------------------------------------

@pytest.mark.parametrize("retried", [{"interruptions": "1"}, {"requeue_reason": "worker stopped"},
                                     {"address_moves": "1"}, {"outsourced": "1"}])
def test_cancelling_a_job_the_server_retried_costs_nothing(worker, retried):
    # It showed 77% on a device that was lost; cancelling while it waits for
    # its next attempt must not charge for that lost progress.
    _, billing = worker
    queue_job(billing, cost=10, **{"progress": "0.77", **retried})

    assert tasks.cancel_job("job") == "queued"

    assert billing.reserved_job_ids() == []
    assert billing.get_balance("user") == 10


def test_cancelling_a_job_that_ran_on_its_first_attempt_costs_its_share(worker):
    _, billing = worker
    queue_job(billing, cost=10, progress="0.5")
    tasks.redis.hset(tasks.job_key("job"), "status", "processing")

    tasks.cancel_job("job")

    assert billing.get_balance("user") == 10 - tasks.cancelled_cost(10, 0.5)


def test_a_retried_job_cancelled_while_running_costs_nothing_also_at_the_worker(worker, monkeypatch):
    _, billing = worker
    monkeypatch.setattr(tasks, "send_job", lambda *arguments, **options: None)
    queue_job(billing, cost=10, source_url="https://example.com/v")
    assert tasks.claim_job("job", "first")
    tasks.update_job("job", progress="0.8")
    tasks.redis.delete(tasks.lease_key("job"))
    assert tasks.fail_abandoned_jobs() == 1

    def transcribe_source(output_dir, cancellation_callback, **kwargs):
        tasks.redis.hset(tasks.job_key("job"), "status", "cancelled")
        assert cancellation_callback()
        raise TranscriptionCancelled()

    install_pipeline(monkeypatch, transcribe_source)
    tasks.transcribe_job.fn("job", "https://example.com/v")

    assert job()["status"] == "cancelled"
    assert billing.reserved_job_ids() == []
    assert billing.get_balance("user") == 10


def test_the_reconciliation_and_the_hub_charge_the_same(worker, monkeypatch):
    _, billing = worker
    monkeypatch.setattr(tasks, "Billing", lambda: billing)
    queue_job(billing, "a", cost=4, progress="0.9", interruptions="1")
    tasks.redis.hset(tasks.job_key("a"), "status", "cancelled")

    cluster.HubBilling(tasks.redis).settle_cancellation("a", 4)
    tasks.settle_node_jobs()

    assert billing.get_balance("user") == 10


def test_devices_behind_one_public_address_are_shown_as_sharing_it(api_context, capsys):
    from app.admin import nodes

    for node in ("hub", "pc", "laptop"):
        if node != "hub":
            cluster.register(tasks.redis, node, ["transcribe"])
    tasks.redis.set(cluster.egress_key("hub"), "203.0.113.7")
    tasks.redis.set(cluster.egress_key("pc"), "203.0.113.7")
    tasks.redis.set(cluster.egress_key("laptop"), "198.51.100.2")

    devices = {device["node"]: device for device in nodes.overview()["devices"]}

    assert devices["pc"]["shares_address_with"] == ["hub"]
    assert devices["laptop"]["shares_address_with"] == []
    nodes.run_list(type("Args", (), {"json": False})())
    text = capsys.readouterr().out
    assert "public address: 203.0.113.7 (shared with pc: video sites limit them together)" in text
    assert "public address: 198.51.100.2\n" in text


def test_a_node_the_hub_refuses_stops_its_workers(monkeypatch):
    from redis.exceptions import AuthenticationError

    instance, redis, started, _ = make_supervisor(monkeypatch, role="node", offered=cluster.TASKS)
    monkeypatch.setattr(cluster.settings.service, "deployment", "node")
    instance.step()
    assert started

    def refused(*arguments):
        raise AuthenticationError("invalid username-password pair or user is disabled.")

    monkeypatch.setattr(cluster, "allowed_tasks", refused)
    instance.step()

    assert instance.state == supervisor.REFUSED
    assert all(process.signals == [signal.SIGTERM] for process in started)


def test_a_revoked_nodes_refusals_are_not_reported_but_the_hubs_are(monkeypatch):
    import logging

    from redis.exceptions import AuthenticationError

    from app import error_report

    error = AuthenticationError("invalid username-password pair or user is disabled.")
    record = logging.LogRecord("app.worker.tasks", logging.ERROR, "tasks.py", 1, "Could not update the lease", (), None)
    record.exc_info = (type(error), error, None)

    monkeypatch.setenv("DEPLOYMENT", "node")
    assert error_report.is_redis_outage(record)
    monkeypatch.setenv("DEPLOYMENT", "compose")
    assert not error_report.is_redis_outage(record)


def test_a_node_without_the_cloud_buckets_keys_does_not_take_its_settings():
    # The hub's STORAGE_ bucket is RunPod's; a node that does not dispatch,
    # with job files in the hub's own bucket, has none of its keys.
    hub = {"STORAGE_BUCKET": "runpod-audio", "STORAGE_ENDPOINT": "https://r2.example.com", "RESULT_RETENTION": "1d"}

    without = cluster.apply_hub_settings({}, hub, storage_keys=False)
    assert "STORAGE_BUCKET" not in without and "STORAGE_ENDPOINT" not in without
    assert without["RESULT_RETENTION"] == "1d"

    with_keys = cluster.apply_hub_settings({}, hub, storage_keys=True)
    assert with_keys["STORAGE_BUCKET"] == "runpod-audio"


def test_a_node_reads_whether_it_has_the_cloud_keys_from_its_secrets(monkeypatch, tmp_path, default_settings):
    redis = fakeredis.FakeRedis(decode_responses=True)
    redis.hset(cluster.HUB_KEY, mapping={"protocol": str(cluster.PROTOCOL),
                                         "settings": json.dumps({"STORAGE_BUCKET": "runpod-audio"})})
    monkeypatch.delenv(cluster.SYNCED_HASH_VARIABLE, raising=False)
    default_settings.secrets_dir = str(tmp_path)
    calls = []

    supervisor.take_hub_settings(redis, ["node"], execve=lambda path, args, env: calls.append(env))
    assert "STORAGE_BUCKET" not in calls[-1]

    (tmp_path / "storage_access_key").write_text("a")
    (tmp_path / "storage_secret_key").write_text("b")
    supervisor.take_hub_settings(redis, ["node"], execve=lambda path, args, env: calls.append(env))
    assert calls[-1]["STORAGE_BUCKET"] == "runpod-audio"


def test_a_node_invited_without_dispatch_does_not_offer_it_or_complain(monkeypatch, tmp_path, default_settings):
    from app.worker import launcher

    monkeypatch.setattr(cluster.settings.service, "deployment", "node")
    monkeypatch.setattr(launcher, "cuda_job_capacities", lambda: None)
    monkeypatch.setattr(supervisor, "ensure_valid", lambda role: pytest.fail("RunPod checked"))
    default_settings.outsource.provider = "runpod"
    default_settings.secrets_dir = str(tmp_path)
    instance = supervisor.Supervisor.__new__(supervisor.Supervisor)
    instance.role, instance.problem, instance.local_plan, instance.remote_plan = "node", "", None, None
    monkeypatch.setattr(supervisor, "OFFERED", {"node": ("dispatch",)})
    assert supervisor.Supervisor.capabilities(instance) == []
    assert instance.problem == ""


def test_each_device_shows_what_its_jobs_run_on_and_how_many_at_once(api_context, capsys):
    from app.admin import nodes

    cluster.register(tasks.redis, "laptop", ["transcribe"])
    gpu = {"kind": "gpu", "places": 3, "limited_by": "CPU cores", "arrangements": True, "gpus": [
        {"index": 0, "name": "NVIDIA GeForce RTX 3060 Ti", "vram_mib": 8192, "free_mib": 5857, "jobs": 3,
         "arrangements": True},
    ]}
    cpu = {"kind": "cpu", "places": 2, "arrangements": False, "name": "Intel(R) Core(TM) i5", "cores": 4,
           "threads": 2, "ram_mib": 16384}
    cluster.publish_status(tasks.redis, "hub", "worker", {"state": "running", "hardware": gpu})
    cluster.publish_status(tasks.redis, "laptop", "worker", {"state": "running", "hardware": cpu})

    devices = {device["node"]: device for device in nodes.overview()["devices"]}
    nodes.run_list(type("Args", (), {"json": False})())
    text = capsys.readouterr().out

    assert devices["hub"]["services"][0]["hardware"] == gpu
    assert "3 place(s) at once (fewer than fit in VRAM: CPU cores); piano and ensemble jobs share them" in text
    assert "GPU 0: NVIDIA GeForce RTX 3060 Ti, 8.0 GiB VRAM, 5.7 GiB free at start, up to 3 job(s), ensemble jobs" in text
    assert "2 place(s) at once; piano jobs only" in text
    assert "CPU: Intel(R) Core(TM) i5, 4 core(s), 2 thread(s) for the models, 16.0 GiB RAM" in text


def test_the_supervisor_publishes_its_hardware(monkeypatch):
    instance, redis, _, _ = make_supervisor(monkeypatch)
    instance.local_plan = launcher.WorkerPlan(processes=1, threads=6, cpu_threads=None, summary="1 GPU(s)",
                                              ensemble=True, places=3, hardware={"kind": "gpu", "places": 3})

    instance.step()

    assert cluster.statuses(redis)[0]["hardware"] == {"kind": "gpu", "places": 3}
