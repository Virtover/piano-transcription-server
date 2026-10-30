import os
import sys
import types
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

from app import job_storage, online_check, requirements, video_info
from app.config import validate_settings
from app.job_storage import PREFIX, S3JobStorage
from app.worker import tasks


class FakeS3:
    """The S3 calls job storage makes, in memory. Lists two keys a page, so
    paging is exercised."""

    PAGE = 2

    def __init__(self):
        self.objects: dict[str, dict] = {}

    def _missing(self, operation):
        return ClientError({"Error": {"Code": "404", "Message": "Not Found"}}, operation)

    def upload_file(self, filename, bucket, key, ExtraArgs=None):
        extra = ExtraArgs or {}
        self.objects[key] = {
            "body": Path(filename).read_bytes(),
            "metadata": extra.get("Metadata", {}),
            "content_type": extra.get("ContentType"),
        }

    def put_object(self, Bucket, Key, Body, Metadata=None):
        self.objects[Key] = {"body": Body, "metadata": Metadata or {}, "content_type": None}

    def download_file(self, bucket, key, filename):
        if key not in self.objects:
            raise self._missing("GetObject")
        Path(filename).write_bytes(self.objects[key]["body"])

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise self._missing("HeadObject")
        return {"Metadata": self.objects[Key]["metadata"]}

    def list_objects_v2(self, Bucket, Prefix, ContinuationToken=None):
        keys = sorted(key for key in self.objects if key.startswith(Prefix))
        start = int(ContinuationToken or 0)
        page = keys[start:start + self.PAGE]
        response = {"Contents": [{"Key": key} for key in page], "IsTruncated": start + self.PAGE < len(keys)}
        if response["IsTruncated"]:
            response["NextContinuationToken"] = str(start + self.PAGE)
        return response

    def delete_objects(self, Bucket, Delete):
        for item in Delete["Objects"]:
            self.objects.pop(item["Key"], None)

    def generate_presigned_url(self, operation, Params, ExpiresIn):
        return f"https://storage.example/{Params['Bucket']}/{Params['Key']}?expires={ExpiresIn}"


@pytest.fixture
def bucket(api_context):
    """JOB_STORAGE=s3 with the fake bucket."""
    _, settings, _ = api_context
    settings.jobs.storage = "s3"
    job_storage.reset()
    client = FakeS3()
    job_storage._s3 = S3JobStorage(client, "jobs-bucket")
    return client


def local_folders(settings) -> list[str]:
    data = Path(settings.service.data_dir)
    return sorted(
        str(path.relative_to(data))
        for folder in ("jobs", job_storage.WORK_FOLDER)
        if (data / folder).is_dir()
        for path in (data / folder).iterdir()
    )


def install_pipeline(monkeypatch, transcribe_source):
    module = types.ModuleType("app.transcription.pipeline")
    module.transcribe_source = transcribe_source
    monkeypatch.setitem(sys.modules, "app.transcription.pipeline", module)


def queue_job(job_id="job", **values):
    tasks.redis.hset(tasks.job_key(job_id), mapping={"status": "queued", "progress": "0.0", **values})


def store(client, job_id, name, body, mtime=None):
    client.objects[f"{PREFIX}{job_id}/{name}"] = {
        "body": body,
        "metadata": {"mtime": str(mtime)} if mtime is not None else {},
        "content_type": None,
    }


# --- The API keeps submissions in the bucket ------------------------------------

def test_an_upload_is_kept_in_the_bucket_and_nowhere_else(api_context, bucket, monkeypatch):
    client, settings, _ = api_context
    from app.api.routes import transcriptions

    monkeypatch.setattr(transcriptions, "probe_audio", lambda path, measure=True, max_seconds=None: 130.0)

    response = client.post("/api/transcriptions/upload", params={"filename": "Song.mp3"}, content=b"ID3-audio")

    assert response.status_code == 202
    job_id = response.json()["job_id"]
    assert bucket.objects[f"{PREFIX}{job_id}/{tasks.UPLOAD_FILE_NAME}"]["body"] == b"ID3-audio"
    assert local_folders(settings) == []


def test_a_failed_upload_leaves_nothing_in_the_bucket(api_context, bucket, monkeypatch):
    client, settings, _ = api_context
    from app.api.routes import transcriptions
    from app.media import UnsupportedMedia

    def refuse(path, measure=True, max_seconds=None):
        raise UnsupportedMedia("not audio")

    monkeypatch.setattr(transcriptions, "probe_audio", refuse)

    response = client.post("/api/transcriptions/upload", params={"filename": "x.mp3"}, content=b"junk")

    assert response.status_code == 422
    assert bucket.objects == {}
    assert local_folders(settings) == []


def test_a_link_keeps_its_video_info_with_the_time_it_was_checked(api_context, bucket):
    client, settings, _ = api_context

    response = client.post("/api/transcriptions", json={"source_url": "https://example.com/video"})

    assert response.status_code == 202
    stored = bucket.objects[f"{PREFIX}{response.json()['job_id']}/{video_info.VIDEO_INFO_FILE_NAME}"]
    assert float(stored["metadata"]["mtime"]) > 0
    assert local_folders(settings) == []


