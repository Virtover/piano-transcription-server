"""The variants of a job's result (app.variants): an ensemble job's piano levels, made where the job ran, kept beside
its result, announced in its status, and downloaded by GET /midi?variant=. On a local worker, a node storing into the
hub's bucket, a RunPod endpoint that returns them, an older endpoint that does not (the remote worker makes them
after the job completed, within its own limit), and a hub that only dispatches."""

import base64
import logging
import sys
import threading
import time
import types
from pathlib import Path

import pytest

from app import job_storage, variants
from app.job_storage import PREFIX, S3JobStorage
from app.outsource import remote, routing
from app.outsource.provider import RemoteStatus
from app.transcription.audio import DownloadedAudio
from app.transcription.ensemble import piano_levels
from app.worker import tasks

from test_job_storage import FakeS3
from test_piano_levels import band, pedalled_piano, piano_solo

URL = "https://example.com/video"
EVERY = ["full", "advanced", "intermediate", "easy", "beginner", "raw"]


@pytest.fixture(scope="module")
def ensemble_midi() -> bytes:
    return band()


@pytest.fixture(scope="module")
def expected_levels(ensemble_midi) -> dict[str, bytes]:
    return piano_levels.all_levels(ensemble_midi)


@pytest.fixture
def worker(api_context):
    client, settings, _ = api_context
    settings.worker.ensemble = True
    job_storage.reset()
    yield client, settings
    job_storage.reset()


def queue(job_id="job", ensemble=True, backend="local"):
    fields = {"status": "queued", "progress": "0.0", "backend": backend, "title": "Song"}
    if ensemble:
        fields.update(mode=tasks.ENSEMBLE, mode_name="ensemble")
    tasks.redis.hset(tasks.job_key(job_id), mapping=fields)


def job(job_id="job"):
    return tasks.redis.hgetall(tasks.job_key(job_id))


def install_pipeline(monkeypatch, data: bytes):
    """A local transcription that writes data as its result."""
    def transcribe_source(output_dir, ensemble, **kwargs):
        midi = output_dir / "transcription.mid"
        midi.write_bytes(data)
        return midi

    module = types.ModuleType("app.transcription.pipeline")
    module.transcribe_source = transcribe_source
    monkeypatch.setitem(sys.modules, "app.transcription.pipeline", module)


# --- Names, order, and files -------------------------------------------------------------------------------------

def test_the_names_and_their_files():
    assert variants.ORDER == ("full", "advanced", "intermediate", "easy", "beginner", "raw")
    assert set(variants.LEVELS) == set(piano_levels.LEVELS)
    assert variants.file_name("raw") == job_storage.RESULT_FILE_NAME == "transcription.mid"
    assert variants.file_name("easy") == "piano-easy.mid"
    with pytest.raises(ValueError):
        variants.file_name("../other")
    assert all(variants.is_result_file(variants.file_name(name)) for name in variants.ORDER)
    assert not variants.is_result_file("upload.mp3")


@pytest.mark.parametrize(
    ("stored", "listed"),
    [
        ({}, ["raw"]),
        ({"variants": "raw"}, ["raw"]),
        ({"variants": "beginner,full,raw"}, ["full", "beginner", "raw"]),
        ({"variants": "full,advanced,intermediate,easy,beginner,raw"}, EVERY),
        # names a newer or broken writer stored are never offered
        ({"variants": "full,virtuoso,,raw"}, ["full", "raw"]),
    ],
)
def test_a_jobs_variants_are_read_in_order_with_raw_last(stored, listed):
    assert variants.job_variants(stored) == listed
    assert variants.stored_field(variants.job_variants(stored)) == ",".join(listed)


def test_the_default_is_full_for_an_ensemble_that_has_it():
    assert variants.default_variant({"variants": "full,raw"}, ensemble=True) == "full"
    assert variants.default_variant({"variants": "easy,raw"}, ensemble=True) == "raw"
    assert variants.default_variant({}, ensemble=True) == "raw"
    assert variants.default_variant({"variants": "full,raw"}, ensemble=False) == "raw"


def test_a_file_is_written_whole_and_leaves_no_temporary_file(tmp_path):
    target = tmp_path / "piano-full.mid"
    target.write_bytes(b"old")
    variants.write_atomically(target, b"MThd new")
    assert target.read_bytes() == b"MThd new"
    assert [path.name for path in tmp_path.iterdir()] == ["piano-full.mid"]


