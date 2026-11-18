"""Clips: transcribing only a part of a video or file (app.clips)."""

import json
import math
import sys
import types
from pathlib import Path

import pytest

from app import clips
from app.admin.__main__ import main as admin_main
from app.api.routes import transcriptions
from app.transcription import audio
from app.worker import tasks


AUTH = {"Authorization": "Bearer user"}
URL = "https://example.com/video"


# --- Checking a clip ---------------------------------------------------------


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        (None, None, None),
        (0, None, None),
        (0, 130, None),
        # An end a little past the measured one means "to the end".
        (0, 131.5, None),
        (30, None, clips.Clip(30.0, None)),
        (30, 131, clips.Clip(30.0, None)),
        (None, 60, clips.Clip(0.0, 60.0)),
        (30.25, 95.5, clips.Clip(30.25, 95.5)),
        (0.0004, 60, clips.Clip(0.0, 60.0)),
        (128.5, None, clips.Clip(128.5, None)),
    ],
)
def test_clips_within_the_audio(start, end, expected):
    assert clips.check_clip(start, end, 130.0) == expected


@pytest.mark.parametrize(
    ("start", "end", "message"),
    [
        (-1, None, "0 seconds or later"),
        (math.nan, None, "0 seconds or later"),
        (math.inf, None, "0 seconds or later"),
        (None, 0, "after 0 seconds"),
        (None, math.nan, "after 0 seconds"),
        (None, -5, "after 0 seconds"),
        (0, 140, "after the end of the audio, at 2:10"),
        (130, None, "start is after the end"),
        (500, None, "start is after the end"),
        (129.5, None, "at least 1 second"),
        (60, 60, "at least 1 second"),
        (60, 30, "at least 1 second"),
        (60, 60.5, "at least 1 second"),
    ],
)
def test_clips_outside_the_audio_are_refused(start, end, message):
    with pytest.raises(clips.InvalidClip, match=message):
        clips.check_clip(start, end, 130.0)


def test_clip_length_and_fields():
    assert clips.Clip(30, 95.5).length(130) == 65.5
    assert clips.Clip(30, None).length(130) == 100
    assert clips.Clip(30, 95.5).fields() == {"clip_start": "30.000", "clip_end": "95.500"}
    assert clips.Clip(30, None).fields() == {"clip_start": "30.000"}


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({}, None),
        ({"clip_start": "30.000", "clip_end": "95.500"}, clips.Clip(30.0, 95.5)),
        ({"clip_start": "30.000"}, clips.Clip(30.0, None)),
        ({"clip_start": "0.000", "clip_end": "60.000"}, clips.Clip(0.0, 60.0)),
        ({"clip_start": "0.000"}, None),
        # Fields that cannot be read are the whole audio, cut at the charged length.
        ({"clip_start": "x"}, None),
        ({"clip_start": "nan"}, None),
        ({"clip_start": "-3"}, None),
        ({"clip_start": "40", "clip_end": "30"}, None),
        ({"clip_start": "40", "clip_end": "inf"}, None),
    ],
)
def test_stored_clip(fields, expected):
    assert clips.stored_clip(fields) == expected


# --- Submitting a link -------------------------------------------------------


@pytest.fixture
def billed(api_context):
    client, settings, billing = api_context
    settings.billing.provider = "google_play"
    settings.billing.products = {"starter": 30}
    settings.billing.free_minutes = 0
    billing.credit_purchase("user", "starter", "secret-token", "GPA.1", "user@example.com")
    return client, settings, billing


def submit(client, **clip):
    return client.post("/api/transcriptions", headers=AUTH, json={"source_url": URL, **clip})


def test_a_clip_of_a_link_is_charged_and_cut_to_its_length(billed):
    client, _, billing = billed
    # The fixture's video is 130 seconds, which costs 2 minutes.
    response = submit(client, start_seconds=30, end_seconds=95.5)

    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]
    job = tasks.redis.hgetall(tasks.job_key(job_id))
    assert job["duration"] == "65.5"
    assert (job["clip_start"], job["clip_end"], job["source_duration"]) == ("30.000", "95.500", "130.0")
    assert job["full_cost"] == "1"
    assert billing.get_balance("user") == 29

    status = client.get(f"/api/transcriptions/{job_id}", headers=AUTH).json()
    assert (status["start_seconds"], status["end_seconds"]) == (30.0, 95.5)


