"""Piano arrangements through the API: the request, the stored job, routing,
the status and file name, retries, an administrator's requeue, and many
submissions at once."""

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app import job_limits
from app.admin import jobs as admin_jobs
from app.api.routes import transcriptions
from app.outsource import routing
from app.worker import tasks

LINK = {"source_url": "https://example.com/video"}
ACTORS = ("transcribe_job", "arrange_job", "transcribe_remote_job")


@pytest.fixture
def sent(api_context, monkeypatch):
    """The messages each actor was sent, as (actor name, arguments, options)."""
    _, settings, _ = api_context
    settings.worker.arrangement = True
    messages = []
    lock = threading.Lock()
    for name in ACTORS:
        def record(*args, _name=name, **kwargs):
            with lock:
                messages.append((_name, args, kwargs))

        monkeypatch.setattr(getattr(tasks, name), "send", record)
    monkeypatch.setattr(transcriptions, "probe_audio", lambda path, measure=True, max_seconds=None: 130.0)
    return messages


def heartbeat(arrangement, worker="worker-1", capacity=2):
    routing.publish_heartbeat(tasks.redis, capacity, 60.0, worker, arrangement=arrangement)


def stored(job_id):
    return tasks.redis.hgetall(tasks.job_key(job_id))


def submit(client, **fields):
    return client.post("/api/transcriptions", json={**LINK, **fields})


def test_arrangement_is_accepted_and_stored_with_the_default_style(api_context, sent):
    client, _, _ = api_context

    response = submit(client, mode="arrangement")

    assert response.status_code == 202
    job_id = response.json()["job_id"]
    job = stored(job_id)
    assert (job["mode"], job["style"], job["backend"]) == ("arrangement", "level2", "local")
    assert [(name, args) for name, args, _ in sent] == [("arrange_job", (job_id, LINK["source_url"]))]


@pytest.mark.parametrize("style", ["level1", "level2", "level3"])
def test_arrangement_style_is_stored(api_context, sent, style):
    client, _, _ = api_context

    job_id = submit(client, mode="arrangement", style=style).json()["job_id"]

    assert stored(job_id)["style"] == style
    assert client.get(f"/api/transcriptions/{job_id}").json()["style"] == style


def test_an_unknown_style_or_mode_is_refused(api_context, sent):
    client, _, _ = api_context

    assert submit(client, mode="arrangement", style="level9").status_code == 422
    assert submit(client, mode="remix").status_code == 422
    assert sent == []
    assert tasks.redis.scard(job_limits.ALL_JOBS_KEY) == 0


@pytest.mark.parametrize("fields", [{"style": "level1"}, {"mode": "transcription", "style": "level2"}])
def test_a_style_without_arrangement_mode_is_refused(api_context, sent, fields):
    client, _, _ = api_context

    assert submit(client, **fields).status_code == 422
    assert sent == []


def test_a_transcription_stores_no_mode(api_context, sent):
    client, _, _ = api_context

    job_id = submit(client).json()["job_id"]

    assert "mode" not in stored(job_id) and "style" not in stored(job_id)
    assert [name for name, _, _ in sent] == ["transcribe_job"]
    status = client.get(f"/api/transcriptions/{job_id}").json()
    assert (status["mode"], status["style"]) == ("transcription", None)


def test_a_job_stored_before_arrangements_reads_as_a_transcription(api_context, sent):
    client, _, _ = api_context
    job_id = submit(client).json()["job_id"]
    tasks.redis.hdel(tasks.job_key(job_id), "mode", "style")

    status = client.get(f"/api/transcriptions/{job_id}").json()

    assert (status["mode"], status["style"]) == ("transcription", None)


def test_arrangements_off_answer_409(api_context, sent):
    client, settings, _ = api_context
    settings.worker.arrangement = False

    response = submit(client, mode="arrangement")

    assert response.status_code == 409
    assert response.json()["detail"]["error_code"] == "arrangement_unavailable"
    assert sent == [] and tasks.redis.scard(job_limits.ALL_JOBS_KEY) == 0
    # Transcriptions are not affected.
    assert submit(client).status_code == 202


