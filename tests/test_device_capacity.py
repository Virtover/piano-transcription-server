import subprocess
import sys
import types

import pytest

from app.transcription import device
from app.worker import launcher

GiB = 1024**3
MiB = 1024**2


@pytest.fixture
def gpus(monkeypatch, default_settings):
    """Fake torch with configurable GPUs: (uuid, CUDA free bytes, total bytes).
    Arrangements, on by default, are off here: the capacities are those of
    transcriptions alone."""
    default_settings.worker.arrangement = False
    state = {"devices": [], "smi": "", "smi_error": None}

    state["reserved"] = {}
    cuda = types.SimpleNamespace(
        is_available=lambda: bool(state["devices"]),
        device_count=lambda: len(state["devices"]),
        mem_get_info=lambda index: state["devices"][index][1:],
        get_device_properties=lambda index: types.SimpleNamespace(uuid=state["devices"][index][0]),
        memory_reserved=lambda index: state["reserved"].get(index, 0),
    )
    monkeypatch.setitem(sys.modules, "torch", types.SimpleNamespace(cuda=cuda))

    def fake_run(command, **kwargs):
        assert command[0] == "nvidia-smi"
        if state["smi_error"]:
            raise state["smi_error"]
        return subprocess.CompletedProcess(command, 0, stdout=state["smi"])

    monkeypatch.setattr(device.subprocess, "run", fake_run)
    return state


def test_nvidia_smi_output_is_parsed(gpus):
    gpus["smi"] = "GPU-AAAA-1, 5812\nGPU-bbbb-2, 100\nnot a line\n"

    assert device.nvidia_smi_free_memory() == {"aaaa-1": 5812 * MiB, "bbbb-2": 100 * MiB}


@pytest.mark.parametrize(
    "error",
    [FileNotFoundError("nvidia-smi"), subprocess.CalledProcessError(9, "nvidia-smi"), subprocess.TimeoutExpired("nvidia-smi", 10)],
)
def test_missing_or_failing_nvidia_smi_is_ignored(gpus, error):
    gpus["smi_error"] = error

    assert device.nvidia_smi_free_memory() == {}


def test_lower_nvidia_smi_free_memory_is_used(gpus):
    # WSL: CUDA reports 7.0 GiB free, but Windows programs use 1.3 GiB more.
    gpus["devices"] = [("aaaa-1", 7126 * MiB, 8 * GiB)]
    gpus["smi"] = "GPU-aaaa-1, 5812\n"

    [(index, capacity, free, total)] = device.cuda_job_capacities()

    assert free == 5812 * MiB
    # The capacity is the total's: floor((8 - 0.25 context - 1.0 - 0.25 tempo map) / 1.1) = 5.
    assert (index, capacity, total) == (0, 5, 8 * GiB)


def test_cuda_free_memory_is_used_when_lower(gpus):
    gpus["devices"] = [("aaaa-1", 5 * GiB, 8 * GiB)]
    gpus["smi"] = "GPU-aaaa-1, 6000\n"

    assert device.cuda_job_capacities()[0][2] == 5 * GiB


def test_cuda_free_memory_is_used_without_nvidia_smi(gpus):
    gpus["devices"] = [("aaaa-1", 7126 * MiB, 8 * GiB)]
    gpus["smi_error"] = FileNotFoundError("nvidia-smi")

    assert device.cuda_job_capacities()[0][2] == 7126 * MiB


def test_gpus_are_matched_by_uuid_not_index(gpus):
    gpus["devices"] = [("aaaa-1", 10 * GiB, 12 * GiB), ("bbbb-2", 10 * GiB, 12 * GiB)]
    gpus["smi"] = "GPU-bbbb-2, 2048\nGPU-aaaa-1, 9216\n"

    capacities = device.cuda_job_capacities()

    assert [free for _, _, free, _ in capacities] == [9 * GiB, 2 * GiB]


def test_the_capacity_is_the_total_vrams_whatever_is_free_at_start(gpus):
    # Another program uses nearly all of the GPU as the worker starts.
    gpus["devices"] = [("aaaa-1", 1 * GiB, 12 * GiB)]
    gpus["smi"] = "GPU-aaaa-1, 1024\n"

    # floor((12 - 0.25 - 1.0 - 0.25) / 1.1) = 9
    assert device.cuda_job_capacities()[0][1] == 9


def test_no_cuda(gpus):
    assert device.cuda_job_capacities() is None


def test_enforce_cpu_ignores_cuda(default_settings, gpus):
    default_settings.worker.enforce_cpu = True
    gpus["available"] = True

    assert device.cuda_job_capacities() is None


def test_launcher_summary_shows_total_and_free_memory(gpus, monkeypatch):
    gpus["devices"] = [("aaaa-1", 7126 * MiB, 8 * GiB)]
    gpus["smi"] = "GPU-aaaa-1, 5812\n"

    assert launcher.gpu_capacity(device.cuda_job_capacities()) == (
        1, 5, "GPU 0: up to 5 job(s), batch 5, 8.00 GiB, 5.68 GiB free now")