def test_storage_down_refuses_the_submission(api_context, bucket, monkeypatch):
    client, _, _ = api_context

    def unavailable(*args, **kwargs):
        raise ConnectionError("no route to storage")

    monkeypatch.setattr(bucket, "upload_file", unavailable)

    response = client.post("/api/transcriptions", json={"source_url": "https://example.com/video"})

    assert response.status_code == 503
    assert list(tasks.redis.scan_iter("transcription:*")) == []


# --- Workers fetch inputs and keep the result -------------------------------------

def test_a_worker_transcribes_from_the_bucket_and_keeps_the_result_there(api_context, bucket, monkeypatch):
    _, settings, _ = api_context
    store(bucket, "job", tasks.UPLOAD_FILE_NAME, b"ID3-audio")
    queue_job(source="upload", title="Song")
    seen = {}

    def transcribe_source(output_dir, upload_path, **kwargs):
        seen["folder"] = output_dir
        seen["upload"] = upload_path.read_bytes()
        midi = output_dir / "transcription.mid"
        midi.write_bytes(b"MThd-result")
        return midi

    install_pipeline(monkeypatch, transcribe_source)
    tasks.transcribe_job.fn("job", upload_title="Song")

    assert seen["upload"] == b"ID3-audio"
    # A scratch folder of this attempt's own, removed afterwards.
    assert seen["folder"].parent == Path(settings.service.data_dir) / job_storage.WORK_FOLDER
    assert local_folders(settings) == []
    stored = tasks.redis.hgetall(tasks.job_key("job"))
    assert stored["status"] == "completed"
    assert stored["result"] == f"s3:{PREFIX}job/transcription.mid"
    # The upload is gone; the result stays until the job expires.
    assert sorted(bucket.objects) == [f"{PREFIX}job/transcription.mid"]
    assert bucket.objects[f"{PREFIX}job/transcription.mid"]["content_type"] == "audio/midi"


def test_fetched_video_info_keeps_its_age(api_context, bucket, monkeypatch):
    checked_at = 1_700_000_000.0
    store(bucket, "job", video_info.VIDEO_INFO_FILE_NAME, b"{}", mtime=checked_at)
    queue_job(source_url="https://example.com/video")
    seen = {}

    def transcribe_source(output_dir, **kwargs):
        seen["mtime"] = os.stat(output_dir / video_info.VIDEO_INFO_FILE_NAME).st_mtime
        midi = output_dir / "transcription.mid"
        midi.write_bytes(b"MThd")
        return midi

    install_pipeline(monkeypatch, transcribe_source)
    tasks.transcribe_job.fn("job", "https://example.com/video")

    assert seen["mtime"] == checked_at


def test_a_failed_job_leaves_neither_inputs_nor_scratch(api_context, bucket, monkeypatch):
    _, settings, _ = api_context
    store(bucket, "job", tasks.UPLOAD_FILE_NAME, b"ID3-audio")
    queue_job(source="upload")

    def transcribe_source(**kwargs):
        raise RuntimeError("model crashed")

    install_pipeline(monkeypatch, transcribe_source)
    tasks.transcribe_job.fn("job", upload_title="Song")

    assert tasks.redis.hget(tasks.job_key("job"), "status") == "failed"
    assert bucket.objects == {}
    assert local_folders(settings) == []


def test_each_attempt_works_in_a_folder_of_its_own(bucket):
    storage = job_storage.storage()

    first, second = storage.workspace("job"), storage.workspace("job")

    assert first != second
    assert job_storage.workspace_job_id(first) == "job"


# --- Downloads -------------------------------------------------------------------

