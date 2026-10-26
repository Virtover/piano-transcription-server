from pathlib import Path

import pytest

from app.api.routes import transcriptions
from app.media import UnsupportedMedia
from app.worker import tasks


@pytest.fixture
def upload(api_context, monkeypatch):
    """Upload helper with ffprobe and the worker queue replaced by fakes."""
    client, settings, billing = api_context
    state = {"duration": 130.0, "probe_error": None, "sent": [], "probed": [], "measured": []}

    def fake_probe(path, measure=True, max_seconds=None):
        state["measured"].append((measure, max_seconds))
        state["probed"].append(Path(path).read_bytes())
        if state["probe_error"]:
            raise state["probe_error"]
        return state["duration"]

    def fake_send(*args, **kwargs):
        state["sent"].append((args, kwargs))

    monkeypatch.setattr(transcriptions, "probe_audio", fake_probe)
    monkeypatch.setattr(tasks.transcribe_job, "send", fake_send)

    def post(content=b"audio-bytes", filename="My Song.mp3", headers=None):
        params = {} if filename is None else {"filename": filename}
        return client.post(
            "/api/transcriptions/upload",
            params=params,
            content=content,
            headers={"Content-Type": "application/octet-stream", **(headers or {})},
        )

    state["post"] = post
    return state


def job_dirs(settings):
    jobs = Path(settings.service.data_dir) / "jobs"
    return sorted(path.name for path in jobs.iterdir()) if jobs.exists() else []


def assert_nothing_left(settings):
    assert job_dirs(settings) == []
    assert list(tasks.redis.scan_iter("transcription:*")) == []


def test_upload_queues_job_with_file(api_context, upload):
    client, settings, _ = api_context

    response = upload["post"](b"ID3-audio")

    assert response.status_code == 202
    job_id = response.json()["job_id"]
    stored = Path(settings.service.data_dir) / "jobs" / job_id / tasks.UPLOAD_FILE_NAME
    assert stored.read_bytes() == b"ID3-audio"
    assert upload["sent"] == [((job_id,), {"upload_title": "My Song"})]

    job = tasks.redis.hgetall(tasks.job_key(job_id))
    assert job["status"] == "queued"
    assert job["source"] == "upload"
    assert job["title"] == "My Song"
    assert job["duration"] == "130.0"
    assert "source_url" not in job
    assert tasks.redis.ttl(tasks.job_key(job_id)) == -1

    status = client.get(f"/api/transcriptions/{job_id}").json()
    assert (status["status"], status["title"]) == ("queued", "My Song")


def test_large_streamed_upload_is_stored_completely(api_context, upload):
    _, settings, _ = api_context
    chunks = [bytes([index]) * 65536 for index in range(32)]

    response = upload["post"](iter(chunks))

    assert response.status_code == 202
    stored = Path(settings.service.data_dir) / "jobs" / response.json()["job_id"] / tasks.UPLOAD_FILE_NAME
    assert stored.read_bytes() == b"".join(chunks)


@pytest.mark.parametrize(
    ("filename", "title"),
    [
        ("Nocturne.flac", "Nocturne"),
        ("C:\\Users\\me\\Music\\Prelude in C.wav", "Prelude in C"),
        ("../../etc/passwd", "passwd"),
        ("recording", "recording"),
        (".hidden", ".hidden"),
        ("archive.tar.gz", "archive.tar"),
        ("bad\x00name\n.mp3", "badname"),
        ("   ", "Uploaded file"),
    ],
)
def test_title_comes_from_file_name_only(filename, title):
    assert transcriptions.title_from_filename(filename) == title


def test_file_name_cannot_choose_storage_path(api_context, upload):
    _, settings, _ = api_context

    response = upload["post"](filename="../../escape.mp3")

    assert response.status_code == 202
    assert job_dirs(settings) == [response.json()["job_id"]]
    assert not (Path(settings.service.data_dir) / "escape.mp3").exists()


def test_declared_size_over_limit_is_rejected_before_reading(api_context, upload):
    _, settings, _ = api_context
    settings.limits.max_upload_size_mb = 1

    response = upload["post"](b"x", headers={"Content-Length": str(2 * 1024**2)})

    assert response.status_code == 413
    assert "1 MB" in response.json()["detail"]
    assert upload["probed"] == []
    assert_nothing_left(settings)


def test_streamed_size_over_limit_is_rejected(api_context, upload):
    _, settings, _ = api_context
    settings.limits.max_upload_size_mb = 1

    # A generator body is sent without Content-Length.
    response = upload["post"](iter([b"x" * 600_000, b"x" * 600_000]))

    assert response.status_code == 413
    assert upload["probed"] == []
    assert_nothing_left(settings)


def test_empty_upload_is_rejected(api_context, upload):
    _, settings, _ = api_context

    response = upload["post"](b"")

    assert response.status_code == 422
    assert_nothing_left(settings)


def test_file_name_is_required(api_context, upload):
    _, settings, _ = api_context

    assert upload["post"](filename=None).status_code == 422
    assert upload["post"](filename="").status_code == 422
    assert_nothing_left(settings)


def test_unreadable_file_is_rejected(api_context, upload):
    _, settings, _ = api_context
    upload["probe_error"] = UnsupportedMedia("This file format is not supported")

    response = upload["post"](b"<html>")

    assert response.status_code == 422
    assert response.json()["detail"] == "This file format is not supported"
    assert_nothing_left(settings)


