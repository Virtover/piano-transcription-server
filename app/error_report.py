"""Optional error reports to the operator's website: every ERROR log record
of the API and the workers, and job failures without a known cause, are sent
to ERROR_REPORT_URL with the secret error_report_token. The Piano Weave
website shows them in its Error Monitor and alerts on Discord.

Reports are sent from a background thread, so logging never waits for the
network. A report that cannot be sent is only printed: logging it as an error
would report it again.

Before a report leaves the server, every secret this server holds, passwords
in connection URLs, and the query of every link (signed storage links carry
their signature there) are cut out. Redirects are not followed, so the token
only ever goes to ERROR_REPORT_URL itself.
"""

import json
import logging
import queue
import re
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

from pydantic import SecretStr

from app.config import settings


# Set by the worker launcher, so each Dramatiq process knows its service.
SERVICE_ENVIRONMENT = "ERROR_REPORT_SERVICE"
REQUEST_TIMEOUT = 10
# The same error is sent at most once per interval; the website counts repeats.
REPEAT_INTERVAL = 60.0
MAX_REPORTS_PER_MINUTE = 30
QUEUE_SIZE = 100
MAX_MESSAGE_LENGTH = 2000
MAX_TRACEBACK_LENGTH = 20000
USER_AGENT = "piano-transcription-server"
REDACTED = "********"
# Secrets shorter than this could match ordinary text, and are too weak to matter.
MIN_REDACTED_LENGTH = 8
# The PostgreSQL password is a secret file of its own (app/billing.py).
EXTRA_SECRET_FILES = ("postgres_password",)
URL_PASSWORD = re.compile(r"(\w+://[^\s:/@]+:)[^\s@/]+@")
URL_QUERY = re.compile(r"(https?://[^\s?#'\"<>]+)\?[^\s'\"<>]*")


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """urllib would send the Authorization header on to the redirect's target."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_opener = urllib.request.build_opener(_NoRedirects)


def post_report(url: str, token: str, report: dict, timeout: float = REQUEST_TIMEOUT) -> int:
    """Sends one report and returns the HTTP status."""
    request = urllib.request.Request(
        url,
        data=json.dumps(report).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with _opener.open(request, timeout=timeout) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code


def secret_values(config) -> list[str]:
    """Every secret of this server, to cut out of reports."""
    values = [
        value.get_secret_value()
        for value in config.secrets.model_dump().values()
        if isinstance(value, SecretStr)
    ]
    for name in EXTRA_SECRET_FILES:
        try:
            values.append((Path(config.secrets_dir) / name).read_text(encoding="utf-8").strip())
        except OSError:
            pass
    # Longest first, so a secret that contains another is cut out whole.
    return sorted({value for value in values if len(value) >= MIN_REDACTED_LENGTH}, key=len, reverse=True)


def redact(text: str, secrets: list[str]) -> str:
    for secret in secrets:
        text = text.replace(secret, REDACTED)
    text = URL_PASSWORD.sub(lambda m: m.group(1) + REDACTED + "@", text)
    return URL_QUERY.sub(lambda m: m.group(1) + "?…", text)


def _location(tb) -> str:
    frames = traceback.extract_tb(tb) if tb else []
    return f"{Path(frames[-1].filename).name}:{frames[-1].lineno}" if frames else ""


def _traceback(error: BaseException) -> str:
    return "".join(traceback.format_exception(type(error), error, error.__traceback__))[-MAX_TRACEBACK_LENGTH:]


class Reporter:
    """Queues reports, drops repeats and floods, and sends them in order."""

    def __init__(self, url: str, token: str, service: str, send: Callable[[str, str, dict], int] = post_report,
                 clock: Callable[[], float] = time.monotonic, background: bool = True, secrets: list[str] = ()):
        self.url = url
        self.token = token
        self.service = service
        self.secrets = [*secrets, token]
        self.send = send
        self.clock = clock
        self.background = background
        self.queue: queue.Queue = queue.Queue(QUEUE_SIZE)
        self.last_sent: dict[tuple, float] = {}
        self.recent: list[float] = []
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None

    def submit(self, report: dict) -> bool:
        """Queues a report. False if it was dropped as a repeat, a flood, or a full queue."""
        report = {
            **report,
            "message": redact(report.get("message", ""), self.secrets),
            "traceback": redact(report.get("traceback", ""), self.secrets),
            "context": {key: redact(str(value), self.secrets) for key, value in report.get("context", {}).items()},
        }
        key = (report.get("type"), report.get("message"), report.get("context", {}).get("Location"))
        now = self.clock()
        with self.lock:
            if now - self.last_sent.get(key, -REPEAT_INTERVAL) < REPEAT_INTERVAL:
                return False
            self.recent = [sent for sent in self.recent if now - sent < 60]
            if len(self.recent) >= MAX_REPORTS_PER_MINUTE:
                return False
            if len(self.last_sent) > 1000:
                self.last_sent = {k: t for k, t in self.last_sent.items() if now - t < REPEAT_INTERVAL}
            self.last_sent[key] = now
            self.recent.append(now)
        try:
            self.queue.put_nowait({"service": self.service, **report})
        except queue.Full:
            return False
        if self.background and self.thread is None:
            self.thread = threading.Thread(target=self._run, daemon=True, name="error-report")
            self.thread.start()
        return True

    def flush(self) -> None:
        """Sends everything queued, on the calling thread."""
        while not self.queue.empty():
            self._deliver(self.queue.get_nowait())

    def _run(self) -> None:
        while True:
            self._deliver(self.queue.get())

    def _deliver(self, report: dict) -> None:
        try:
            status = self.send(self.url, self.token, report)
        except Exception as error:
            print(f"Could not send an error report: {type(error).__name__}", file=sys.stderr, flush=True)
            return
        if status >= 300:
            print(f"The error report endpoint answered HTTP {status}", file=sys.stderr, flush=True)


def report_from_record(record: logging.LogRecord) -> dict:
    error = record.exc_info[1] if record.exc_info else None
    message = record.getMessage()
    if error is not None and str(error) and str(error) not in message:
        message = f"{message}: {error}"
    return {
        "level": record.levelname,
        "type": type(error).__name__ if error is not None else "LoggedError",
        "message": message[:MAX_MESSAGE_LENGTH],
        "traceback": _traceback(error) if error is not None else "",
        "context": {"Location": f"{Path(record.pathname).name}:{record.lineno}", "Logger": record.name},
    }


class ReportHandler(logging.Handler):
    def __init__(self, reporter: Reporter):
        super().__init__(level=logging.ERROR)
        self.reporter = reporter

    def emit(self, record: logging.LogRecord) -> None:
        if record.name == __name__:
            return
        try:
            self.reporter.submit(report_from_record(record))
        except Exception:
            self.handleError(record)


_reporter: Reporter | None = None


def install(service: str, config=None) -> bool:
    """Reports this process's errors, if ERROR_REPORT_URL and its token are set.
    True if reporting is on."""
    global _reporter
    config = config or settings
    url, token = config.error_report.url, config.secrets.error_report_token
    if _reporter is None and url and token is not None:
        _reporter = Reporter(url, token.get_secret_value(), service, secrets=secret_values(config))
        handler = ReportHandler(_reporter)
        root = logging.getLogger()
        # Without a handler of its own, the root logger prints warnings and
        # errors to stderr through Python's last resort, which a first handler
        # turns off: they keep going to the container's log too.
        if not root.handlers:
            console = logging.StreamHandler()
            console.setLevel(logging.WARNING)
            root.addHandler(console)
        # uvicorn's loggers do not propagate to the root logger.
        for name in ("", "uvicorn"):
            logging.getLogger(name).addHandler(handler)
    return _reporter is not None


def report_exception(error: BaseException, context: dict[str, str] | None = None) -> bool:
    """Reports an error that is handled, so it is not logged at ERROR level."""
    if _reporter is None:
        return False
    return _reporter.submit({
        "level": "ERROR",
        "type": type(error).__name__,
        "message": (str(error) or type(error).__name__)[:MAX_MESSAGE_LENGTH],
        "traceback": _traceback(error),
        "context": {"Location": _location(error.__traceback__), **(context or {})},
    })
