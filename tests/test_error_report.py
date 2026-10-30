import json
import logging

import pytest

from app import error_report, online_check
from app.config import ConfigError, load_settings, validate_settings


URL = "https://pianoweave.example/api/server-errors"


def load(tmp_path, text="", token=None):
    env_file = tmp_path / ".env"
    env_file.write_text(text, encoding="utf-8")
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir(exist_ok=True)
    if token is not None:
        (secrets_dir / "error_report_token").write_text(token, encoding="utf-8")
    return load_settings(str(env_file), str(secrets_dir))


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def reporter(clock=None):
    sent = []

    def send(url, token, report):
        sent.append((url, token, report))
        return 200

    return error_report.Reporter(URL, "token", "worker", send=send, clock=clock or Clock(), background=False), sent


@pytest.fixture
def installed(monkeypatch):
    """A reporter installed like at startup, removed again after the test."""
    instance, sent = reporter()
    handler = error_report.ReportHandler(instance)
    logging.getLogger().addHandler(handler)
    monkeypatch.setattr(error_report, "_reporter", instance)
    yield instance, sent
    logging.getLogger().removeHandler(handler)


# --- Configuration ------------------------------------------------------------

def test_reports_are_off_by_default(tmp_path):
    config = load(tmp_path)

    assert config.error_report.url is None
    assert validate_settings(config, "all") == []
    assert error_report.install("api", config) is False


def test_url_needs_the_token(tmp_path):
    [problem] = validate_settings(load(tmp_path, f"ERROR_REPORT_URL={URL}\n"), "api")

    assert "error_report_token" in problem
    assert validate_settings(load(tmp_path, f"ERROR_REPORT_URL={URL}\n", token="secret"), "api") == []


def test_cleanup_does_not_need_the_token(tmp_path):
    assert validate_settings(load(tmp_path, f"ERROR_REPORT_URL={URL}\n"), "cleanup") == []


def test_url_must_be_a_web_address(tmp_path):
    with pytest.raises(ConfigError, match="ERROR_REPORT_URL"):
        load(tmp_path, "ERROR_REPORT_URL=pianoweave.example/api/server-errors\n")


# --- Reports ------------------------------------------------------------------

def test_logged_exception_is_reported_with_its_traceback(installed):
    instance, sent = installed
    try:
        raise RuntimeError("database is locked")
    except RuntimeError:
        logging.getLogger("app.billing").exception("Could not back up billing database")
    instance.flush()

    [(url, token, report)] = sent
    assert (url, token) == (URL, "token")
    assert report["service"] == "worker"
    assert report["type"] == "RuntimeError"
    assert report["message"] == "Could not back up billing database: database is locked"
    assert "Traceback" in report["traceback"]
    assert report["context"]["Logger"] == "app.billing"
    assert report["context"]["Location"].startswith("test_error_report.py:")


def test_warnings_are_not_reported(installed):
    instance, sent = installed
    logging.getLogger("app.worker.tasks").warning("Job %s failed", "job")
    instance.flush()

    assert sent == []


def test_repeats_are_sent_once_a_minute():
    clock = Clock()
    instance, sent = reporter(clock)
    report = {"type": "RuntimeError", "message": "boom", "context": {"Location": "a.py:1"}}

    assert instance.submit(report) is True
    assert instance.submit(report) is False
    clock.now += error_report.REPEAT_INTERVAL
    assert instance.submit(report) is True
    instance.flush()

    assert len(sent) == 2


def test_floods_are_limited():
    instance, _ = reporter()
    accepted = [
        instance.submit({"type": "RuntimeError", "message": f"error {number}"})
        for number in range(error_report.MAX_REPORTS_PER_MINUTE + 5)
    ]

    assert accepted.count(True) == error_report.MAX_REPORTS_PER_MINUTE


def test_failed_delivery_is_printed_not_logged(installed, capsys):
    instance, _ = installed

    def broken(url, token, report):
        raise ConnectionError("no route")

    instance.send = broken
    instance.submit({"type": "RuntimeError", "message": "boom"})
    instance.flush()

    assert "Could not send an error report: ConnectionError" in capsys.readouterr().err


def test_handled_exception_can_be_reported(installed):
    instance, sent = installed
    try:
        raise MemoryError("CUDA out of memory")
    except MemoryError as error:
        assert error_report.report_exception(error, {"Job": "job-1"}) is True
    instance.flush()

    [(_, _, report)] = sent
    assert report["type"] == "MemoryError"
    assert report["context"]["Job"] == "job-1"
    assert report["context"]["Location"].startswith("test_error_report.py:")


def test_report_exception_without_reporting_is_a_no_op(monkeypatch):
    monkeypatch.setattr(error_report, "_reporter", None)

    assert error_report.report_exception(RuntimeError("boom")) is False