def test_launcher_uses_cpu_when_the_gpu_is_too_small_for_one_job(gpus, monkeypatch):
    gpus["devices"] = [("aaaa-1", 1 * GiB, 2 * GiB)]
    gpus["smi"] = "GPU-aaaa-1, 1000\n"

    assert launcher.gpu_capacity(device.cuda_job_capacities()) is None


# --- piano arrangements -----------------------------------------------------------

def test_arrangement_fits_at_its_threshold(default_settings):
    worker = default_settings.worker
    worker.arrangement = True
    # Reserves: 1.0 models + 0.25 arrangement models + 0.25 tempo map = 1.5 GiB.
    assert device.gpu_budget_gib(3 * GiB) == pytest.approx(1.5)
    threshold = (1.5 + worker.arrangement_memory_gib) * GiB

    assert device.arrangement_fits(threshold)
    assert device.arrangement_fits(threshold + 1)
    assert not device.arrangement_fits(threshold - 0.01 * GiB)
    assert not device.arrangement_fits(0)


def test_arrangement_never_fits_when_it_is_off(default_settings):
    default_settings.worker.arrangement = False
    assert not device.arrangement_fits(80 * GiB)


def test_the_arrangement_models_and_the_tempo_map_shrink_the_budget(default_settings):
    worker = default_settings.worker
    worker.arrangement = False
    assert device.gpu_budget_gib(4 * GiB) == pytest.approx(2.75)
    worker.arrangement = True
    assert device.gpu_budget_gib(4 * GiB) == pytest.approx(2.5)
    worker.tempo_map = False
    assert device.gpu_budget_gib(4 * GiB) == pytest.approx(2.75)
    worker.gpu_reserve_gib = 2.0
    assert device.gpu_budget_gib(4 * GiB) == pytest.approx(2.0)
    assert device.gpu_budget_gib(1 * GiB) == 0.0


def test_job_capacity_counts_whole_jobs_in_the_budget(default_settings):
    worker = default_settings.worker
    worker.arrangement = True
    worker.memory_per_job_gib = 1.0
    worker.tempo_map = False

    assert device.job_capacity((1.25 + 3) * GiB) == 3
    assert device.job_capacity((1.25 + 3.9) * GiB) == 3
    assert device.job_capacity(1 * GiB) == 0


# --- The launcher's measurement, handed to Dramatiq ---------------------------------------

@pytest.mark.parametrize("text", [None, "", "x", "0=", "0=5", "0=-5:8", "0=5:-8", "=1:2", "0=1:2:3"])
def test_launcher_capacities_without_a_usable_measurement(default_settings, monkeypatch, text):
    if text is None:
        monkeypatch.delenv(device.MEASURED_VARIABLE, raising=False)
    else:
        monkeypatch.setenv(device.MEASURED_VARIABLE, text)

    # The worker then measures itself, as before.
    assert device.launcher_capacities() is None


def test_launcher_capacities_give_each_gpus_most_jobs(default_settings, monkeypatch):
    worker = default_settings.worker
    total = int((device.GPU_CONTEXT_GIB + device.gpu_reserve_gib(worker) + device.TEMPO_MAP_MEMORY_GIB
                 + 3 * worker.memory_per_job_gib) * 1024**3) + 1
    monkeypatch.setenv(device.MEASURED_VARIABLE, f"0={GiB}:{total},1=0:0")

    assert device.launcher_capacities() == [(0, 3, GiB, total), (1, 0, 0, 0)]


def test_launcher_capacities_round_trip(default_settings, monkeypatch):
    measured = [(0, device.most_jobs(12 * 1024**3), 11 * 1024**3, 12 * 1024**3)]
    monkeypatch.setenv(device.MEASURED_VARIABLE, device.measured_text(measured))

    assert device.launcher_capacities() == measured


def test_launcher_capacities_ignored_with_enforce_cpu(default_settings, monkeypatch):
    default_settings.worker.enforce_cpu = True
    monkeypatch.setenv(device.MEASURED_VARIABLE, "0=12000000000:12000000000")

    assert device.launcher_capacities() is None


# --- The room for jobs, read live -----------------------------------------------------------

class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def room_budget(free_gib, reserved_gib=0.0, own_gib=device.OWN_MEMORY_GIB):
    """What GpuRoom should read: free VRAM and the worker's own memory, less
    its context and the reserves."""
    return free_gib + reserved_gib + own_gib - device.OWN_CONTEXT_GIB - 1.0 - device.TEMPO_MAP_MEMORY_GIB


def test_the_room_is_what_other_programs_leave(gpus):
    gpus["devices"] = [("aaaa-1", 6 * GiB, 8 * GiB)]
    gpus["smi"] = "GPU-aaaa-1, 4096\n"

    room = device.GpuRoom({0: 8 * GiB}, clock=Clock())

    # nvidia-smi's lower reading: programs on Windows use 2 GiB more.
    assert room.budget_gib() == pytest.approx(room_budget(4))


