"""Placing jobs by load across a home cluster's devices (app/worker/load.py,
app.worker.tasks.pass_reason): no job goes to a saturated GPU while another
device is idle and has room."""

import types

import fakeredis
import pytest

from app import cluster
from app.worker import load, tasks


@pytest.fixture
def placing(api_context, monkeypatch):
    _, settings, billing = api_context
    settings.billing.provider = "google_play"
    settings.billing.products = {"starter": 10}
    billing.credit_purchase("user", "starter", "token", "order")
    billing.reserve("user", "job", 2)
    tasks.redis.hset(tasks.job_key("job"), mapping={"status": "queued", "user_id": "user", "full_cost": "2",
                                                     "source": "upload"})
    cluster.register(tasks.redis, "desktop", cluster.TASKS)
    cluster.forget_egress_cache()
    sent = []
    monkeypatch.setattr(tasks, "send_job", lambda job_id, backend, *arguments, **options: sent.append(options))
    yield settings, sent
    monkeypatch.setattr(tasks, "load_monitor", None)
    cluster.forget_egress_cache()


def here(monkeypatch, utilization, free_mib=8000.0, gpu=True):
    monitor = types.SimpleNamespace(current=lambda: load.Load(utilization, free_mib, gpu))
    monkeypatch.setattr(tasks, "load_monitor", monitor)


def other(node="desktop", utilization=10.0, free_mib=10000.0, running=0, capacity=4, **options):
    tasks.routing.publish_heartbeat(tasks.redis, capacity, 30.0, worker=f"{node}:1", node=node, running=running,
                                    utilization=utilization, free_mib=free_mib, gpu=True, **options)


def test_a_saturated_gpu_leaves_the_job_to_an_idle_device(placing, monkeypatch):
    _, sent = placing
    here(monkeypatch, utilization=100.0)
    other()

    assert tasks.pass_job("job", "local")
    assert sent == [{"delay": tasks.PASS_DELAY_MS}]


def test_a_gpu_without_vram_for_one_more_job_leaves_it_to_one_with_room(placing, monkeypatch):
    settings, sent = placing
    here(monkeypatch, utilization=20.0, free_mib=settings.worker.memory_per_job_gib * 1024 - 1)
    other()

    assert tasks.pass_job("job", "local")


def test_a_device_with_room_runs_the_job_itself(placing, monkeypatch):
    here(monkeypatch, utilization=40.0)
    other()

    assert not tasks.pass_job("job", "local")


@pytest.mark.parametrize("options", [
    {"utilization": 95.0},          # as busy
    {"running": 4},                 # every place taken
    {"free_mib": 500.0},            # no VRAM for the job
    {"utilization": None},          # load unknown
    {"node": "hub"},                # this device itself
    {"transcription": False},       # takes no transcriptions
])
def test_the_job_stays_when_no_other_device_is_better(placing, monkeypatch, options):
    here(monkeypatch, utilization=100.0)
    other(**options)

    assert not tasks.pass_job("job", "local")


def test_an_arrangement_goes_only_to_a_device_that_arranges(placing, monkeypatch):
    here(monkeypatch, utilization=100.0)
    tasks.redis.hset(tasks.job_key("job"), "mode", tasks.ARRANGEMENT)
    other(arrangement=False)
    assert not tasks.pass_job("job", "local")

    other(node="laptop", arrangement=True)
    assert tasks.pass_job("job", "local")


def test_a_busy_gpu_never_hands_jobs_to_a_cpu_worker(placing, monkeypatch):
    here(monkeypatch, utilization=100.0)
    tasks.routing.publish_heartbeat(tasks.redis, 4, 30.0, worker="desktop:1", node="desktop", utilization=5.0, gpu=False)
    assert not tasks.pass_job("job", "local")

    # A busy CPU worker does.
    here(monkeypatch, utilization=100.0, free_mib=None, gpu=False)
    assert tasks.pass_job("job", "local")