def test_live_workers_that_do_not_arrange_answer_503_without_outsourcing(api_context, sent):
    client, _, _ = api_context
    heartbeat(arrangement=False)

    response = submit(client, mode="arrangement")

    assert response.status_code == 503
    assert response.json()["detail"]["error_code"] == "arrangement_unavailable"
    assert sent == [] and tasks.redis.scard(job_limits.ALL_JOBS_KEY) == 0
    assert submit(client).status_code == 202


def test_one_arranging_worker_among_others_is_enough(api_context, sent):
    client, _, _ = api_context
    heartbeat(arrangement=False, worker="a")
    heartbeat(arrangement=True, worker="b")

    assert submit(client, mode="arrangement").status_code == 202


def test_without_any_live_worker_an_arrangement_is_queued(api_context, sent):
    client, _, _ = api_context

    assert submit(client, mode="arrangement").status_code == 202
    assert [name for name, _, _ in sent] == ["arrange_job"]


def test_without_an_arranging_worker_an_arrangement_goes_to_runpod(api_context, sent):
    client, settings, _ = api_context
    settings.outsource.provider = "runpod"
    heartbeat(arrangement=False)

    job_id = submit(client, mode="arrangement").json()["job_id"]

    assert stored(job_id)["backend"] == "remote"
    assert [name for name, _, _ in sent] == ["transcribe_remote_job"]
    assert tasks.redis.sismember(routing.ACTIVE_KEYS["remote"], job_id)


def test_an_arrangement_stays_local_on_an_arranging_worker_with_outsourcing_on(api_context, sent):
    client, settings, _ = api_context
    settings.outsource.provider = "runpod"
    heartbeat(arrangement=True)

    job_id = submit(client, mode="arrangement").json()["job_id"]

    assert stored(job_id)["backend"] == "local"
    assert [name for name, _, _ in sent] == ["arrange_job"]


def test_the_midi_of_an_arrangement_is_named_arrangement(api_context, sent):
    client, settings, _ = api_context
    arranged = submit(client, mode="arrangement").json()["job_id"]
    plain = submit(client).json()["job_id"]
    for job_id in (arranged, plain):
        result = Path(settings.service.data_dir) / "jobs" / job_id / "transcription.mid"
        result.parent.mkdir(parents=True, exist_ok=True)
        result.write_bytes(b"MThd")
        tasks.redis.hset(tasks.job_key(job_id), mapping={"status": "completed", "result": str(result)})

    assert "arrangement.mid" in client.get(f"/api/transcriptions/{arranged}/midi").headers["content-disposition"]
    assert "transcription.mid" in client.get(f"/api/transcriptions/{plain}/midi").headers["content-disposition"]


def test_an_upload_takes_mode_and_style_as_query_parameters(api_context, sent):
    client, _, _ = api_context

    response = client.post(
        "/api/transcriptions/upload",
        params={"filename": "Song.mp3", "mode": "arrangement", "style": "level3"},
        content=b"audio",
    )

    assert response.status_code == 202
    job_id = response.json()["job_id"]
    job = stored(job_id)
    assert (job["mode"], job["style"], job["source"]) == ("arrangement", "level3", "upload")
    assert sent == [("arrange_job", (job_id,), {"upload_title": "Song"})]


def test_an_upload_refuses_what_a_link_refuses(api_context, sent):
    client, settings, _ = api_context

    def upload(**params):
        return client.post("/api/transcriptions/upload", params={"filename": "Song.mp3", **params}, content=b"audio")

    assert upload(style="level1").status_code == 422
    assert upload(mode="arrangement", style="x").status_code == 422
    settings.worker.arrangement = False
    refused = upload(mode="arrangement")
    assert refused.status_code == 409
    assert refused.json()["detail"]["error_code"] == "arrangement_unavailable"
    assert sent == [] and tasks.redis.scard(job_limits.ALL_JOBS_KEY) == 0