def test_a_failed_write_keeps_the_old_file(tmp_path, monkeypatch):
    target = tmp_path / "piano-full.mid"
    target.write_bytes(b"old")

    def broken(source, destination):
        raise OSError("disk full")

    monkeypatch.setattr(variants.os, "replace", broken)
    with pytest.raises(OSError):
        variants.write_atomically(target, b"new")
    assert target.read_bytes() == b"old"
    assert [path.name for path in tmp_path.iterdir()] == ["piano-full.mid"]


# --- Making them in a child process ------------------------------------------------------------------------------

def test_the_child_process_makes_every_level_as_the_module_does(tmp_path, ensemble_midi, expected_levels):
    source = tmp_path / "transcription.mid"
    source.write_bytes(ensemble_midi)
    # one left by an earlier attempt is made again, never kept
    (tmp_path / "piano-easy.mid").write_bytes(b"stale")

    assert variants.make_levels(source, tmp_path, "job") == list(variants.LEVELS)
    for level in variants.LEVELS:
        assert (tmp_path / variants.file_name(level)).read_bytes() == expected_levels[level]
    assert sorted(path.name for path in tmp_path.iterdir()) == sorted(
        ["transcription.mid", *(variants.file_name(level) for level in variants.LEVELS)])


def test_a_result_that_is_not_midi_gets_none_and_never_raises(tmp_path, caplog):
    source = tmp_path / "transcription.mid"
    source.write_bytes(b"MThd")
    with caplog.at_level(logging.WARNING):
        assert variants.make_levels(source, tmp_path, "job") == []
    assert "piano levels not made" in caplog.text


def test_more_notes_than_the_cap_get_none(tmp_path, ensemble_midi, caplog):
    source = tmp_path / "transcription.mid"
    source.write_bytes(ensemble_midi)
    with caplog.at_level(logging.WARNING):
        assert variants.make_levels(source, tmp_path, "job", max_notes=10) == []
    assert "too many notes" in caplog.text
    assert variants.existing(tmp_path) == []


def test_a_run_that_takes_too_long_keeps_what_it_finished(tmp_path, monkeypatch, caplog):
    source = tmp_path / "transcription.mid"
    source.write_bytes(b"MThd")

    def slow(command, **kwargs):
        # beginner and easy done, intermediate cut off half-written
        (tmp_path / "piano-beginner.mid").write_bytes(b"MThd")
        (tmp_path / "piano-easy.mid").write_bytes(b"MThd")
        (tmp_path / ".piano-intermediate.mid.1.2.partial").write_bytes(b"MTh")
        raise variants.subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(variants.subprocess, "run", slow)
    with caplog.at_level(logging.WARNING):
        assert variants.make_levels(source, tmp_path, "job") == ["easy", "beginner"]
    assert "were stopped" in caplog.text
    assert not list(tmp_path.glob("*.partial"))


def test_the_levels_need_neither_torch_nor_the_servers_settings():
    import subprocess

    probe = ("import sys; import app.variants, app.transcription.ensemble.piano_levels; "
             "print(sorted(m for m in ('torch', 'app.config', 'redis', 'boto3') if m in sys.modules))")
    out = subprocess.run([sys.executable, "-c", probe], cwd=Path(variants.__file__).resolve().parent.parent,
                         capture_output=True, text=True, check=True).stdout
    assert out.strip() == "[]"


def test_the_fallback_runs_at_most_its_limit_at_once(caplog):
    fallback = variants.FallbackLevels(limit=1)
    release, ran = threading.Event(), []

    assert fallback.submit("first", lambda: (release.wait(10), ran.append("first")))
    with caplog.at_level(logging.WARNING):
        assert not fallback.submit("second", lambda: ran.append("second"))
    assert "Job second: no piano levels" in caplog.text
    release.set()
    for _ in range(200):
        if ran:
            break
        time.sleep(0.01)
    time.sleep(0.05)
    assert ran == ["first"]
    # its place is free again, also after a failure
    done = threading.Event()
    assert fallback.submit("third", lambda: (_ for _ in ()).throw(RuntimeError("broken")))
    time.sleep(0.1)
    assert fallback.submit("fourth", done.set)
    assert done.wait(5)


# --- A local or node worker --------------------------------------------------------------------------------------

