import httplib2
import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from googleapiclient.errors import HttpError

from app import config as config_module
from app import online_check
from app.online_check import (
    Result,
    TEST_BODY,
    check_client_ids,
    check_google_play,
    check_runpod,
    check_storage,
    format_results,
)
from app.outsource.runpod import RunPodProvider


# --- Fakes ------------------------------------------------------------------

class FakeS3Client:
    def __init__(self, put_error=None, delete_error=None):
        self.objects = {}
        self.put_error = put_error
        self.delete_error = delete_error

    def put_object(self, Bucket, Key, Body):
        if self.put_error:
            raise self.put_error
        self.objects[Key] = Body

    def delete_object(self, Bucket, Key):
        if self.delete_error:
            raise self.delete_error
        self.objects.pop(Key, None)


class FakeStorage:
    bucket = "audio"

    def __init__(self, client):
        self.client = client

    def presigned_url(self, key, expires_seconds):
        return f"https://storage.example/{key}?signature=secret"


class HttpResponse:
    def __init__(self, status_code=200, content=TEST_BODY, payload=None):
        self.status_code = status_code
        self.content = content
        self._payload = payload

    @property
    def ok(self):
        return 200 <= self.status_code < 300

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class Http:
    def __init__(self, response):
        self.response = response

    def get(self, url, timeout):
        return self.response


class Session:
    def __init__(self, *responses):
        self.responses = list(responses)

    def request(self, method, url, **kwargs):
        return self.responses.pop(0)


def client_error(code):
    return ClientError({"Error": {"Code": code, "Message": "secret details"}}, "PutObject")


def storage_check(client, http=None):
    return check_storage(object(), lambda config: FakeStorage(client), http or Http(HttpResponse()))


def runpod(*responses):
    return RunPodProvider("https://api.runpod.ai/v2", "endpoint-1", "key", session=Session(*responses))


class Voided:
    def __init__(self, error=None):
        self.error = error
        self.arguments = None

    def voidedpurchases(self):
        return self

    def list(self, **arguments):
        self.arguments = arguments
        return self

    def execute(self):
        if self.error:
            raise self.error
        return {"voidedPurchases": []}


class Products:
    def __init__(self, pages=None, error=None, voided_error=None):
        self.pages = list(pages or [])
        self.error = error
        self.voided = Voided(voided_error)

    def purchases(self):
        return self.voided

    def monetization(self):
        return self

    def onetimeproducts(self):
        return self

    def list(self, **arguments):
        return self

    def execute(self):
        if self.error:
            raise self.error
        return self.pages.pop(0)


def play_error(status):
    return HttpError(httplib2.Response({"status": status}), b"{}")


@pytest.fixture
def settings(default_settings):
    default_settings.billing.provider = "google_play"
    default_settings.billing.products = {"minutes_60": 60}
    default_settings.google_play.package_name = "com.example.app"
    return default_settings


# --- Storage ----------------------------------------------------------------

def test_storage_check_uploads_downloads_and_deletes():
    client = FakeS3Client()

    result = storage_check(client)

    assert result.status == "ok"
    assert client.objects == {}


@pytest.mark.parametrize(
    "code, hint",
    [
        ("InvalidAccessKeyId", "access key ID is wrong"),
        ("SignatureDoesNotMatch", "secret access key"),
        ("AccessDenied", "Object Read & Write"),
        ("NoSuchBucket", ".eu."),
    ],
)
def test_storage_upload_errors_explain_the_fix(code, hint):
    result = storage_check(FakeS3Client(put_error=client_error(code)))

    assert result.status == "fail"
    assert code in result.detail
    assert hint in result.hint
    assert "secret details" not in result.detail + result.hint


def test_unreachable_storage_endpoint():
    result = storage_check(FakeS3Client(put_error=EndpointConnectionError(endpoint_url="https://x")))

    assert result.status == "fail"
    assert "OUTSOURCE_STORAGE_ENDPOINT" in result.hint