def test_a_retry_with_the_same_key_returns_the_earlier_arrangement(api_context, sent):
    client, settings, _ = api_context
    headers = {"Idempotency-Key": "key-1"}
    first = client.post("/api/transcriptions", json={**LINK, "mode": "arrangement", "style": "level1"}, headers=headers)

    # Even when the feature was turned off meanwhile.
    settings.worker.arrangement = False
    second = client.post("/api/transcriptions", json={**LINK, "mode": "arrangement"}, headers=headers)

    assert (first.status_code, second.status_code) == (202, 202)
    assert second.json()["job_id"] == first.json()["job_id"]
    assert len(sent) == 1
    assert tasks.redis.scard(job_limits.ALL_JOBS_KEY) == 1


def fail(job_id, backend):
    """The job as it is after it failed: out of the places and the routing sets."""
    tasks.redis.hset(tasks.job_key(job_id), mapping={"status": "failed", "error": "boom"})
    routing.mark_finished(tasks.redis, backend, job_id)
    job_limits.release(tasks.redis, job_id, "anonymous")


def test_an_administrator_requeue_keeps_the_mode_and_routes_it_as_an_arrangement(api_context, sent):
    client, settings, _ = api_context
    settings.outsource.provider = "runpod"
    heartbeat(arrangement=False)
    job_id = submit(client, mode="arrangement", style="level3").json()["job_id"]
    assert stored(job_id)["backend"] == "remote"
    fail(job_id, "remote")
    sent.clear()
    # An arranging worker comes up: the same arrangement now runs locally.
    heartbeat(arrangement=True, worker="worker-2")

    result = admin_jobs.requeue(job_id, None, "tester", "again")

    job = stored(job_id)
    assert (job["mode"], job["style"], job["status"]) == ("arrangement", "level3", "queued")
    assert result["backend"] == "local"
    assert [name for name, _, _ in sent] == ["arrange_job"]


def test_an_administrator_requeue_without_an_arranging_worker_goes_remote(api_context, sent):
    client, settings, _ = api_context
    settings.outsource.provider = "runpod"
    heartbeat(arrangement=True)
    job_id = submit(client, mode="arrangement").json()["job_id"]
    fail(job_id, "local")
    sent.clear()
    # The arranging worker stopped, and one that does not arrange took its place.
    tasks.redis.flushdb()
    tasks.redis.hset(tasks.job_key(job_id), mapping={"status": "failed", "mode": "arrangement", "style": "level2",
                                                     "source_url": LINK["source_url"]})
    heartbeat(arrangement=False)

    result = admin_jobs.requeue(job_id, None, "tester", "again")

    assert result["backend"] == "remote"
    assert [name for name, _, _ in sent] == ["transcribe_remote_job"]
    assert stored(job_id)["mode"] == "arrangement"


@pytest.mark.parametrize("live_worker", [False, True])
def test_an_administrator_requeue_refuses_an_arrangement_that_could_not_run(api_context, sent, live_worker):
    client, settings, _ = api_context
    heartbeat(arrangement=True)
    job_id = submit(client, mode="arrangement").json()["job_id"]
    fail(job_id, "local")
    sent.clear()
    before = stored(job_id)
    if live_worker:
        # Outsourcing off, and the only live worker does not arrange.
        tasks.redis.flushdb()
        tasks.redis.hset(tasks.job_key(job_id), mapping=before)
        heartbeat(arrangement=False)
    else:
        settings.worker.arrangement = False

    with pytest.raises(admin_jobs.AdminError, match="cannot run again"):
        admin_jobs.requeue(job_id, None, "tester", "again")

    assert stored(job_id)["status"] == "failed"
    assert sent == []


def test_the_server_info_says_whether_arrangements_are_on(api_context):
    client, settings, _ = api_context

    settings.worker.arrangement = False
    assert client.get("/api/server-info").json()["arrangement"] is False
    settings.worker.arrangement = True
    assert client.get("/api/server-info").json()["arrangement"] is True