def test_the_threshold_is_a_setting(placing, monkeypatch):
    settings, _ = placing
    settings.worker.busy_utilization = 99.5
    here(monkeypatch, utilization=99.0)
    other()

    assert not tasks.pass_job("job", "local")


def test_jobs_sent_to_runpod_are_not_placed_by_gpu_load(placing, monkeypatch):
    here(monkeypatch, utilization=100.0)
    other()

    assert not tasks.pass_job("job", "remote")


def test_without_nodes_the_load_moves_nothing(api_context, monkeypatch):
    tasks.redis.hset(tasks.job_key("job"), mapping={"status": "queued", "source": "upload"})
    here(monkeypatch, utilization=100.0)
    other()
    monkeypatch.setattr(tasks, "send_job", lambda *arguments, **options: pytest.fail("passed"))

    assert not tasks.pass_job("job", "local")
    monkeypatch.setattr(tasks, "load_monitor", None)


# --- Sampling -------------------------------------------------------------------------------

def nvidia_smi(output, code=0):
    return lambda command, **options: types.SimpleNamespace(returncode=code, stdout=output)


def test_the_busiest_gpu_counts_and_the_roomiest_takes_the_job(monkeypatch):
    monkeypatch.setattr(load.shutil, "which", lambda name: "/usr/bin/nvidia-smi")
    monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)
    monkeypatch.delenv("NVIDIA_VISIBLE_DEVICES", raising=False)

    assert load.sample_gpus(nvidia_smi("0, 93, 3600\n1, 5, 11000\n")) == (93.0, 11000.0)


def test_only_the_gpus_this_worker_sees_count(monkeypatch):
    monkeypatch.setattr(load.shutil, "which", lambda name: "/usr/bin/nvidia-smi")
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "1")

    assert load.sample_gpus(nvidia_smi("0, 93, 3600\n1, 5, 11000\n")) == (5.0, 11000.0)


@pytest.mark.parametrize("output, code", [("", 0), ("garbage\n", 0), ("0, 93, 3600\n", 9)])
def test_an_unreadable_gpu_is_an_unknown_load(monkeypatch, output, code):
    monkeypatch.setattr(load.shutil, "which", lambda name: "/usr/bin/nvidia-smi")
    monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)

    assert load.sample_gpus(nvidia_smi(output, code)) is None


def test_without_nvidia_smi_the_load_is_unknown(monkeypatch):
    monkeypatch.setattr(load.shutil, "which", lambda name: None)

    assert load.sample_gpus() is None


def test_the_monitor_averages_utilization_and_keeps_the_least_free_memory():
    samples = iter([(100.0, 4000.0), (80.0, 6000.0), (90.0, 5000.0)])
    monitor = load.LoadMonitor(gpu=True, gpu_sampler=lambda: next(samples))
    for _ in range(3):
        monitor.sample()

    assert monitor.current() == load.Load(90.0, 4000.0, True)


def test_one_spike_does_not_make_a_device_busy():
    samples = iter([(10.0, 9000.0), (10.0, 9000.0), (10.0, 9000.0), (100.0, 9000.0)])
    monitor = load.LoadMonitor(gpu=True, gpu_sampler=lambda: next(samples))
    for _ in range(4):
        monitor.sample()

    assert monitor.current().utilization == 32.5


def test_a_cpu_worker_reports_cpu_use():
    monitor = load.LoadMonitor(gpu=False, cpu_sampler=lambda: 75.0)
    monitor.sample()

    assert monitor.current() == load.Load(75.0, None, False)


def test_the_heartbeat_carries_the_load():
    redis = fakeredis.FakeRedis(decode_responses=True)
    tasks.routing.publish_heartbeat(redis, 3, 20.0, worker="w", node="desktop", running=2, utilization=55.5,
                                    free_mib=7000, gpu=True)

    [worker] = tasks.routing.local_workers(redis)
    assert (worker["node"], worker["running"], worker["utilization"], worker["free_mib"], worker["gpu"]) == (
        "desktop", 2, 55.5, 7000.0, True)