def test_a_local_ensemble_job_gets_every_level_beside_its_result(worker, monkeypatch, ensemble_midi, expected_levels):
    client, settings = worker
    install_pipeline(monkeypatch, ensemble_midi)
    queue()

    tasks.ensemble_job.fn("job", URL)

    stored = job()
    assert stored["status"] == "completed"
    assert stored["variants"] == ",".join(EVERY)
    folder = tasks.job_dir("job")
    for level in variants.LEVELS:
        assert (folder / variants.file_name(level)).read_bytes() == expected_levels[level]
    # the API announces them, and sends each
    assert client.get("/api/transcriptions/job").json()["variants"] == EVERY
    default = client.get("/api/transcriptions/job/midi")
    assert default.status_code == 200 and default.content == expected_levels["full"]
    assert 'filename="ensemble-full.mid"' in default.headers["content-disposition"]
    for level in variants.LEVELS:
        response = client.get("/api/transcriptions/job/midi", params={"variant": level})
        assert response.content == expected_levels[level]
        assert f'filename="ensemble-{level}.mid"' in response.headers["content-disposition"]
    raw = client.get("/api/transcriptions/job/midi", params={"variant": "raw"})
    assert raw.content == ensemble_midi and 'filename="ensemble-raw.mid"' in raw.headers["content-disposition"]
    # retention: every file of the job goes together
    job_storage.delete_job_files("job")
    assert not folder.exists()


def test_the_admin_job_view_shows_a_completed_jobs_variants(worker):
    from app.admin import jobs as admin_jobs

    job_id = "0b6f1c2e-5d4a-4c3b-9a8f-7e6d5c4b3a21"
    tasks.redis.hset(tasks.job_key(job_id), mapping={"status": "completed", "mode": tasks.ENSEMBLE,
                                                      "result": "x", "variants": "full,easy,raw"})
    assert admin_jobs.job_details(job_id)["variants"] == ["full", "easy", "raw"]
    tasks.redis.hset(tasks.job_key(job_id), "status", "processing")
    assert admin_jobs.job_details(job_id)["variants"] is None


@pytest.mark.parametrize("mode_name", [None, "piano"])
def test_a_piano_job_gets_levels_but_still_downloads_its_own_result(worker, monkeypatch, mode_name):
    client, _ = worker
    data = pedalled_piano()
    install_pipeline(monkeypatch, data)
    queue(ensemble=False)
    if mode_name:
        tasks.redis.hset(tasks.job_key("job"), "mode_name", mode_name)
    word = mode_name or "transcription"

    tasks.transcribe_job.fn("job", URL)

    expected = dict(variants.distinct_levels(data))
    assert list(expected) == list(variants.LEVELS)
    assert job()["status"] == "completed" and job()["variants"] == ",".join(EVERY)
    assert client.get("/api/transcriptions/job").json()["variants"] == EVERY
    # without a variant, and as raw: Transkun's own result, under its old name, as old apps expect
    for variant in (None, "raw"):
        response = client.get("/api/transcriptions/job/midi", params={"variant": variant} if variant else {})
        assert response.content == data
        assert f'filename="{word}.mid"' in response.headers["content-disposition"]
    for level in variants.LEVELS:
        response = client.get("/api/transcriptions/job/midi", params={"variant": level})
        assert response.content == expected[level]
        assert f'filename="{word}-{level}.mid"' in response.headers["content-disposition"]
    assert client.get("/api/transcriptions/job/midi", params={"variant": "virtuoso"}).status_code == 422


def test_a_piano_job_whose_levels_are_the_same_lists_only_those_that_differ(worker, monkeypatch):
    client, _ = worker
    install_pipeline(monkeypatch, melody_only())
    queue(ensemble=False)

    tasks.transcribe_job.fn("job", URL)

    assert client.get("/api/transcriptions/job").json()["variants"] == ["full", "raw"]
    refused = client.get("/api/transcriptions/job/midi", params={"variant": "easy"})
    assert refused.status_code == 404 and refused.json()["detail"]["error_code"] == "variant_unavailable"


def test_a_piano_job_from_before_levels_has_its_result_only(worker):
    client, _ = worker
    folder = tasks.job_dir("job")
    folder.mkdir(parents=True)
    (folder / "transcription.mid").write_bytes(b"MThd")
    tasks.redis.hset(tasks.job_key("job"), mapping={"status": "completed", "result": str(folder / "transcription.mid")})
    assert client.get("/api/transcriptions/job").json()["variants"] == ["raw"]
    refused = client.get("/api/transcriptions/job/midi", params={"variant": "full"})
    assert refused.status_code == 404 and refused.json()["detail"]["error_code"] == "variant_unavailable"