def test_rejected_signed_download_suggests_ip_filtering_and_still_deletes():
    client = FakeS3Client()

    result = storage_check(client, Http(HttpResponse(403, b"denied")))

    assert result.status == "fail"
    assert "IP address filtering" in result.hint
    assert "signature" not in result.detail
    assert client.objects == {}


def test_failed_delete_is_reported():
    result = storage_check(FakeS3Client(delete_error=client_error("AccessDenied")))

    assert result.status == "fail"
    assert result.detail.startswith("Delete")


# --- RunPod -----------------------------------------------------------------

GOOD_SETTINGS = {
    "template": {"imageName": "user/piano-transcription-runpod:4", "env": {}},
    "env": {"OUTSOURCE_STORAGE_ENDPOINT": "https://storage.example"},
    "minCudaVersion": "12.8",
    "executionTimeoutMs": 20 * 60_000,
    "workersMin": 0,
    "workersMax": 2,
    "scalerType": "REQUEST_COUNT",
    "scalerValue": 12,
    "idleTimeout": 5,
}


@pytest.fixture
def outsourcing(default_settings):
    default_settings.outsource.storage_endpoint = "https://storage.example"
    default_settings.outsource.max_concurrency = 4
    return default_settings


def endpoint_check(config, **changes):
    settings = {**GOOD_SETTINGS, **changes}
    provider = runpod(HttpResponse(payload={"workers": {"idle": 1}}), HttpResponse(payload=settings))
    return check_runpod(config, lambda config: provider)


def test_runpod_health_and_matching_settings(outsourcing):
    results = endpoint_check(outsourcing)

    assert [result.status for result in results] == ["ok", "ok", "ok"]
    assert "1 idle" in results[0].detail
    assert "user/piano-transcription-runpod:4" in results[1].detail


def test_endpoint_settings_are_read_with_the_template(outsourcing):
    session = Session(HttpResponse(payload={"workers": {}}), HttpResponse(payload=GOOD_SETTINGS))
    calls = []
    original = session.request
    session.request = lambda method, url, **kwargs: calls.append((url, kwargs.get("params"))) or original(method, url, **kwargs)
    provider = RunPodProvider("https://api.runpod.ai/v2", "endpoint-1", "key", session=session)

    check_runpod(outsourcing, lambda config: provider)

    assert calls[1] == ("https://rest.runpod.io/v1/endpoints/endpoint-1", {"includeTemplate": "true"})


@pytest.mark.parametrize("status, hint", [(401, "API key is wrong"), (403, "no access"), (404, "RUNPOD_ENDPOINT_ID")])
def test_runpod_errors_explain_the_fix(default_settings, status, hint):
    [result] = check_runpod(default_settings, lambda config: runpod(HttpResponse(status)))

    assert result.status == "fail"
    assert hint in result.hint


@pytest.mark.parametrize(
    "changes, status, text",
    [
        ({"env": {}}, "fail", "is missing"),
        ({"env": {"OUTSOURCE_STORAGE_ENDPOINT": "https://other.example"}}, "fail", "differs"),
        ({"minCudaVersion": None, "allowedCudaVersions": []}, "warn", "older than 12.8"),
        ({"minCudaVersion": None, "allowedCudaVersions": ["12.6", "12.8"]}, "warn", "older than 12.8"),
        ({"executionTimeoutMs": 10 * 60_000}, "warn", "needs at least 20 min"),
        ({"workersMin": 1}, "warn", "always on"),
        ({"workersMax": 0}, "warn", "fewer than OUTSOURCE_MAX_CONCURRENCY"),
        ({"scalerType": "QUEUE_DELAY"}, "warn", "queue delay"),
        ({"idleTimeout": 120}, "warn", "idle timeout 120 s"),
    ],
)
def test_endpoint_settings_mismatches(outsourcing, changes, status, text):
    results = endpoint_check(outsourcing, **changes)

    [problem] = [result for result in results if result.status != "ok"]
    assert problem.status == status
    assert text in problem.detail
    assert problem.hint