def test_a_clip_to_the_end_of_a_link(billed):
    client, _, _ = billed
    response = submit(client, start_seconds=10)

    job = tasks.redis.hgetall(tasks.job_key(response.json()["job_id"]))
    assert job["duration"] == "120.0"
    assert job["clip_start"] == "10.000" and "clip_end" not in job
    status = client.get(f"/api/transcriptions/{response.json()['job_id']}", headers=AUTH).json()
    assert (status["start_seconds"], status["end_seconds"]) == (10.0, None)


def test_a_clip_of_the_whole_video_is_a_plain_job(billed):
    client, _, billing = billed
    response = submit(client, start_seconds=0, end_seconds=131)

    job_id = response.json()["job_id"]
    job = tasks.redis.hgetall(tasks.job_key(job_id))
    assert job["duration"] == "130.0"
    assert not {"clip_start", "clip_end", "source_duration"} & set(job)
    assert billing.get_balance("user") == 28
    status = client.get(f"/api/transcriptions/{job_id}", headers=AUTH).json()
    assert (status["start_seconds"], status["end_seconds"]) == (None, None)


@pytest.mark.parametrize(
    "clip",
    [
        {"start_seconds": 200},
        {"end_seconds": 300},
        {"start_seconds": 60, "end_seconds": 30},
        {"start_seconds": 60, "end_seconds": 60.5},
    ],
)
def test_a_clip_outside_the_video_is_refused_and_leaves_nothing(billed, clip):
    client, _, billing = billed
    response = submit(client, **clip)

    assert response.status_code == 422
    assert response.json()["detail"]["error_code"] == "invalid_clip"
    assert list(tasks.redis.scan_iter("transcription:*")) == []
    assert billing.get_balance("user") == 30
    # The place it held is free again.
    assert submit(client).status_code == 202


@pytest.mark.parametrize(
    "body",
    [
        '{"source_url": "%s", "start_seconds": NaN}' % URL,
        '{"source_url": "%s", "end_seconds": Infinity}' % URL,
        '{"source_url": "%s", "start_seconds": -1}' % URL,
        '{"source_url": "%s", "end_seconds": 0}' % URL,
        '{"source_url": "%s", "start_seconds": "1e999"}' % URL,
        '{"source_url": "%s", "start_seconds": "30; rm -rf /"}' % URL,
        '{"source_url": "%s", "start_seconds": [1]}' % URL,
    ],
)
def test_hostile_clip_values_are_refused_before_anything_runs(billed, body):
    client, _, billing = billed
    response = client.post(
        "/api/transcriptions", headers={**AUTH, "Content-Type": "application/json"}, content=body
    )

    assert response.status_code == 422
    assert list(tasks.redis.scan_iter("transcription:*")) == []
    assert billing.get_balance("user") == 30


def test_a_retried_clip_finds_its_first_job(billed):
    client, _, billing = billed
    headers = {**AUTH, "Idempotency-Key": "clip-1"}
    first = client.post("/api/transcriptions", headers=headers, json={"source_url": URL, "start_seconds": 30})
    again = client.post("/api/transcriptions", headers=headers, json={"source_url": URL, "start_seconds": 30})

    assert first.json()["job_id"] == again.json()["job_id"]
    assert billing.get_balance("user") == 29


# --- Uploads and the cost ----------------------------------------------------


@pytest.fixture
def upload(billed, monkeypatch):
    client, _, _ = billed
    sent = []
    monkeypatch.setattr(transcriptions, "probe_audio", lambda path, measure=True, max_seconds=None: 300.0)
    monkeypatch.setattr(tasks.transcribe_job, "send", lambda *args, **kwargs: sent.append((args, kwargs)))

    def post(**params):
        return client.post(
            "/api/transcriptions/upload",
            params={"filename": "Song.mp3", **params},
            content=b"audio",
            headers={**AUTH, "Content-Type": "application/octet-stream"},
        )

    return post, sent


def test_a_clip_of_an_upload_is_charged_for_its_length(billed, upload):
    _, settings, billing = billed
    post, sent = upload

    response = post(start_seconds=60, end_seconds=180)

    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]
    job = tasks.redis.hgetall(tasks.job_key(job_id))
    assert (job["duration"], job["clip_start"], job["clip_end"]) == ("120.0", "60.000", "180.000")
    assert job["source"] == "upload" and job["title"] == "Song"
    assert billing.get_balance("user") == 28
    assert sent == [((job_id,), {"upload_title": "Song"})]