def test_the_server_info_says_piano_jobs_get_levels(worker):
    client, _ = worker
    assert client.get("/api/server-info").json()["piano_levels"] is True


def test_a_job_has_no_variants_before_it_completes(worker):
    client, _ = worker
    queue()
    assert client.get("/api/transcriptions/job").json()["variants"] == []


def test_levels_that_cannot_be_made_never_fail_the_job(worker, monkeypatch):
    client, _ = worker
    install_pipeline(monkeypatch, b"MThd")
    queue()

    tasks.ensemble_job.fn("job", URL)

    assert job()["status"] == "completed" and job()["variants"] == "raw"
    assert client.get("/api/transcriptions/job").json()["variants"] == ["raw"]
    # without full, an ensemble job's download is its raw result
    response = client.get("/api/transcriptions/job/midi")
    assert response.content == b"MThd" and 'filename="ensemble-raw.mid"' in response.headers["content-disposition"]
    assert client.get("/api/transcriptions/job/midi", params={"variant": "easy"}).status_code == 404


def test_a_level_that_cannot_be_stored_is_left_out(worker, monkeypatch, ensemble_midi):
    install_pipeline(monkeypatch, ensemble_midi)
    queue()
    local = job_storage.LocalJobStorage()
    save = local.save_variant

    def flaky(job_id, path):
        if path.name == "piano-advanced.mid":
            raise job_storage.StorageUnavailable("no room")
        save(job_id, path)

    monkeypatch.setattr(local, "save_variant", flaky)
    monkeypatch.setattr(job_storage, "_storage", local)

    tasks.ensemble_job.fn("job", URL)

    assert job()["status"] == "completed"
    assert job()["variants"] == "full,intermediate,easy,beginner,raw"


@pytest.fixture
def hub_bucket(worker):
    """A node: JOB_STORAGE=local with the hub's bucket, faked."""
    _, settings = worker
    settings.service.cluster_storage_endpoint = "http://storage:8333"
    client = FakeS3()
    job_storage._buckets[job_storage.HUB_REFERENCE] = S3JobStorage(client, job_storage.HUB_BUCKET,
                                                                  job_storage.HUB_REFERENCE, links=False)
    return client


def test_a_nodes_levels_go_to_the_hubs_bucket_with_its_result(worker, hub_bucket, monkeypatch, ensemble_midi,
                                                             expected_levels):
    client, settings = worker
    install_pipeline(monkeypatch, ensemble_midi)
    queue()

    tasks.ensemble_job.fn("job", URL)

    assert job()["result"] == f"hub:{PREFIX}job/transcription.mid"
    assert job()["variants"] == ",".join(EVERY)
    for level in variants.LEVELS:
        stored = hub_bucket.objects[f"{PREFIX}job/{variants.file_name(level)}"]
        assert stored["body"] == expected_levels[level] and stored["content_type"] == "audio/midi"
    # nothing stays on the node
    assert not list((Path(settings.service.data_dir) / job_storage.WORK_FOLDER).glob("*"))
    # the API sends them from the hub's bucket
    response = client.get("/api/transcriptions/job/midi", params={"variant": "beginner"})
    assert response.content == expected_levels["beginner"]
    assert 'filename="ensemble-beginner.mid"' in response.headers["content-disposition"]
    # a retry fetches the inputs only, and retention deletes every file
    folder = Path(settings.service.data_dir) / "retry"
    job_storage.storage().fetch_inputs("job", folder)
    assert list(folder.iterdir()) == []
    job_storage.delete_job_files("job")
    assert hub_bucket.objects == {}


def test_an_s3_job_keeps_its_levels_when_its_inputs_go(worker):
    client = FakeS3()
    files = S3JobStorage(client, "jobs-bucket")
    for name in ("upload.mp3", "transcription.mid", "piano-full.mid", "piano-easy.mid"):
        client.objects[f"{PREFIX}job/{name}"] = {"body": b"MThd", "metadata": {}, "content_type": None}
    files.discard_inputs("job", Path("unused"))
    assert sorted(client.objects) == [f"{PREFIX}job/piano-easy.mid", f"{PREFIX}job/piano-full.mid",
                                      f"{PREFIX}job/transcription.mid"]