def test_new_cuda_versions_only_are_accepted(outsourcing):
    results = endpoint_check(outsourcing, minCudaVersion=None, allowedCudaVersions=["12.8", "12.9", "13.0"])

    assert all(result.status == "ok" for result in results)


def test_execution_timeout_requirement_follows_outsource_timeout(outsourcing):
    outsourcing.outsource.timeout = "30m"

    results = endpoint_check(outsourcing)

    assert any("needs at least 35 min" in result.detail for result in results)


def test_auto_concurrency_reports_max_workers(outsourcing):
    outsourcing.outsource.max_concurrency = None

    results = endpoint_check(outsourcing, workersMax=3)

    assert any(result.detail == "max workers 3 (OUTSOURCE_MAX_CONCURRENCY=auto)" for result in results)


def test_unreadable_settings_warn_with_a_hint(outsourcing):
    provider = runpod(HttpResponse(payload={"workers": {}}), HttpResponse(403))

    results = check_runpod(outsourcing, lambda config: provider)

    assert [result.status for result in results] == ["ok", "warn"]
    assert "read access" in results[1].hint


# --- Google -----------------------------------------------------------------

def test_well_formed_client_ids_pass(settings):
    settings.auth.client_ids = ["123-abc.apps.googleusercontent.com", "456-def.apps.googleusercontent.com"]

    assert check_client_ids(settings).status == "ok"


def test_malformed_client_id_fails(settings):
    settings.auth.client_ids = ["123-abc.apps.googleusercontent.com", "GOCSPX-secret"]

    result = check_client_ids(settings)

    assert result.status == "fail"
    assert "1 of 2" in result.detail


def test_google_play_products_match(settings):
    service = Products([{"oneTimeProducts": [{"productId": "minutes_60"}]}])

    result = check_google_play(settings, lambda config: service)

    assert result.status == "ok"


def test_google_play_reads_every_page(settings):
    settings.billing.products = {"minutes_60": 60, "minutes_300": 300}
    service = Products(
        [
            {"oneTimeProducts": [{"productId": "minutes_60"}], "nextPageToken": "next"},
            {"oneTimeProducts": [{"productId": "minutes_300"}]},
        ]
    )

    assert check_google_play(settings, lambda config: service).status == "ok"


def test_google_play_warns_about_missing_and_unconfigured_products(settings):
    missing = check_google_play(settings, lambda config: Products([{"oneTimeProducts": []}]))
    unconfigured = check_google_play(
        settings,
        lambda config: Products([{"oneTimeProducts": [{"productId": "minutes_60"}, {"productId": "extra"}]}]),
    )

    assert missing.status == "warn" and "minutes_60" in missing.detail
    assert unconfigured.status == "warn" and "extra" in unconfigured.detail


@pytest.mark.parametrize("status, hint", [(401, "new JSON key"), (403, "24 hours"), (404, "GOOGLE_PLAY_PACKAGE_NAME")])
def test_google_play_errors_explain_the_fix(settings, status, hint):
    result = check_google_play(settings, lambda config: Products(error=play_error(status)))

    assert result.status == "fail"
    assert hint in result.hint


def test_google_play_reads_voided_purchases(settings):
    service = Products([{"oneTimeProducts": [{"productId": "minutes_60"}]}])

    assert check_google_play(settings, lambda config: service).status == "ok"
    assert service.voided.arguments == {"packageName": "com.example.app", "maxResults": 1}


@pytest.mark.parametrize("error, shown", [(play_error(403), "HTTP 403"), (TimeoutError(), "TimeoutError")])
def test_google_play_without_access_to_voided_purchases_fails(settings, error, shown):
    service = Products([{"oneTimeProducts": [{"productId": "minutes_60"}]}], voided_error=error)

    result = check_google_play(settings, lambda config: service)

    assert result.status == "fail"
    assert f"Reading voided purchases: {shown}" == result.detail
    assert "refunded minutes are never taken back" in result.hint


