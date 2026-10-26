import os

import pytest

from app.worker import launcher

GiB = 1024**3


@pytest.fixture
def cores(monkeypatch):
    def set_cores(count):
        monkeypatch.setattr(launcher, "physical_cpu_count", lambda: count)
        monkeypatch.setattr("app.transcription.device.physical_cpu_count", lambda: count)

    set_cores(8)
    return set_cores


def capacity(*jobs):
    return [(index, count, 6 * GiB, 8 * GiB) for index, count in enumerate(jobs)]


def test_cpu_mode_threads_follow_batch_size(default_settings, cores):
    plan = launcher.plan_worker(None)

    assert (plan.processes, plan.threads, plan.cpu_threads) == (1, 6, 6)
    assert "no CUDA GPU" in plan.summary


def test_cpu_mode_uses_configured_threads_and_batch(default_settings, cores):
    default_settings.worker.threads = 4
    default_settings.worker.batch_size = 3

    plan = launcher.plan_worker(None)

    assert (plan.threads, plan.cpu_threads) == (3, 4)


def test_cpu_threads_are_capped_by_physical_cores(default_settings, cores):
    cores(2)
    default_settings.worker.threads = 16

    assert launcher.plan_worker(None).cpu_threads == 2


def test_gpu_mode_threads_follow_vram_capacity(default_settings, cores):
    plan = launcher.plan_worker(capacity(3, 2))

    assert (plan.threads, plan.cpu_threads) == (5, None)
    assert "2 GPU(s), up to 5 job(s) by VRAM" in plan.summary


def test_enforce_cpu_ignores_gpu_capacity(default_settings, cores):
    default_settings.worker.enforce_cpu = True

    plan = launcher.plan_worker(capacity(4))

    assert plan.cpu_threads == 6
    assert plan.threads == 6
    assert "no CUDA GPU" in plan.summary


def test_gpu_mode_respects_max_concurrency_and_cores(default_settings, cores):
    default_settings.worker.max_concurrency = 2
    assert launcher.plan_worker(capacity(5)).threads == 2

    default_settings.worker.max_concurrency = None
    cores(3)
    assert launcher.plan_worker(capacity(5)).threads == 3


def test_gpu_without_room_falls_back_to_cpu(default_settings, cores):
    assert launcher.plan_worker(capacity(0)).cpu_threads == 6


def test_main_passes_resolved_values_to_dramatiq(default_settings, cores, monkeypatch):
    calls = []
    monkeypatch.setattr(launcher, "cuda_job_capacities", lambda: None)
    monkeypatch.setattr(launcher.shutil, "which", lambda name: "/usr/bin/dramatiq")
    monkeypatch.setattr(launcher.os, "execv", lambda path, args: calls.append(args))
    monkeypatch.delenv("WORKER_THREADS", raising=False)
    monkeypatch.setenv("OMP_NUM_THREADS", "1")

    launcher.main([])

    assert calls == [["/usr/bin/dramatiq", "app.worker.tasks", "--processes", "1", "--threads", "6", "--queues", "default"]]
    assert os.environ["OMP_NUM_THREADS"] == "6"
    assert "WORKER_THREADS" not in os.environ


def test_remote_worker_idles_when_outsourcing_is_off(default_settings, monkeypatch, capsys):
    # The service always runs; .env decides whether it does anything.
    idled = []
    monkeypatch.setattr(launcher, "idle", lambda reason: idled.append(reason))
    monkeypatch.setattr(launcher.os, "execv", lambda path, args: pytest.fail("Dramatiq started"))

    launcher.main(["remote"])

    assert len(idled) == 1 and "OUTSOURCE_PROVIDER" in idled[0]


def test_idle_waits_until_the_container_is_stopped(monkeypatch, capsys):
    handlers = {}
    monkeypatch.setattr(launcher.signal, "signal", lambda signum, handler: handlers.setdefault(signum, handler))

    def sleep(seconds):
        # docker stop sends SIGTERM.
        handlers[launcher.signal.SIGTERM](launcher.signal.SIGTERM, None)

    monkeypatch.setattr(launcher.time, "sleep", sleep)

    with pytest.raises(SystemExit) as stopped:
        launcher.idle("Outsourcing is off")

    assert stopped.value.code == 0
    assert "Outsourcing is off" in capsys.readouterr().out