def test_a_signed_link_names_the_level_and_points_at_its_object(worker, expected_levels):
    client, settings = worker
    settings.jobs.storage = "s3"
    bucket = FakeS3()
    job_storage._buckets[job_storage.S3_REFERENCE] = S3JobStorage(bucket, "jobs-bucket")
    for name in ("transcription.mid", "piano-full.mid", "piano-easy.mid"):
        bucket.objects[f"{PREFIX}job/{name}"] = {"body": b"MThd", "metadata": {}, "content_type": None}
    tasks.redis.hset(tasks.job_key("job"), mapping={
        "status": "completed", "result": f"s3:{PREFIX}job/transcription.mid", "mode": tasks.ENSEMBLE,
        "mode_name": "arrangement", "variants": "full,easy,raw"})

    for variant, key in ((None, "piano-full.mid"), ("easy", "piano-easy.mid"), ("raw", "transcription.mid")):
        response = client.get("/api/transcriptions/job/midi", params={"variant": variant} if variant else {},
                              follow_redirects=False)
        assert response.status_code == 307
        assert bucket.signed[-1]["Key"] == f"{PREFIX}job/{key}"
        name = variant or "full"
        assert bucket.signed[-1]["ResponseContentDisposition"] == f'attachment; filename="arrangement-{name}.mid"'
    assert client.get("/api/transcriptions/job/midi", params={"variant": "advanced"},
                      follow_redirects=False).status_code == 404
    # a level the job lists but whose object is gone
    del bucket.objects[f"{PREFIX}job/piano-easy.mid"]
    assert client.get("/api/transcriptions/job/midi", params={"variant": "easy"},
                      follow_redirects=False).status_code == 404


# --- RunPod --------------------------------------------------------------------------------------------------------

class Provider:
    def __init__(self, output):
        self.output = output

    def submit(self, payload):
        return "remote-1"

    def status(self, remote_id, timeout):
        return RemoteStatus("completed", output=self.output)

    def cancel(self, remote_id):
        pass


class AudioStore:
    def upload(self, path):
        return "audio/0.flac"

    def presigned_url(self, key, expires_seconds):
        return "https://storage.example/audio"

    def delete(self, key):
        pass


