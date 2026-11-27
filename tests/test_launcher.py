import os
import types

import pytest

from app.worker import launcher
from app.transcription.device import MEASURED_VARIABLE
from app.worker.places import PLACES_VARIABLE

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
        total = 64 * 1024**3

    # A stand-in module, so that this also runs where psutil is not installed.
    monkeypatch.setattr(
        "app.transcription.device.psutil",
        types.SimpleNamespace(virtual_memory=lambda: Memory(), cpu_count=lambda logical=False: 8),
    )

    set_cores(8)
    return set_cores


def capacity(*jobs):
    return [(index, count, 6 * GiB, 8 * GiB) for index, count in enumerate(jobs)]


@pytest.fixture(autouse=True)
def launcher_environment(monkeypatch):
    # main() writes Dramatiq's environment into os.environ: undone after each test.
    for name in (PLACES_VARIABLE, MEASURED_VARIABLE, "dramatiq_queue_prefetch", "OMP_NUM_THREADS",
                 "MKL_NUM_THREADS"):
        monkeypatch.setenv(name, "")


def test_cpu_mode_threads_follow_batch_size(default_settings, cores):
    plan = launcher.plan_worker(None)

    # On the CPU, one thread per place: the last steps need the same cores.
    assert (plan.processes, plan.threads, plan.cpu_threads, plan.places) == (1, 6, 6, 6)
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

    # A place per job that fits in VRAM, and a second thread for each, for
    # jobs past their model step.
    assert (plan.places, plan.threads, plan.cpu_threads) == (5, 10, None)
    assert "2 GPU(s), up to 5 job(s) by total VRAM" in plan.summary


def test_enforce_cpu_ignores_gpu_capacity(default_settings, cores):
    default_settings.worker.enforce_cpu = True

    plan = launcher.plan_worker(capacity(4))

    assert plan.cpu_threads == 6
    assert plan.threads == 6
    assert "no CUDA GPU" in plan.summary


def test_gpu_places_follow_vram_and_max_concurrency_not_cores(default_settings, cores):
    default_settings.worker.max_concurrency = 2
    plan = launcher.plan_worker(capacity(5))
    assert (plan.places, plan.hardware["limited_by"]) == (2, "WORKER_MAX_CONCURRENCY")

    # A place lasts only to the end of the model step, which runs in one
    # thread for the whole batch: fewer cores than places is fine.
    default_settings.worker.max_concurrency = None
    cores(3)
    plan = launcher.plan_worker(capacity(5))
    assert (plan.places, plan.threads, plan.hardware["limited_by"]) == (5, 10, None)


def test_gpu_without_room_falls_back_to_cpu(default_settings, cores):
    assert launcher.plan_worker(capacity(0)).cpu_threads == 6


def test_main_passes_resolved_values_to_dramatiq(default_settings, cores, monkeypatch):
    default_settings.worker.ensemble = False
    calls = []
    monkeypatch.setattr(launcher, "measure_gpus", lambda: (None, {}))
    monkeypatch.setattr(launcher.shutil, "which", lambda name: "/usr/bin/dramatiq")
    monkeypatch.setattr(launcher.os, "execv", lambda path, args: calls.append(args))
    monkeypatch.delenv("WORKER_THREADS", raising=False)
    monkeypatch.setenv("OMP_NUM_THREADS", "1")

    launcher.main([])

    assert calls == [["/usr/bin/dramatiq", "app.worker.tasks", "--processes", "1", "--threads", "6", "--queues", "default"]]
    assert os.environ["OMP_NUM_THREADS"] == "6"
    assert os.environ[PLACES_VARIABLE] == "6"
    assert "WORKER_THREADS" not in os.environ


def test_main_gives_dramatiq_the_places_and_a_prefetch_of_its_threads(default_settings, cores, monkeypatch):
    default_settings.worker.ensemble = False

    args = run_main(monkeypatch, capacity(3))

    assert args[args.index("--threads") + 1] == "6"
    assert os.environ[PLACES_VARIABLE] == "3"
    # The worker sizes its batches by this measurement, not its own.
    assert os.environ[MEASURED_VARIABLE] == f"0={6 * GiB}:{8 * GiB}"
    # Not Dramatiq's default of twice the threads per queue, which would
    # hold jobs here that a free device could run.
    assert os.environ["dramatiq_queue_prefetch"] == "6"


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