def test_the_result_is_a_signed_link_to_the_bucket(api_context, bucket):
    client, _, _ = api_context
    store(bucket, "job", "transcription.mid", b"MThd")
    tasks.redis.hset(tasks.job_key("job"), mapping={"status": "completed", "result": f"s3:{PREFIX}job/transcription.mid"})

    response = client.get("/api/transcriptions/job/midi", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == (
        f"https://storage.example/jobs-bucket/{PREFIX}job/transcription.mid?expires={job_storage.RESULT_LINK_SECONDS}"
    )
    assert response.headers["cache-control"] == "no-store"


def test_a_result_missing_from_the_bucket_is_gone(api_context, bucket):
    client, _, _ = api_context
    tasks.redis.hset(tasks.job_key("job"), mapping={"status": "completed", "result": f"s3:{PREFIX}job/transcription.mid"})

    assert client.get("/api/transcriptions/job/midi", follow_redirects=False).status_code == 404


def test_results_stored_before_switching_back_to_local_stay_downloadable(api_context, bucket):
    client, settings, _ = api_context
    store(bucket, "job", "transcription.mid", b"MThd")
    tasks.redis.hset(tasks.job_key("job"), mapping={"status": "completed", "result": f"s3:{PREFIX}job/transcription.mid"})
    settings.jobs.storage = "local"
    job_storage._storage = None

    assert client.get("/api/transcriptions/job/midi", follow_redirects=False).status_code == 307


# --- Cleanup and deletion ----------------------------------------------------------

def test_cleanup_deletes_only_expired_jobs_from_the_bucket(bucket):
    for job_id in ("expired-a", "expired-b", "kept"):
        store(bucket, job_id, "transcription.mid", b"MThd")
        store(bucket, job_id, tasks.UPLOAD_FILE_NAME, b"x")
    tasks.redis.hset(tasks.job_key("kept"), "status", "completed")

    assert tasks.cleanup_stored_jobs() == 2

    assert sorted(bucket.objects) == [f"{PREFIX}kept/transcription.mid", f"{PREFIX}kept/{tasks.UPLOAD_FILE_NAME}"]


def test_cleanup_of_the_bucket_does_nothing_with_local_storage(api_context):
    assert tasks.cleanup_stored_jobs() == 0


def test_cleanup_removes_scratch_folders_a_crashed_worker_left(api_context, bucket):
    _, settings, _ = api_context
    work = Path(settings.service.data_dir) / job_storage.WORK_FOLDER
    (work / "gone.0a1b2c").mkdir(parents=True)
    (work / "running.3d4e5f").mkdir()
    tasks.redis.hset(tasks.job_key("running"), "status", "processing")

    tasks.cleanup_expired_jobs()

    assert [path.name for path in work.iterdir()] == ["running.3d4e5f"]


def test_deleting_an_account_deletes_its_files_from_the_bucket(bucket):
    store(bucket, "mine", "transcription.mid", b"MThd")
    store(bucket, "theirs", "transcription.mid", b"MThd")
    tasks.redis.hset(tasks.job_key("mine"), "user_id", "user")
    tasks.redis.hset(tasks.job_key("theirs"), "user_id", "other")

    tasks.delete_user_jobs("user")

    assert list(bucket.objects) == [f"{PREFIX}theirs/transcription.mid"]


def test_restart_fails_a_completed_job_whose_result_left_the_bucket(bucket):
    tasks.redis.hset(tasks.job_key("job"), mapping={"status": "completed", "result": f"s3:{PREFIX}job/transcription.mid"})

    assert not tasks.check_completed_result("job", tasks.redis.hgetall(tasks.job_key("job")))
    assert tasks.redis.hget(tasks.job_key("job"), "error_code") == tasks.INVALID_RESULT


def test_admin_lists_a_jobs_stored_files(bucket):
    store(bucket, "job", "transcription.mid", b"MThd")

    assert job_storage.storage().names("job") == ["transcription.mid"]
    assert job_storage.storage().names("other") is None


# --- Configuration -------------------------------------------------------------------

def test_s3_job_storage_needs_the_bucket_and_its_keys(default_settings):
    default_settings.jobs.storage = "s3"

    problems = validate_settings(default_settings, "worker")

    assert any(problem.startswith("JOB_STORAGE=s3 needs object storage: set STORAGE_BUCKET") for problem in problems)

    default_settings.storage.bucket = "jobs"
    problems = validate_settings(default_settings, "worker")

    assert "STORAGE_ENDPOINT is required when STORAGE_BUCKET is set" in problems
    assert any("STORAGE_BUCKET needs the secret file" in problem for problem in problems)


def test_local_job_storage_needs_no_bucket(default_settings):
    assert not any("JOB_STORAGE" in problem for problem in validate_settings(default_settings, "worker"))


@pytest.mark.parametrize("storage, retention, lifetime", [
    ("local", 3600, "1 day"),
    ("s3", 3600, "2 days"),
    ("s3", 3 * 86400, "4 days"),
])
def test_automatic_deletion_waits_for_results_to_expire(default_settings, storage, retention, lifetime):
    default_settings.jobs.storage = storage
    default_settings.limits.result_retention = retention

    assert requirements.storage_object_lifetime(default_settings) == lifetime


def test_requirements_list_storage_for_job_files_without_outsourcing(default_settings):
    default_settings.jobs.storage = "s3"

    [storage] = requirements.storage_sections(default_settings)

    assert storage.enabled
    assert storage.note == "enabled (JOB_STORAGE=s3)"
    assert "list, read, write, and delete" in next(item.value for item in storage.requirements if item.item == "Credentials")
    assert any(item.item == "Shared with" for item in storage.requirements)


def test_requirements_for_outsourcing_alone_allow_sharing(default_settings):
    default_settings.outsource.provider = "runpod"

    [storage] = requirements.storage_sections(default_settings)

    assert not any(item.item == "Shared with" for item in storage.requirements)


def test_online_check_of_job_storage(default_settings):
    client = FakeS3()

    result = online_check.check_job_storage(default_settings, lambda config: S3JobStorage(client, "jobs-bucket"))

    assert result.status == "ok"
    assert client.objects == {}


def test_online_check_reports_storage_without_metadata(default_settings, monkeypatch):
    client = FakeS3()
    monkeypatch.setattr(client, "head_object", lambda Bucket, Key: {"Metadata": {}})

    result = online_check.check_job_storage(default_settings, lambda config: S3JobStorage(client, "jobs-bucket"))

    assert result.status == "fail"
    assert "metadata" in result.detail
