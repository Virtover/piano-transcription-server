from urllib.parse import parse_qs, urlsplit

import pytest
import requests

from app.outsource import handler_input
from app.outsource.handler_input import InvalidInput, download, validate_input
from app.outsource.provider import RemoteFailed, get_provider
from app.outsource.runpod import RunPodProvider
from app.outsource.storage import S3Storage


# --- RunPod client ----------------------------------------------------------

class Response:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload

    @property
    def ok(self):
        return 200 <= self.status_code < 300

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class Session:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def provider(*responses, api_base="https://api.runpod.ai/v2"):
    session = Session(*responses)
    return RunPodProvider(api_base, "endpoint-1", "rp-secret-key", session=session), session


def test_submit_posts_input_to_run_with_bearer_key():
    client, session = provider(Response(200, {"id": "job-9", "status": "IN_QUEUE"}))

    assert client.submit({"audio_url": "https://storage/x", "job_id": "abc"}) == "job-9"

    method, url, kwargs = session.calls[0]
    assert (method, url) == ("POST", "https://api.runpod.ai/v2/endpoint-1/run")
    assert kwargs["json"] == {"input": {"audio_url": "https://storage/x", "job_id": "abc"}}
    assert kwargs["headers"] == {"Authorization": "Bearer rp-secret-key"}


def test_local_api_server_is_used_without_endpoint_id():
    client, session = provider(Response(200, {"id": "test-1"}), api_base="http://localhost:8010/")

    client.submit({})

    assert session.calls[0][1] == "http://localhost:8010/run"


@pytest.mark.parametrize(
    ("payload", "state", "progress", "error"),
    [
        ({"status": "IN_QUEUE"}, "queued", None, None),
        ({"status": "IN_PROGRESS"}, "running", None, None),
        ({"status": "IN_PROGRESS", "output": {"progress": 0.42}}, "running", 0.42, None),
        ({"status": "IN_PROGRESS", "output": "warming up"}, "running", None, None),
        ({"status": "FAILED", "error": "handler crashed"}, "failed", None, "handler crashed"),
        ({"status": "TIMED_OUT"}, "failed", None, "TIMED_OUT"),
        ({"status": "CANCELLED"}, "cancelled", None, None),
        ({"status": "COMPLETED", "output": {"error": "The audio URL is not on the configured storage host"}}, "failed", None, "The audio URL is not on the configured storage host"),
    ],
)
def test_status_mapping(payload, state, progress, error):
    client, session = provider(Response(200, payload))

    status = client.status("job-9", timeout=30)

    assert (status.state, status.progress, status.error) == (state, progress, error)
    assert session.calls[0][:2] == ("GET", "https://api.runpod.ai/v2/endpoint-1/status/job-9")


def test_completed_status_returns_output():
    output = {"midi_base64": "TVRoZA==", "timing": {}}
    client, _ = provider(Response(200, {"status": "COMPLETED", "output": output}))

    assert client.status("job-9", timeout=30).output == output


def test_status_falls_back_to_post_for_local_server():
    client, session = provider(Response(405), Response(200, {"status": "COMPLETED", "output": {"midi_base64": "x"}}), api_base="http://localhost:8010")

    assert client.status("test-1", timeout=900).state == "completed"
    assert [call[0] for call in session.calls] == ["GET", "POST"]
    assert session.calls[1][2]["timeout"] == 900


@pytest.mark.parametrize(
    "response",
    [Response(500), Response(401), Response(200, None), Response(200, {"status": "WEIRD"}), requests.ConnectionError("down")],
)
def test_errors_raise_remote_failed_without_the_key(response):
    client, _ = provider(response)

    with pytest.raises(RemoteFailed) as error:
        client.status("job-9", timeout=30)
    assert "rp-secret-key" not in str(error.value)


def test_submit_without_id_fails():
    client, _ = provider(Response(200, {"status": "IN_QUEUE"}))

    with pytest.raises(RemoteFailed, match="job ID"):
        client.submit({})


def test_cancel_posts_and_never_raises():
    client, session = provider(Response(404), requests.ConnectionError("down"))

    client.cancel("job-9")
    client.cancel("job-9")

    assert session.calls[0][:2] == ("POST", "https://api.runpod.ai/v2/endpoint-1/cancel/job-9")


def test_provider_from_settings(default_settings):
    default_settings.outsource.provider = "runpod"
    with pytest.raises(RemoteFailed, match="not configured"):
        get_provider()

    default_settings.runpod.endpoint_id = "endpoint-1"
    default_settings.secrets.runpod_api_key = "key"
    assert get_provider().base_url == "https://api.runpod.ai/v2/endpoint-1"

    default_settings.outsource.provider = "none"
    with pytest.raises(RemoteFailed, match="disabled"):
        get_provider()


# --- Storage ----------------------------------------------------------------

class S3Client:
    def __init__(self, fail=False):
        self.fail = fail
        self.uploads = []
        self.deleted = []

    def upload_file(self, path, bucket, key):
        if self.fail:
            raise ConnectionError("unreachable")
        self.uploads.append((path, bucket, key))

    def delete_object(self, Bucket, Key):
        if self.fail:
            raise ConnectionError("unreachable")
        self.deleted.append((Bucket, Key))