# --- Many submissions at once ----------------------------------------------------

def test_two_hundred_mixed_submissions_are_stored_routed_and_counted_once(api_context, sent):
    client, settings, _ = api_context
    settings.limits.max_active_jobs = 1000
    heartbeat(arrangement=True, capacity=4)
    modes = ["arrangement" if index % 3 == 0 else "transcription" for index in range(200)]

    def submit_one(mode):
        return submit(client, mode=mode, **({"style": "level1"} if mode == "arrangement" else {}))

    with ThreadPoolExecutor(max_workers=16) as pool:
        responses = list(pool.map(submit_one, modes))

    assert {response.status_code for response in responses} == {202}
    ids = [response.json()["job_id"] for response in responses]
    assert len(set(ids)) == 200
    for job_id, mode in zip(ids, modes):
        job = stored(job_id)
        assert job["status"] == "queued"
        assert job.get("mode", "transcription") == mode
        assert (job.get("style") == "level1") == (mode == "arrangement")
    arranged = {job_id for job_id, mode in zip(ids, modes) if mode == "arrangement"}
    assert tasks.redis.smembers(job_limits.ALL_JOBS_KEY) == set(ids)
    assert tasks.redis.smembers(routing.ACTIVE_KEYS["local"]) == set(ids)
    assert tasks.redis.scard(routing.ACTIVE_KEYS["remote"]) == 0
    by_actor = {name: [args[0] for actor, args, _ in sent if actor == name] for name in ACTORS}
    # Each job was sent once, to the actor of its mode.
    assert len(sent) == 200
    assert set(by_actor["arrange_job"]) == arranged and len(by_actor["arrange_job"]) == len(arranged)
    assert set(by_actor["transcribe_job"]) == set(ids) - arranged
    assert by_actor["transcribe_remote_job"] == []


def test_the_total_limit_holds_under_concurrent_mixed_submissions(api_context, sent):
    client, settings, _ = api_context
    settings.limits.max_active_jobs = 20
    modes = ["arrangement" if index % 2 else "transcription" for index in range(100)]

    with ThreadPoolExecutor(max_workers=16) as pool:
        responses = list(pool.map(lambda mode: submit(client, mode=mode), modes))

    accepted = [response.json()["job_id"] for response in responses if response.status_code == 202]
    assert len(accepted) == 20
    assert {response.status_code for response in responses} == {202, 429}
    assert tasks.redis.smembers(job_limits.ALL_JOBS_KEY) == set(accepted)
    assert tasks.redis.smembers(routing.ACTIVE_KEYS["local"]) == set(accepted)
    assert len(sent) == 20


def test_the_per_user_limit_holds_under_concurrent_mixed_submissions(api_context, sent):
    client, settings, billing = api_context
    settings.billing.provider = "google_play"
    settings.billing.products = {"starter": 1000}
    settings.limits.max_active_jobs = 1000
    settings.limits.max_active_jobs_per_user = 5
    for user in ("alice", "bob"):
        billing.credit_purchase(user, "starter", f"token-{user}", f"order-{user}")
    attempts = [(user, "arrangement" if index % 2 else "transcription") for index in range(40) for user in ("alice", "bob")]

    def submit_for(attempt):
        user, mode = attempt
        response = client.post(
            "/api/transcriptions", json={**LINK, "mode": mode}, headers={"Authorization": f"Bearer {user}"}
        )
        return user, response

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(submit_for, attempts))

    for user in ("alice", "bob"):
        statuses = [response.status_code for who, response in results if who == user]
        assert statuses.count(202) == 5
        assert set(statuses) == {202, 429}
    assert tasks.redis.scard(job_limits.ALL_JOBS_KEY) == 10
    assert len(sent) == 10
    # Only accepted jobs hold minutes.
    reserved = billing.read("SELECT COUNT(*) AS n FROM transcription_reservations WHERE status = 'reserved'")
    assert reserved[0]["n"] == 10