# --- Running ----------------------------------------------------------------

def test_disabled_services_are_skipped(default_settings):
    results = online_check.run_checks(default_settings)

    assert [result.status for result in results] == ["skip", "skip"]


def test_format_puts_hints_under_their_result():
    lines = format_results([Result("Storage", "fail", "Upload: AccessDenied", "Fix it."), Result("RunPod", "ok", "fine")])

    assert lines[0].startswith("FAIL  Storage")
    assert lines[1].strip() == "-> Fix it."
    assert lines[2].startswith("OK    RunPod")


def test_cli_online_failure_sets_exit_code(tmp_path, monkeypatch, capsys):
    env_file = tmp_path / ".env"
    env_file.write_text("")
    monkeypatch.setattr(online_check, "run_checks", lambda config: [Result("Storage", "fail", "Upload: AccessDenied")])

    assert config_module.main(["check", "--env-file", str(env_file), "--secrets-dir", str(tmp_path), "--online"]) == 1
    assert "FAIL  Storage" in capsys.readouterr().out


def test_cli_without_online_does_not_contact_services(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("")
    monkeypatch.setattr(online_check, "run_checks", lambda config: pytest.fail("contacted services"))

    assert config_module.main(["check", "--env-file", str(env_file), "--secrets-dir", str(tmp_path)]) == 0


def test_read_key_is_used_only_for_the_management_api(outsourcing):
    session = Session(HttpResponse(payload={"workers": {}}), HttpResponse(payload=GOOD_SETTINGS))
    calls = []
    original = session.request
    session.request = lambda method, url, **kwargs: calls.append((url, kwargs["headers"])) or original(method, url, **kwargs)
    provider = RunPodProvider("https://api.runpod.ai/v2", "endpoint-1", "run-key", session=session, read_api_key="read-key")

    check_runpod(outsourcing, lambda config: provider)

    assert calls[0] == ("https://api.runpod.ai/v2/endpoint-1/health", {"Authorization": "Bearer run-key"})
    assert calls[1] == ("https://rest.runpod.io/v1/endpoints/endpoint-1", {"Authorization": "Bearer read-key"})


def test_without_read_key_the_main_key_reads_settings():
    provider = RunPodProvider("https://api.runpod.ai/v2", "endpoint-1", "run-key", session=Session())

    assert not provider.has_read_key
    assert provider._read_headers == {"Authorization": "Bearer run-key"}


def test_unreadable_settings_hint_suggests_the_read_key(outsourcing):
    provider = runpod(HttpResponse(payload={"workers": {}}), HttpResponse(401))

    [_, warning] = check_runpod(outsourcing, lambda config: provider)

    assert "secrets/runpod_read_api_key" in warning.hint
    assert "Jobs run either way" in warning.hint


def test_rejected_read_key_says_so(outsourcing):
    provider = RunPodProvider(
        "https://api.runpod.ai/v2", "endpoint-1", "run-key",
        session=Session(HttpResponse(payload={"workers": {}}), HttpResponse(401)), read_api_key="read-key",
    )

    [_, warning] = check_runpod(outsourcing, lambda config: provider)

    assert "runpod_read_api_key was rejected" in warning.hint


def test_read_key_is_loaded_from_its_secret_file(tmp_path):
    from app.config import load_settings

    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "runpod_api_key").write_text("run-key")
    (secrets / "runpod_read_api_key").write_text("read-key")
    (tmp_path / ".env").write_text("RUNPOD_ENDPOINT_ID=endpoint-1\n")
    config = load_settings(str(tmp_path / ".env"), str(secrets))

    provider = RunPodProvider.from_settings(config)

    assert provider.has_read_key
    assert provider._read_headers == {"Authorization": "Bearer read-key"}
    assert provider._headers == {"Authorization": "Bearer run-key"}
