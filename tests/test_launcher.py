import os
import types

import pytest

from app.worker import launcher

GiB = 1024**3


@pytest.fixture
def cores(monkeypatch):
    def set_cores(count):
        monkeypatch.setattr(launcher, "physical_cpu_count", lambda: count)
        monkeypatch.setattr("app.transcription.device.physical_cpu_count", lambda: count)

    # Plenty of RAM, so the CPU capacity follows the cores whatever this
    # machine, or earlier tests, left free.
    class Memory:
        available = 64 * 1024**3

    # A stand-in module, so that this also runs where psutil is not installed.
    monkeypatch.setattr(
        "app.transcription.device.psutil",
        types.SimpleNamespace(virtual_memory=lambda: Memory(), cpu_count=lambda logical=False: 8),
    )

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
    default_settings.worker.arrangement = False
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


# --- piano arrangements -----------------------------------------------------------

def test_a_gpu_with_room_takes_the_arrangement_queue(default_settings, cores):
    default_settings.worker.arrangement = True

    plan = launcher.plan_worker(capacity(3))

    assert plan.arrangement and plan.warnings == ()
    assert "arrangements" in plan.summary


def test_a_gpu_without_room_for_one_arrangement_warns_and_skips_the_queue(default_settings, cores):
    default_settings.worker.arrangement = True
    # 2 GiB free: 1.5 GiB of reserves leave 0.5, under the 0.8 one arrangement needs; one job fits.
    plan = launcher.plan_worker([(0, 1, 2 * GiB, 8 * GiB)])

    assert not plan.arrangement
    assert len(plan.warnings) == 1 and "WORKER_ARRANGEMENT is on" in plan.warnings[0]
    assert "arrangements" not in plan.summary


def test_one_roomy_gpu_among_several_is_enough(default_settings, cores):
    default_settings.worker.arrangement = True

    assert launcher.plan_worker([(0, 1, 2 * GiB, 8 * GiB), (1, 3, 6 * GiB, 8 * GiB)]).arrangement


def test_arrangements_off_never_take_the_queue(default_settings, cores):
    default_settings.worker.arrangement = False
    plan = launcher.plan_worker(capacity(5))

    assert not plan.arrangement and plan.warnings == ()


@pytest.mark.parametrize("enabled", [True, False])
def test_cpu_plans_arrange_when_the_feature_is_on(default_settings, cores, enabled):
    default_settings.worker.arrangement = enabled

    assert launcher.plan_worker(None).arrangement is enabled


def run_main(monkeypatch, capacities):
    calls = []
    monkeypatch.setattr(launcher, "cuda_job_capacities", lambda: capacities)
    monkeypatch.setattr(launcher.shutil, "which", lambda name: "/usr/bin/dramatiq")
    monkeypatch.setattr(launcher.os, "execv", lambda path, args: calls.append(args))
    launcher.main([])
    return calls[0]


def test_main_passes_both_queues_to_dramatiq_when_the_worker_arranges(default_settings, cores, monkeypatch, capsys):
    default_settings.worker.arrangement = True

    args = run_main(monkeypatch, None)

    assert args[args.index("--queues"):] == ["--queues", "default", "arrangement"]
    assert "queue(s) default, arrangement" in capsys.readouterr().out


def test_main_passes_one_queue_when_the_worker_does_not_arrange(default_settings, cores, monkeypatch):
    default_settings.worker.arrangement = True

    args = run_main(monkeypatch, [(0, 1, 2 * GiB, 8 * GiB)])

    assert args[args.index("--queues"):] == ["--queues", "default"]


def test_main_prints_the_warning_when_no_gpu_has_room(default_settings, cores, monkeypatch, capsys):
    default_settings.worker.arrangement = True

    run_main(monkeypatch, [(0, 1, 2 * GiB, 8 * GiB)])

    assert "Warning: WORKER_ARRANGEMENT is on" in capsys.readouterr().out