def test_the_workers_own_memory_is_room_for_its_jobs(gpus):
    # The worker's PyTorch holds 3 GiB, of running jobs and memory it keeps
    # after a step: counted as the worker's, not as another program's.
    gpus["devices"] = [("aaaa-1", 2 * GiB, 8 * GiB)]
    gpus["reserved"] = {0: 3 * GiB}

    room = device.GpuRoom({0: 8 * GiB}, clock=Clock())

    assert room.budget_gib() == pytest.approx(room_budget(2, 3))


def test_the_room_is_never_more_than_the_gpu_holds(gpus):
    gpus["devices"] = [("aaaa-1", 8 * GiB, 8 * GiB)]
    gpus["reserved"] = {0: 2 * GiB}

    room = device.GpuRoom({0: 8 * GiB}, clock=Clock())

    assert room.budget_gib() == pytest.approx(room.most_gib())
    assert room.most_gib() == pytest.approx(8 - device.GPU_CONTEXT_GIB - 1.0 - device.TEMPO_MAP_MEMORY_GIB)


def test_the_room_of_several_gpus_is_their_sum(gpus):
    gpus["devices"] = [("aaaa-1", 4 * GiB, 8 * GiB), ("bbbb-2", 3 * GiB, 8 * GiB)]

    room = device.GpuRoom({0: 8 * GiB, 1: 8 * GiB}, clock=Clock())

    assert room.budget_gib() == pytest.approx(room_budget(4) + room_budget(3))


def test_a_full_gpu_leaves_no_room(gpus):
    gpus["devices"] = [("aaaa-1", 0, 8 * GiB)]

    assert device.GpuRoom({0: 8 * GiB}, clock=Clock()).budget_gib() == 0.0


def test_the_room_is_read_again_once_its_reading_is_old(gpus):
    clock = Clock()
    gpus["devices"] = [("aaaa-1", 6 * GiB, 8 * GiB)]
    room = device.GpuRoom({0: 8 * GiB}, max_age=1.0, clock=clock)
    first = room.budget_gib()

    # Another program takes 3 GiB: unseen within max_age, seen after it.
    gpus["devices"] = [("aaaa-1", 3 * GiB, 8 * GiB)]
    clock.now += 0.5
    assert room.budget_gib() == first
    clock.now += 0.5
    assert room.budget_gib() == pytest.approx(first - 3)


def broken(index):
    raise RuntimeError("CUDA error")


def test_a_failed_reading_keeps_the_last_one(gpus, monkeypatch):
    clock = Clock()
    gpus["devices"] = [("aaaa-1", 6 * GiB, 8 * GiB)]
    room = device.GpuRoom({0: 8 * GiB}, clock=clock)
    first = room.budget_gib()

    monkeypatch.setattr(sys.modules["torch"].cuda, "mem_get_info", broken)
    clock.now += 5

    assert room.budget_gib() == first


def test_without_any_reading_the_room_is_what_the_gpu_holds(gpus, monkeypatch):
    monkeypatch.setattr(sys.modules["torch"].cuda, "mem_get_info", broken)
    room = device.GpuRoom({0: 8 * GiB}, clock=Clock())

    assert room.budget_gib() == pytest.approx(room.most_gib())


def test_the_workers_memory_beside_pytorchs_is_measured_once_its_models_are_loaded(gpus):
    # 7 GiB free before the worker; its models (0.25 GiB in PyTorch) and
    # 0.4 GiB of context and kernels leave 6.35.
    gpus["devices"] = [("aaaa-1", int(6.35 * GiB), 8 * GiB)]
    gpus["reserved"] = {0: int(0.25 * GiB)}
    room = device.GpuRoom({0: 8 * GiB}, clock=Clock(), free_at_start={0: 7 * GiB})
    before = room.budget_gib()

    room.measure_own_memory()

    assert room.own_memory[0] == pytest.approx(0.4 * GiB, abs=1024)
    assert room.budget_gib() == pytest.approx(room_budget(6.35, 0.25, 0.4))
    assert room.budget_gib() > before


@pytest.mark.parametrize("free_now_gib, expected_gib", [
    (7.0, device.OWN_CONTEXT_GIB),          # another program freed memory meanwhile
    (3.0, device.OWN_CONTEXT_GIB + 1.0),    # another program took memory meanwhile
])
def test_the_measured_memory_is_kept_within_bounds(gpus, free_now_gib, expected_gib):
    gpus["devices"] = [("aaaa-1", int(free_now_gib * GiB), 8 * GiB)]
    room = device.GpuRoom({0: 8 * GiB}, clock=Clock(), free_at_start={0: 7 * GiB})

    room.measure_own_memory()

    assert room.own_memory[0] == pytest.approx(expected_gib * GiB)


def test_without_the_launchers_reading_the_workers_memory_is_assumed(gpus):
    gpus["devices"] = [("aaaa-1", 6 * GiB, 8 * GiB)]
    room = device.GpuRoom({0: 8 * GiB}, clock=Clock())

    room.measure_own_memory()

    assert room.own_memory[0] == device.OWN_MEMORY_GIB * GiB