# --- ensemble jobs -----------------------------------------------------------

def test_a_gpu_with_room_takes_the_ensemble_queue(default_settings, cores):
    default_settings.worker.ensemble = True

    plan = launcher.plan_worker(capacity(3))

    assert plan.ensemble and plan.warnings == ()
    assert "ensemble jobs" in plan.summary


# A GPU of 2.4 GiB: its context and 1.5 GiB of reserves leave 0.7, under the
# 2 GiB one ensemble job needs, however little other programs use.
SMALL_GPU = [(0, 1, 2 * GiB, int(2.4 * GiB))]


def test_a_gpu_without_room_for_one_ensemble_job_warns_and_skips_the_queue(default_settings, cores):
    default_settings.worker.ensemble = True
    plan = launcher.plan_worker(SMALL_GPU)

    assert not plan.ensemble
    assert len(plan.warnings) == 1 and "WORKER_ENSEMBLE is on" in plan.warnings[0]
    assert "no GPU has room for ensemble jobs (WORKER_ENSEMBLE_MEMORY_GIB=" in plan.warnings[0]
    assert "ensemble jobs" not in plan.summary


def test_one_roomy_gpu_among_several_is_enough(default_settings, cores):
    default_settings.worker.ensemble = True

    assert launcher.plan_worker([*SMALL_GPU, (1, 3, 6 * GiB, 8 * GiB)]).ensemble


def test_ensembles_follow_total_vram_not_what_is_free_at_start(default_settings, cores):
    # Another program used most of the VRAM as the worker started: it still
    # takes ensemble jobs, which wait for room like every job.
    default_settings.worker.ensemble = True

    plan = launcher.plan_worker([(0, 5, int(0.5 * GiB), 8 * GiB)])

    assert plan.ensemble and plan.places == 5


def test_ensembles_off_never_take_the_queue(default_settings, cores):
    default_settings.worker.ensemble = False
    plan = launcher.plan_worker(capacity(5))

    assert not plan.ensemble and plan.warnings == ()


@pytest.mark.parametrize("enabled", [True, False])
def test_cpu_plans_take_ensembles_when_the_feature_is_on(default_settings, cores, enabled):
    default_settings.worker.ensemble = enabled

    assert launcher.plan_worker(None).ensemble is enabled


def run_main(monkeypatch, capacities):
    calls = []
    monkeypatch.setattr(launcher, "measure_gpus", lambda: (capacities, {}))
    monkeypatch.setattr(launcher.shutil, "which", lambda name: "/usr/bin/dramatiq")
    monkeypatch.setattr(launcher.os, "execv", lambda path, args: calls.append(args))
    launcher.main([])
    return calls[0]


def test_main_passes_both_queues_to_dramatiq_when_the_worker_takes_ensembles(default_settings, cores, monkeypatch, capsys):
    default_settings.worker.ensemble = True

    args = run_main(monkeypatch, None)

    assert args[args.index("--queues"):] == ["--queues", "default", "arrangement"]
    assert "queue(s) default, arrangement" in capsys.readouterr().out


def test_main_passes_one_queue_when_the_worker_takes_no_ensembles(default_settings, cores, monkeypatch):
    default_settings.worker.ensemble = True

    args = run_main(monkeypatch, SMALL_GPU)

    assert args[args.index("--queues"):] == ["--queues", "default"]


def test_main_prints_the_warning_when_no_gpu_has_room(default_settings, cores, monkeypatch, capsys):
    default_settings.worker.ensemble = True

    run_main(monkeypatch, SMALL_GPU)

    assert "Warning: WORKER_ENSEMBLE is on" in capsys.readouterr().out


def test_a_gpu_plan_names_the_gpu_and_its_vram(default_settings, cores, monkeypatch):
    default_settings.worker.ensemble = True
    plan = launcher.plan_worker([(0, 3, 6 * GiB, 8 * GiB)], {0: "NVIDIA GeForce RTX 3060 Ti"})

    assert "GPU 0 (NVIDIA GeForce RTX 3060 Ti): up to 3 job(s), batch 3, ensemble jobs, 8.00 GiB, 6.00 GiB free now" in \
        plan.summary
    assert plan.hardware == {"kind": "gpu", "places": 3, "limited_by": None, "arrangements": True, "gpus": [
        {"index": 0, "name": "NVIDIA GeForce RTX 3060 Ti", "vram_mib": 8192, "free_mib": 6144, "jobs": 3,
         "arrangements": True},
    ]}