@pytest.fixture
def runpod(worker, monkeypatch):
    """The remote actor against a fake endpoint, whose output the test sets."""
    _, settings = worker
    settings.outsource.provider = "runpod"
    output = {}

    def fetch(output_dir, **kwargs):
        output_dir.mkdir(parents=True, exist_ok=True)
        wav = output_dir / "audio.wav"
        wav.write_bytes(b"RIFF")
        return DownloadedAudio(path=wav, metadata={}, source_rate=48000)

    real = remote.transcribe_remotely

    def transcribe_remotely(context, **kwargs):
        return real(context, provider=Provider(output), storage=AudioStore(), **kwargs)

    monkeypatch.setattr(remote, "fetch_audio", fetch)
    monkeypatch.setattr(remote, "convert_to_flac", lambda wav, output_dir, cancellation_callback=None: wav)
    monkeypatch.setattr(remote, "transcribe_remotely", transcribe_remotely)
    monkeypatch.setattr(remote.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(remote, "record_jobs_per_worker", lambda redis, capacity: None)
    monkeypatch.setattr(routing, "wait_for_remote_slot", lambda redis, job_id, cancelled: True)
    return output


def encoded(data: bytes) -> str:
    return base64.b64encode(data).decode()


def wait_for(condition, seconds=60.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return False


def test_levels_from_the_endpoint_are_stored_as_they_came(worker, runpod, monkeypatch, ensemble_midi, caplog):
    client, _ = worker
    sent = {level: b"MThd from the endpoint " + level.encode() for level in variants.LEVELS if level != "easy"}
    runpod.update(midi_base64=encoded(ensemble_midi),
                  piano_levels_base64={**{level: encoded(data) for level, data in sent.items()},
                                       # not a MIDI file: left out, never the job
                                       "easy": encoded(b"<html>"), "virtuoso": encoded(b"MThd")})

    def never(*args, **kwargs):
        raise AssertionError("the levels came with the result")

    monkeypatch.setattr(variants, "make_levels", never)
    queue(backend="remote")

    with caplog.at_level(logging.WARNING):
        tasks.transcribe_remote_job.fn("job", URL)

    assert job()["status"] == "completed"
    assert job()["variants"] == "full,advanced,intermediate,beginner,raw"
    assert "piano level easy left out" in caplog.text
    for level, data in sent.items():
        assert (tasks.job_dir("job") / variants.file_name(level)).read_bytes() == data
        assert client.get("/api/transcriptions/job/midi", params={"variant": level}).content == data


def test_an_older_endpoint_gets_its_levels_from_the_remote_worker_after_completion(
        worker, runpod, monkeypatch, ensemble_midi, expected_levels):
    client, settings = worker
    runpod.update(midi_base64=encoded(ensemble_midi))
    seen = []
    make = variants.make_levels

    def after_completion(midi_path, folder, job_id="", **kwargs):
        # the job is delivered with its raw result before the levels start
        seen.append((job()["status"], job()["variants"]))
        return make(midi_path, folder, job_id, **kwargs)

    monkeypatch.setattr(variants, "make_levels", after_completion)
    monkeypatch.setattr(tasks, "late_levels", variants.FallbackLevels(limit=1))
    queue(backend="remote")

    tasks.transcribe_remote_job.fn("job", URL)

    assert job()["status"] == "completed"
    assert wait_for(lambda: job().get("variants") == ",".join(EVERY))
    assert seen == [("completed", "raw")]
    for level in variants.LEVELS:
        assert client.get("/api/transcriptions/job/midi", params={"variant": level}).content == expected_levels[level]
    # its scratch folder is gone
    assert wait_for(lambda: not list((Path(settings.service.data_dir) / job_storage.WORK_FOLDER).glob("*")), 5)


def test_a_busy_fallback_delivers_the_raw_result_only(worker, runpod, monkeypatch, ensemble_midi, caplog):
    _, settings = worker
    runpod.update(midi_base64=encoded(ensemble_midi))
    busy = variants.FallbackLevels(limit=1)
    release = threading.Event()
    assert busy.submit("other", lambda: release.wait(10))
    monkeypatch.setattr(tasks, "late_levels", busy)
    queue(backend="remote")

    try:
        with caplog.at_level(logging.WARNING):
            tasks.transcribe_remote_job.fn("job", URL)
    finally:
        release.set()

    assert job()["status"] == "completed" and job()["variants"] == "raw"
    assert "Job job: no piano levels" in caplog.text
    assert not list((Path(settings.service.data_dir) / job_storage.WORK_FOLDER).glob("*"))


def test_a_runpod_piano_result_with_levels_is_stored_as_it_came(worker, runpod, monkeypatch):
    data = pedalled_piano()
    sent = dict(variants.distinct_levels(data))
    runpod.update(midi_base64=encoded(data), piano_levels_base64={level: encoded(m) for level, m in sent.items()})
    monkeypatch.setattr(variants, "make_levels", lambda *args, **kwargs: pytest.fail("the levels came with the result"))
    queue(ensemble=False, backend="remote")

    tasks.transcribe_remote_job.fn("job", URL)

    assert job()["status"] == "completed" and job()["variants"] == ",".join(EVERY)
    for level, midi in sent.items():
        assert (tasks.job_dir("job") / variants.file_name(level)).read_bytes() == midi


def test_an_older_endpoints_piano_result_gets_its_levels_after_completion(worker, runpod, monkeypatch):
    client, _ = worker
    data = pedalled_piano()
    runpod.update(midi_base64=encoded(data))
    monkeypatch.setattr(tasks, "late_levels", variants.FallbackLevels(limit=1))
    queue(ensemble=False, backend="remote")

    tasks.transcribe_remote_job.fn("job", URL)

    assert job()["status"] == "completed"
    assert wait_for(lambda: job().get("variants") == ",".join(EVERY))
    assert client.get("/api/transcriptions/job/midi").content == data


def test_late_levels_are_not_announced_for_a_job_that_changed(worker):
    tasks.redis.hset(tasks.job_key("job"), mapping={"status": "completed", "result": "other", "variants": "raw"})
    assert not tasks.add_variants("job", "mine", ["full"])
    assert job()["variants"] == "raw"
    assert tasks.add_variants("job", "other", ["full", "easy"])
    assert job()["variants"] == "full,easy,raw"


def test_a_hub_that_only_dispatches_makes_the_levels_of_runpod_results(worker, runpod, monkeypatch, ensemble_midi,
                                                                      expected_levels):
    """A hub without a GPU (WORKER_PROCESSOR=cpu) whose worker takes only dispatch: the levels need neither the
    transcription pipeline nor torch, and come from the remote actor itself."""
    from app.worker import supervisor

    assert supervisor.OFFERED["remote"] == ("dispatch",)
    monkeypatch.setitem(sys.modules, "app.transcription.pipeline", None)
    monkeypatch.setitem(sys.modules, "torch", None)
    monkeypatch.setattr(tasks, "late_levels", variants.FallbackLevels(limit=1))
    runpod.update(midi_base64=encoded(ensemble_midi))
    queue(backend="remote")

    tasks.transcribe_remote_job.fn("job", URL)

    assert job()["status"] == "completed"
    assert wait_for(lambda: job().get("variants") == ",".join(EVERY))
    assert (tasks.job_dir("job") / "piano-full.mid").read_bytes() == expected_levels["full"]


def test_the_handler_and_the_server_name_the_output_field_alike():
    handler = (Path(__file__).resolve().parent.parent / "runpod" / "handler.py").read_text(encoding="utf-8")
    assert "variants.LEVELS_FIELD" in handler and "variants.make_levels" in handler
    assert variants.LEVELS_FIELD == "piano_levels_base64"


# --- Only the levels that differ --------------------------------------------------------------------------------

from test_piano_levels import TPB, build  # noqa: E402


def melody_only() -> bytes:
    """One flute, 32 notes."""
    return build([(73, 0, [(i * TPB, TPB, 72 + (i % 5), 80) for i in range(32)])])


def melody_and_bass() -> bytes:
    return build([(73, 0, [(i * TPB, TPB, 72 + (i % 5), 80) for i in range(32)]),
                  (33, 1, [(i * 2 * TPB, 2 * TPB, 36 + (i % 3) * 2, 80) for i in range(16)])])


def drums_only() -> bytes:
    return build([(0, 9, [(i * TPB, 50, 36, 100) for i in range(16)])])


def empty() -> bytes:
    return build([])


@pytest.mark.parametrize("make", [melody_only, melody_and_bass, piano_solo, band])
def test_only_distinct_levels_with_notes_are_kept(make):
    data = make()
    every = piano_levels.all_levels(data)
    kept = dict(variants.distinct_levels(data))
    assert list(kept)[0] == "full"
    # each kept level as the module makes it, and each left out one the same as the next harder level kept
    previous = None
    for level in variants.LEVELS:
        notes = piano_levels.played_notes(every[level])
        if level in kept:
            assert kept[level] == every[level] and notes and notes != previous
            previous = notes
        else:
            assert notes == previous or not notes


def test_a_lone_melody_is_one_level():
    # the same 32 notes at every level, also the bytes of beginner and easy, intermediate and advanced
    every = piano_levels.all_levels(melody_only())
    assert len({piano_levels.played_notes(midi) for midi in every.values()}) == 1
    assert len(piano_levels.played_notes(every["full"])) == 32
    assert list(dict(variants.distinct_levels(melody_only()))) == ["full"]


def test_played_notes_ignore_pedal_and_velocity_but_not_hands():
    every = piano_levels.all_levels(band())
    full = piano_levels.played_notes(every["full"])
    assert {hand for *_, hand in full} == {0, 1}
    assert all(start < end for start, end, *_ in full)


@pytest.mark.parametrize("make", [drums_only, empty])
def test_nothing_pitched_gives_no_level(make):
    assert list(variants.distinct_levels(make())) == []


@pytest.mark.parametrize(("make", "listed"), [(melody_only, ["full", "raw"]), (drums_only, ["raw"]), (empty, ["raw"])])
def test_a_simple_jobs_variants_list_only_what_exists(worker, monkeypatch, make, listed):
    client, _ = worker
    data = make()
    install_pipeline(monkeypatch, data)
    queue()

    tasks.ensemble_job.fn("job", URL)

    assert job()["status"] == "completed"
    assert client.get("/api/transcriptions/job").json()["variants"] == listed
    present = sorted(path.name for path in tasks.job_dir("job").iterdir())
    assert present == sorted(variants.file_name(name) for name in listed)
    default = client.get("/api/transcriptions/job/midi")
    assert 'filename="ensemble-%s.mid"' % listed[0] in default.headers["content-disposition"]
    if listed == ["raw"]:
        assert default.content == data
    assert client.get("/api/transcriptions/job/midi", params={"variant": "easy"}).status_code == 404
