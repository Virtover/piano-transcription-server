import logging

import pytest
import requests

from app.outsource import capacity, routing
from app.outsource.runpod import RunPodProvider
from app.worker import launcher, tasks


@pytest.fixture
def auto(api_context):
    _, settings, _ = api_context
    settings.outsource.provider = "runpod"
    settings.outsource.max_concurrency = "auto"
    return settings


class Provider:
    def __init__(self, workers):
        self.workers = workers
        self.calls = 0

    def max_workers(self):
        self.calls += 1
        return self.workers


def test_default_is_four(api_context):
    _, settings, _ = api_context

    assert settings.outsource.max_concurrency == 4
    assert capacity.remote_job_limit(tasks.redis) == 4
    assert capacity.remote_worker_threads(tasks.redis) == 4


def test_fixed_value_ignores_provider(api_context):
    _, settings, _ = api_context
    settings.outsource.max_concurrency = 9
    tasks.redis.hset(capacity.CAPACITY_KEY, mapping={"workers_max": 5, "jobs_per_worker": 12})

    assert capacity.remote_job_limit(tasks.redis) == 9
    assert capacity.remote_worker_threads(tasks.redis) == 9


def test_auto_is_max_workers_times_requests_per_worker(auto):
    capacity.refresh_max_workers(tasks.redis, Provider(3))

    assert capacity.remote_job_limit(tasks.redis) == 3 * capacity.DEFAULT_JOBS_PER_WORKER

    capacity.record_jobs_per_worker(tasks.redis, 20)
    assert capacity.remote_job_limit(tasks.redis) == 60


@pytest.mark.parametrize("reported", [0, -3, "12", None, True, 3.5])
def test_invalid_worker_capacity_reports_are_ignored(auto, reported):
    capacity.refresh_max_workers(tasks.redis, Provider(2))

    capacity.record_jobs_per_worker(tasks.redis, reported)

    assert capacity.remote_job_limit(tasks.redis) == 24


def test_reported_capacity_is_capped(auto):
    capacity.refresh_max_workers(tasks.redis, Provider(1))

    capacity.record_jobs_per_worker(tasks.redis, 500)

    assert capacity.remote_job_limit(tasks.redis) == capacity.MAX_JOBS_PER_WORKER


def test_auto_falls_back_when_max_workers_unknown(auto, caplog):
    with caplog.at_level(logging.WARNING):
        capacity.refresh_max_workers(tasks.redis, Provider(None))

    assert capacity.remote_job_limit(tasks.redis) == capacity.FALLBACK_LIMIT
    assert "max workers unknown" in caplog.text


def test_auto_keeps_last_known_value_when_refresh_fails(auto):
    capacity.refresh_max_workers(tasks.redis, Provider(2))
    capacity.refresh_max_workers(tasks.redis, Provider(None))

    assert capacity.remote_job_limit(tasks.redis) == 24


def test_auto_threads_leave_room_for_larger_gpus(auto):
    capacity.refresh_max_workers(tasks.redis, Provider(2))
    assert capacity.remote_worker_threads(tasks.redis) == 2 * capacity.MAX_JOBS_PER_WORKER

    capacity.refresh_max_workers(tasks.redis, Provider(10))
    assert capacity.remote_worker_threads(tasks.redis) == capacity.MAX_THREADS


def test_routing_uses_the_auto_limit(auto):
    capacity.refresh_max_workers(tasks.redis, Provider(1))
    capacity.record_jobs_per_worker(tasks.redis, 3)

    backends = [routing.choose_backend(tasks.redis, f"job-{index}") for index in range(5)]

    assert backends == ["remote", "remote", "remote", "local", "local"]


def test_remote_launcher_reads_max_workers_at_startup(auto, monkeypatch):
    provider = Provider(2)
    monkeypatch.setattr("app.outsource.provider.get_provider", lambda: provider)
    monkeypatch.setattr("redis.Redis.from_url", lambda url, **kwargs: tasks.redis)

    plan = launcher.remote_plan()

    assert provider.calls == 1
    assert plan.threads == 48
    assert "up to 24 remote job(s) (auto" in plan.summary


def test_remote_launcher_with_fixed_value_does_not_call_provider(api_context, monkeypatch):
    _, settings, _ = api_context
    settings.outsource.provider = "runpod"
    settings.outsource.max_concurrency = 6
    monkeypatch.setattr("app.outsource.provider.get_provider", lambda: pytest.fail("provider called"))
    monkeypatch.setattr("redis.Redis.from_url", lambda url, **kwargs: tasks.redis)

    plan = launcher.remote_plan()

    assert plan.threads == 6


def test_config_accepts_auto(tmp_path):
    from app.config import load_settings

    env = tmp_path / ".env"
    env.write_text("OUTSOURCE_MAX_CONCURRENCY=auto\n")

    assert load_settings(str(env), None).outsource.max_concurrency is None


# --- RunPod max workers -----------------------------------------------------

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
    def __init__(self, response):
        self.response = response
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def test_runpod_max_workers_from_rest_api():
    session = Session(Response(200, {"id": "endpoint-1", "workersMax": 3, "workersMin": 0}))
    provider = RunPodProvider("https://api.runpod.ai/v2", "endpoint-1", "rp-key", session=session)

    assert provider.max_workers() == 3
    method, url, kwargs = session.calls[0]
    assert (method, url) == ("GET", "https://rest.runpod.io/v1/endpoints/endpoint-1")
    assert kwargs["headers"] == {"Authorization": "Bearer rp-key"}


@pytest.mark.parametrize(
    "response",
    [Response(403), Response(200, None), Response(200, {"workersMax": 0}), Response(200, {}), requests.ConnectionError("down")],
)
def test_runpod_max_workers_unknown(response, caplog):
    provider = RunPodProvider("https://api.runpod.ai/v2", "endpoint-1", "rp-key", session=Session(response))

    assert provider.max_workers() is None
    assert "rp-key" not in caplog.text


def test_local_test_server_counts_as_one_worker():
    session = Session(Response(500))
    provider = RunPodProvider("http://localhost:8010", "local", "key", session=session)

    assert provider.max_workers() == 1
    assert session.calls == []