def test_too_long_file_is_rejected(api_context, upload):
    _, settings, _ = api_context
    upload["duration"] = 20 * 60 + 1

    response = upload["post"]()

    assert response.status_code == 413
    assert upload["sent"] == []
    assert_nothing_left(settings)


def test_upload_requires_authentication_when_billing(api_context, upload):
    _, settings, _ = api_context
    settings.billing.provider = "google_play"

    assert upload["post"]().status_code == 401
    assert upload["post"](headers={"Authorization": "Bearer invalid"}).status_code == 401
    assert_nothing_left(settings)


def test_upload_reserves_minutes(api_context, upload):
    client, settings, billing = api_context
    settings.billing.provider = "google_play"
    settings.billing.products = {"starter": 10}
    billing.credit_purchase("user", "starter", "token", "order")
    upload["duration"] = 300

    response = upload["post"](headers={"Authorization": "Bearer user"})

    assert response.status_code == 202
    assert response.json()["minutes"] == 5
    job = tasks.redis.hgetall(tasks.job_key(response.json()["job_id"]))
    assert (job["user_id"], job["full_cost"]) == ("user", "5")
    assert billing.reserved_job_ids() == [response.json()["job_id"]]

    other = client.get(
        f"/api/transcriptions/{response.json()['job_id']}",
        headers={"Authorization": "Bearer someone-else"},
    )
    assert other.status_code == 404


def test_upload_with_insufficient_minutes_is_removed(api_context, upload):
    _, settings, billing = api_context
    settings.billing.provider = "google_play"
    settings.billing.free_minutes = 1
    settings.billing.free_minutes_period = "1d"

    response = upload["post"](headers={"Authorization": "Bearer user"})

    assert response.status_code == 402
    assert response.json()["detail"]["required_minutes"] == 2
    assert billing.reserved_job_ids() == []
    assert_nothing_left(settings)


def test_upload_without_minutes_is_refused_before_reading(api_context, upload):
    _, settings, _ = api_context
    settings.billing.provider = "google_play"

    response = upload["post"](headers={"Authorization": "Bearer user"})

    assert response.status_code == 402
    assert response.json()["detail"]["required_minutes"] == 1
    assert upload["probed"] == []
    assert_nothing_left(settings)


def test_upload_length_is_measured_up_to_the_limit(api_context, upload):
    _, settings, _ = api_context

    assert upload["post"]().status_code == 202
    settings.limits.max_video_length_minutes = None
    assert upload["post"]().status_code == 202

    assert upload["measured"] == [(True, 20 * 60), (True, None)]


def test_upload_with_unavailable_worker_is_removed(api_context, upload, monkeypatch):
    _, settings, billing = api_context
    settings.billing.provider = "google_play"
    settings.billing.products = {"starter": 10}
    billing.credit_purchase("user", "starter", "token", "order")

    def broken_send(*args, **kwargs):
        raise ConnectionError("broker is down")

    monkeypatch.setattr(tasks.transcribe_job, "send", broken_send)
    response = upload["post"](headers={"Authorization": "Bearer user"})

    assert response.status_code == 503
    assert billing.get_balance("user") == 10
    assert_nothing_left(settings)


def test_upload_is_rejected_until_startup_finishes(api_context, upload):
    _, settings, _ = api_context
    transcriptions.transcription_submissions_enabled = False

    assert upload["post"]().status_code == 503
    assert_nothing_left(settings)


def test_cleanup_does_not_remove_file_during_upload(api_context, upload, monkeypatch):
    _, settings, _ = api_context

    def probe_while_cleanup_runs(path, measure=True, max_seconds=None):
        job = tasks.redis.hgetall(tasks.job_key(Path(path).parent.name))
        assert job["status"] == "uploading"
        assert 0 < tasks.redis.ttl(tasks.job_key(Path(path).parent.name)) <= transcriptions.PLACEHOLDER_TTL
        tasks.cleanup_expired_jobs()
        assert Path(path).exists()
        return 130.0

    monkeypatch.setattr(transcriptions, "probe_audio", probe_while_cleanup_runs)

    assert upload["post"]().status_code == 202


def test_uploaded_job_can_be_cancelled(api_context, upload):
    client, _, _ = api_context
    job_id = upload["post"]().json()["job_id"]

    response = client.delete(f"/api/transcriptions/{job_id}")

    assert response.json()["status"] == "cancelled"


def test_cost_for_file_duration(api_context):
    client, _, _ = api_context

    response = client.get("/api/billing/cost", params={"duration_seconds": 300})

    assert response.json() == {"duration_seconds": 300.0, "cost_minutes": 5}


@pytest.mark.parametrize(
    ("params", "status"),
    [
        ({}, 422),
        ({"duration_seconds": 0}, 422),
        ({"duration_seconds": 300, "source_url": "https://example.com/video"}, 422),
        ({"duration_seconds": 20 * 60 + 1}, 413),
    ],
)
def test_cost_for_file_duration_is_validated(api_context, params, status):
    client, _, _ = api_context

    assert client.get("/api/billing/cost", params=params).status_code == status


def test_server_info_exposes_upload_limit(api_context):
    client, settings, _ = api_context
    settings.limits.max_upload_size_mb = 150

    assert client.get("/api/server-info").json()["max_upload_size_mb"] == 150