def test_a_clip_outside_an_upload_is_refused_and_its_file_removed(billed, upload):
    _, settings, billing = billed
    post, sent = upload

    response = post(start_seconds=10, end_seconds=400)

    assert response.status_code == 422
    assert response.json()["detail"]["error_code"] == "invalid_clip"
    assert sent == []
    jobs = Path(settings.service.data_dir) / "jobs"
    assert not jobs.exists() or list(jobs.iterdir()) == []
    assert list(tasks.redis.scan_iter("transcription:*")) == []
    assert billing.get_balance("user") == 30


@pytest.mark.parametrize("params", [{"start_seconds": "nan"}, {"end_seconds": "inf"}, {"start_seconds": "-2"}])
def test_hostile_clip_values_of_an_upload_are_refused(billed, upload, params):
    post, sent = upload
    assert post(**params).status_code == 422
    assert sent == []


def test_cost_of_a_clip(api_context):
    client, _, _ = api_context

    whole = client.get("/api/billing/cost", params={"duration_seconds": 300}).json()
    clip = client.get("/api/billing/cost", params={"duration_seconds": 300, "start_seconds": 60, "end_seconds": 120}).json()
    refused = client.get("/api/billing/cost", params={"duration_seconds": 300, "start_seconds": 400})

    assert whole == {"duration_seconds": 300.0, "cost_minutes": 5}
    assert clip == {"duration_seconds": 300.0, "cost_minutes": 1, "clip_seconds": 60.0}
    assert refused.status_code == 422 and refused.json()["detail"]["error_code"] == "invalid_clip"


def test_server_info_says_clips_are_supported(api_context):
    client, _, _ = api_context
    assert client.get("/api/server-info").json()["trimming"] is True


# --- The worker --------------------------------------------------------------


def install_pipeline(monkeypatch, seen):
    module = types.ModuleType("app.transcription.pipeline")

    def transcribe_source(output_dir, **kwargs):
        seen.append(kwargs)
        midi = output_dir / "transcription.mid"
        midi.write_bytes(b"MThd")
        return midi

    module.transcribe_source = transcribe_source
    monkeypatch.setitem(sys.modules, "app.transcription.pipeline", module)


def test_the_worker_transcribes_the_stored_clip(billed, monkeypatch):
    client, _, billing = billed
    job_id = submit(client, start_seconds=30, end_seconds=95.5).json()["job_id"]
    seen = []
    install_pipeline(monkeypatch, seen)

    tasks.transcribe_job.fn(job_id, URL)

    assert seen[0]["clip"] == clips.Clip(30.0, 95.5)
    assert seen[0]["max_seconds"] == 65.5
    assert tasks.redis.hget(tasks.job_key(job_id), "status") == "completed"
    assert billing.get_balance("user") == 29


def test_a_job_whose_worker_stopped_keeps_its_clip(billed, monkeypatch):
    """A power cut or crash mid-job: the job runs again from its stored
    fields, so the same part, charged once."""
    client, _, billing = billed
    job_id = submit(client, start_seconds=30, end_seconds=95.5).json()["job_id"]
    monkeypatch.setattr(tasks, "requeue_job", lambda *args, **kwargs: True)
    # The first attempt dies with the machine.
    tasks.redis.hset(tasks.job_key(job_id), mapping={"status": "processing", "progress": "0.4"})
    tasks.redis.hset(tasks.job_key(job_id), "status", "queued")
    seen = []
    install_pipeline(monkeypatch, seen)

    tasks.transcribe_job.fn(job_id, URL)

    assert seen[0]["clip"] == clips.Clip(30.0, 95.5) and seen[0]["max_seconds"] == 65.5
    assert billing.get_balance("user") == 29