def test_a_cpu_plan_names_the_processor_and_its_ram(default_settings, cores, monkeypatch):
    monkeypatch.setattr(launcher, "cpu_model_name", lambda: "Intel(R) Core(TM) i7-10700K")

    hardware = launcher.plan_worker(None).hardware

    assert hardware["kind"] == "cpu" and hardware["places"] == 6
    assert (hardware["name"], hardware["cores"], hardware["threads"], hardware["ram_mib"]) == (
        "Intel(R) Core(TM) i7-10700K", 8, 6, 64 * 1024)


def test_the_gpus_are_measured_in_a_process_of_their_own(default_settings):
    # A process that used CUDA keeps a context on the GPU until it exits.
    calls = []

    def run(command, **options):
        calls.append(command)
        output = '{"capacities": [[0, 9, 12348030976, 12481724416]], "names": {"0": "NVIDIA GeForce RTX 3060"}}'
        return types.SimpleNamespace(returncode=0, stdout="a warning\n" + output + "\n", stderr="")

    capacities, names = launcher.measure_gpus(run)

    assert calls[0][:2] == [launcher.sys.executable, "-c"]
    assert capacities == [(0, 9, 12348030976, 12481724416)]
    assert names == {0: "NVIDIA GeForce RTX 3060"}


@pytest.mark.parametrize(("reported", "expected"), [('"fp32": true, ', True), ('"fp32": false, ', False), ("", False)])
def test_the_measuring_process_tells_whether_the_gpus_run_float32(default_settings, monkeypatch, reported, expected):
    from app.transcription import device

    monkeypatch.setattr(device, "_fp32_gpus", None)

    def run(command, **options):
        output = '{"capacities": [[0, 9, 12348030976, 12481724416]], ' + reported + '"names": {"0": "GPU"}}'
        return types.SimpleNamespace(returncode=0, stdout=output + "\n", stderr="")

    launcher.measure_gpus(run)

    # Stored for ensemble_memory_gib, so this process needs no CUDA.
    assert device._fp32_gpus is expected


def test_a_float32_gpu_needs_more_room_for_the_ensemble_queue(default_settings, cores, monkeypatch):
    from app.transcription import device

    default_settings.worker.ensemble = True
    # Room for one ensemble job (WORKER_ENSEMBLE_MEMORY_GIB), not for its 1.25 times in float32.
    need = default_settings.worker.ensemble_memory_gib
    free = (device.gpu_reserve_gib(default_settings.worker) + 0.25 + need * 1.1) * GiB
    total = int(free + device.GPU_CONTEXT_GIB * GiB)
    for fp32, takes in ((False, True), (True, False)):
        monkeypatch.setattr(device, "_fp32_gpus", fp32)
        assert device.ensemble_fits(device.usable_memory(total)) is takes
        assert launcher.plan_worker([(0, 1, total, total)]).ensemble is takes


def test_a_failed_measuring_process_measures_here(default_settings, monkeypatch, capsys):
    monkeypatch.setattr(launcher, "cuda_job_capacities", lambda: [(0, 2, 4 * GiB, 8 * GiB)])
    monkeypatch.setattr(launcher, "cuda_device_names", lambda: {0: "GPU"})

    def run(command, **options):
        return types.SimpleNamespace(returncode=1, stdout="", stderr="Traceback: boom")

    assert launcher.measure_gpus(run) == ([(0, 2, 4 * GiB, 8 * GiB)], {0: "GPU"})
    assert "could not measure the GPUs in a process of their own" in capsys.readouterr().out


def test_no_gpus_measured_without_cuda(default_settings):
    def run(command, **options):
        return types.SimpleNamespace(returncode=0, stdout='{"capacities": null, "names": {}}\n', stderr="")

    assert launcher.measure_gpus(run) == (None, {})


def test_enforce_cpu_measures_no_gpu(default_settings):
    default_settings.worker.enforce_cpu = True

    assert launcher.measure_gpus(lambda *a, **k: pytest.fail("measured")) == (None, {})