def test_upload_uses_random_keys(tmp_path):
    client = S3Client()
    storage = S3Storage(client, "bucket")
    path = tmp_path / "audio.flac"
    path.write_bytes(b"fLaC")

    first, second = storage.upload(path), storage.upload(path)

    assert first != second
    assert first.startswith("audio/") and first.endswith(".flac")
    assert client.uploads[0][1:] == ("bucket", first)


def test_storage_errors(tmp_path):
    storage = S3Storage(S3Client(fail=True), "bucket")

    with pytest.raises(RemoteFailed, match="Could not upload"):
        storage.upload(tmp_path / "audio.flac")
    storage.delete("audio/x.flac")


def test_presigned_urls_are_path_style_on_the_endpoint(default_settings):
    default_settings.outsource.storage_endpoint = "https://account.eu.r2.cloudflarestorage.com"
    default_settings.outsource.storage_bucket = "piano-audio"
    default_settings.secrets.outsource_storage_access_key = "access"
    default_settings.secrets.outsource_storage_secret_key = "secret"

    url = S3Storage.from_settings().presigned_url("audio/abc.flac", 600)

    parts = urlsplit(url)
    assert (parts.scheme, parts.netloc, parts.path) == ("https", "account.eu.r2.cloudflarestorage.com", "/piano-audio/audio/abc.flac")
    assert parse_qs(parts.query)["X-Amz-Expires"] == ["600"]
    assert validate_input({"audio_url": url, "job_id": "job-1"}, default_settings.outsource.storage_endpoint)[0] == url


def test_storage_requires_configuration(default_settings):
    with pytest.raises(RemoteFailed, match="not configured"):
        S3Storage.from_settings()


# --- Handler input ----------------------------------------------------------

STORAGE = "https://account.eu.r2.cloudflarestorage.com"


@pytest.mark.parametrize(
    ("job_input", "message"),
    [
        (None, "must be an object"),
        ({"job_id": "a"}, "audio_url is required"),
        ({"audio_url": STORAGE + "/b/k"}, "job_id"),
        ({"audio_url": STORAGE + "/b/k", "job_id": "../../etc"}, "job_id"),
        ({"audio_url": "https://evil.example/b/k", "job_id": "a"}, "not on the configured storage host"),
        ({"audio_url": "http://account.eu.r2.cloudflarestorage.com/b/k", "job_id": "a"}, "not on the configured storage host"),
        ({"audio_url": "https://account.eu.r2.cloudflarestorage.com:8443/b/k", "job_id": "a"}, "not on the configured storage host"),
        ({"audio_url": "https://account.eu.r2.cloudflarestorage.com.evil.example/b/k", "job_id": "a"}, "not on the configured storage host"),
        ({"audio_url": "https://user:pw@account.eu.r2.cloudflarestorage.com/b/k", "job_id": "a"}, "credentials"),
        ({"audio_url": "file:///etc/passwd", "job_id": "a"}, "not an HTTP"),
        ({"audio_url": "https://[::1", "job_id": "a"}, "malformed|not an HTTP"),
    ],
)
def test_invalid_handler_input(job_input, message):
    with pytest.raises(InvalidInput, match=message) as error:
        validate_input(job_input, STORAGE)
    assert "evil.example" not in str(error.value)


def test_valid_handler_input():
    url = STORAGE + "/bucket/audio/x.flac?X-Amz-Signature=abc"

    assert validate_input({"audio_url": url, "job_id": "3f2c-11"}, STORAGE) == (url, "3f2c-11")
    assert validate_input({"audio_url": "http://minio:9000/b/k", "job_id": "a"}, "http://minio:9000")[0] == "http://minio:9000/b/k"


def test_handler_rejects_everything_without_storage_endpoint():
    with pytest.raises(InvalidInput, match="not configured"):
        validate_input({"audio_url": STORAGE + "/b/k", "job_id": "a"}, None)


class DownloadResponse:
    def __init__(self, chunks, status_code=200, headers=None):
        self.chunks = chunks
        self.status_code = status_code
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_content(self, size):
        yield from self.chunks


class DownloadSession:
    def __init__(self, response):
        self.response = response
        self.kwargs = None

    def get(self, url, **kwargs):
        self.kwargs = kwargs
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def test_download_streams_to_file(tmp_path):
    session = DownloadSession(DownloadResponse([b"fLaC", b"data"]))
    destination = tmp_path / "audio.flac"

    assert download("https://storage/x", destination, 100, session) == 8
    assert destination.read_bytes() == b"fLaCdata"
    assert session.kwargs["allow_redirects"] is False


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (DownloadResponse([b"x" * 60, b"x" * 60]), "larger than OUTSOURCE_MAX_AUDIO_MB"),
        (DownloadResponse([b"x"], headers={"Content-Length": "1000"}), "larger than OUTSOURCE_MAX_AUDIO_MB"),
        (DownloadResponse([], status_code=302), "HTTP 302"),
        (DownloadResponse([], status_code=403), "HTTP 403"),
        (DownloadResponse([]), "empty"),
        (requests.ConnectionError("https://storage/x?X-Amz-Signature=secret"), "ConnectionError"),
    ],
)
def test_download_limits_and_errors(tmp_path, response, message):
    with pytest.raises(InvalidInput, match=message) as error:
        download("https://storage/x", tmp_path / "audio.flac", 100, DownloadSession(response))
    assert "secret" not in str(error.value)


def test_download_timeout_is_set():
    assert handler_input.DOWNLOAD_TIMEOUT > 0