def test_the_remote_dispatcher_cuts_the_clip_before_sending(billed, monkeypatch):
    from app.outsource import remote

    fetched = []

    class Stop(Exception):
        pass

    def fake_fetch(output_dir, **kwargs):
        fetched.append(kwargs)
        raise Stop()

    monkeypatch.setattr(remote, "fetch_audio", fake_fetch)
    context = tasks.JobContext("job", Path("."), max_seconds=65.5, clip=clips.Clip(30.0, 95.5))
    with pytest.raises(Stop):
        remote.transcribe_remotely(context, source_url=URL, max_seconds=65.5, clip=context.clip)

    assert fetched[0]["clip"] == clips.Clip(30.0, 95.5)


def test_admin_requeue_keeps_the_clip_and_charges_only_it(billed, capsys, monkeypatch):
    client, _, billing = billed
    job_id = submit(client, start_seconds=30, end_seconds=95.5).json()["job_id"]
    failing = types.ModuleType("app.transcription.pipeline")
    failing.transcribe_source = lambda **kwargs: (_ for _ in ()).throw(RuntimeError("download failed"))
    monkeypatch.setitem(sys.modules, "app.transcription.pipeline", failing)
    tasks.transcribe_job.fn(job_id, URL)
    assert tasks.redis.hget(tasks.job_key(job_id), "status") == "failed"
    assert billing.get_balance("user") == 30

    assert admin_main(["jobs", "requeue", job_id, "--yes", "--reason", "retry"]) == 0, capsys.readouterr()

    job = tasks.redis.hgetall(tasks.job_key(job_id))
    assert (job["status"], job["duration"], job["clip_start"], job["clip_end"]) == ("queued", "65.5", "30.000", "95.500")
    assert billing.get_balance("user") == 29


# --- Cutting the audio -------------------------------------------------------


@pytest.fixture
def processes(monkeypatch):
    commands = []

    def run_process(command, cancellation_callback=None):
        commands.append(command)
        if command[0] == "yt-dlp":
            output_dir = Path(command[command.index("-o") + 1]).parent
            (output_dir / "download.webm").write_bytes(b"audio")
            return audio.subprocess.CompletedProcess(command, 0, json.dumps({"title": "Nocturne", "duration": 130}), "")
        Path(command[-1]).write_bytes(b"RIFF")
        return audio.subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(audio, "run_process", run_process)
    monkeypatch.setattr(audio, "ensure_safe_source_url", lambda url: url)
    monkeypatch.setattr(audio, "ffmpeg_thread_count", lambda: None)
    return commands


def option(command, name):
    return command[command.index(name) + 1]


def test_a_clip_is_cut_exactly_by_ffmpeg(processes, tmp_path):
    audio.download_audio(URL, tmp_path, max_seconds=65.5, clip=clips.Clip(30.0, 95.5))

    ffmpeg = processes[1]
    # Seeking in the input, before -i, so the output starts at the clip.
    assert ffmpeg.index("-ss") < ffmpeg.index("-i")
    assert option(ffmpeg, "-ss") == "30.000"
    assert option(ffmpeg, "-t") == "65.500"
    assert option(ffmpeg, "-protocol_whitelist") == "file"


def test_a_clip_to_the_end_keeps_the_tolerance(processes, tmp_path):
    audio.download_audio(URL, tmp_path, max_seconds=100, clip=clips.Clip(30.0, None))

    assert option(processes[1], "-ss") == "30.000"
    assert option(processes[1], "-t") == f"{100 + audio.DURATION_TOLERANCE:.3f}"


def test_a_clip_from_the_start_does_not_seek(processes, tmp_path):
    audio.convert_to_wav(tmp_path / "input", tmp_path, max_seconds=60, clip=clips.Clip(0.0, 60.0))

    assert "-ss" not in processes[0]
    assert option(processes[0], "-t") == "60.000"


def test_a_clip_of_an_upload_keeps_the_upload_restrictions(processes, tmp_path, monkeypatch):
    monkeypatch.setattr(audio, "probe_audio", lambda path, measure=True, max_seconds=None: 300.0)

    prepared = audio.prepare_upload(tmp_path / "upload.input", "Song", tmp_path, max_seconds=120, clip=clips.Clip(60, 180))

    ffmpeg = processes[0]
    assert "-format_whitelist" in ffmpeg and ffmpeg.index("-format_whitelist") < ffmpeg.index("-i")
    assert ffmpeg.index("-ss") < ffmpeg.index("-i")
    assert (option(ffmpeg, "-ss"), option(ffmpeg, "-t")) == ("60.000", "120.000")
    assert prepared.metadata["duration"] == 120