def test_post_report_sends_json_with_the_token(monkeypatch):
    requests = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class Opener:
        def open(self, request, timeout):
            requests.append(request)
            return Response()

    monkeypatch.setattr(error_report, "_opener", Opener())

    assert error_report.post_report(URL, "secret", {"type": "RuntimeError"}) == 200
    [request] = requests
    assert request.get_header("Authorization") == "Bearer secret"
    assert json.loads(request.data) == {"type": "RuntimeError"}


# --- check --online -----------------------------------------------------------

class Response:
    def __init__(self, status_code):
        self.status_code = status_code


class Http:
    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def test_online_check_sends_a_test_report(tmp_path):
    config = load(tmp_path, f"ERROR_REPORT_URL={URL}\n", token="secret")
    http = Http(Response(200))

    result = online_check.check_error_report(config, http)

    assert result.status == "ok"
    [(url, kwargs)] = http.calls
    assert url == URL
    assert kwargs["json"]["test"] is True
    assert kwargs["headers"]["Authorization"] == "Bearer secret"


@pytest.mark.parametrize(("status", "hint"), [(401, "SERVER_ERROR_TOKEN"), (404, "ERROR_REPORT_URL"), (503, "SERVER_ERROR_TOKEN")])
def test_online_check_explains_refusals(tmp_path, status, hint):
    config = load(tmp_path, f"ERROR_REPORT_URL={URL}\n", token="secret")

    result = online_check.check_error_report(config, Http(Response(status)))

    assert result.status == "fail"
    assert hint in result.hint


def test_online_check_skips_without_url(tmp_path):
    results = online_check.run_checks(load(tmp_path))

    assert ("Error reports", "skip") in [(result.name, result.status) for result in results]


# --- Keeping secrets in -------------------------------------------------------

@pytest.mark.parametrize(
    "url", ["http://pianoweave.example/api/server-errors", "ftp://pianoweave.example/x", "https://"],
)
def test_url_must_use_https(tmp_path, url):
    with pytest.raises(ConfigError, match="ERROR_REPORT_URL"):
        load(tmp_path, f"ERROR_REPORT_URL={url}\n")


def test_plain_http_is_allowed_only_on_this_machine(tmp_path):
    config = load(tmp_path, "ERROR_REPORT_URL=http://127.0.0.1:8090/api/server-errors\n")

    assert config.error_report.url == "http://127.0.0.1:8090/api/server-errors"


def test_secrets_are_cut_out_of_reports():
    instance, sent = reporter()
    instance.secrets = ["a-long-runpod-key", "token"]
    instance.submit({
        "type": "OperationalError",
        "message": "postgresql://billing:hunter2pass@postgres/billing refused a-long-runpod-key",
        "traceback": "GET https://bucket.r2.dev/a.wav?X-Amz-Signature=abc&X-Amz-Credential=key failed",
        "context": {"Job": "sent with token"},
    })
    instance.flush()

    [(_, _, report)] = sent
    assert report["message"] == "postgresql://billing:********@postgres/billing refused ********"
    assert report["traceback"] == "GET https://bucket.r2.dev/a.wav?… failed"
    assert report["context"]["Job"] == "sent with ********"


def test_every_secret_of_the_server_is_redacted(tmp_path):
    config = load(tmp_path, f"ERROR_REPORT_URL={URL}\n", token="the-report-token")
    secrets_dir = tmp_path / "secrets"
    (secrets_dir / "runpod_api_key").write_text("rp-key-0123456789", encoding="utf-8")
    (secrets_dir / "postgres_password").write_text("pg-password-0123\n", encoding="utf-8")
    config = load_settings(str(tmp_path / ".env"), str(secrets_dir))

    values = error_report.secret_values(config)

    assert {"the-report-token", "rp-key-0123456789", "pg-password-0123"} <= set(values)


def test_redirects_are_not_followed():
    import http.server
    import threading

    hits = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            hits.append((self.path, self.headers.get("Authorization")))
            self.send_response(302)
            self.send_header("Location", "/elsewhere")
            self.end_headers()

        do_GET = do_POST

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status = error_report.post_report(f"http://127.0.0.1:{server.server_port}/api/server-errors", "secret", {})
    finally:
        server.shutdown()

    assert status == 302
    assert hits == [("/api/server-errors", "Bearer secret")]


def test_online_check_refuses_a_redirect(tmp_path):
    config = load(tmp_path, f"ERROR_REPORT_URL={URL}\n", token="secret")
    http = Http(Response(301))

    result = online_check.check_error_report(config, http)

    assert result.status == "fail"
    assert "redirected" in result.detail
    assert http.calls[0][1]["allow_redirects"] is False


def test_errors_still_reach_the_containers_log(monkeypatch, capsys, default_settings):
    import logging

    root = logging.getLogger()
    saved = list(root.handlers)
    monkeypatch.setattr(error_report, "_reporter", None)
    default_settings.error_report.url = "https://monitor.example/errors"
    default_settings.secrets.error_report_token = "token"
    root.handlers = []
    try:
        error_report.install("api", default_settings)
        logging.getLogger("app.main").error("Off-site backup copying is failing")
        assert "Off-site backup copying is failing" in capsys.readouterr().err
    finally:
        root.handlers = saved
