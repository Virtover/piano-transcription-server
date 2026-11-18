import logging
import os
import subprocess
import threading
import time

from app.config import gpu_reserve_gib, settings

try:
    import psutil
except ImportError:
    psutil = None


logger = logging.getLogger(__name__)

# The launcher's measurement of each GPU, "index=free:total,..." in bytes, in
# Dramatiq's environment: the worker sizes its batches and memory budget by
# it instead of measuring again beside its own CUDA context.
MEASURED_VARIABLE = "MODEL_GPU_MEMORY"

# VRAM of a GPU that the worker's jobs never get: its CUDA context (~0.13 GiB
# measured), with some headroom. What the driver keeps (~0.38 GiB of a 12 GB
# card) is already missing from the total CUDA reports. A GPU's job capacity
# is counted from its total VRAM less this, as if no other program used it;
# the room for jobs at any moment is read live (GpuRoom).
GPU_CONTEXT_GIB = 0.2
# The worker's own CUDA context (measured ~0.13 GiB), and that with the GPU
# kernels it loads (~0.14 GiB more with lazy loading): memory of the worker
# outside PyTorch's, which a live reading of free VRAM counts as used. GpuRoom
# measures it once the models are loaded, and assumes the second until then.
OWN_CONTEXT_GIB = 0.13
OWN_MEMORY_GIB = 0.27

# VRAM of the tempo map's beat tracker (app/transcription/tempo.py), which
# runs on the job's GPU one job at a time. Only its weights and one 30 s
# chunk are on the GPU, so this does not depend on the audio's length.
TEMPO_MAP_MEMORY_GIB = 0.25

def physical_cpu_count() -> int:
    detected = psutil.cpu_count(logical=False) if psutil else None
    return max(1, detected or os.cpu_count() or 1)


def automatic_cpu_threads() -> int:
    return max(1, physical_cpu_count() - 2)


def cpu_thread_count() -> int:
    """CPU threads for PyTorch and ffmpeg: WORKER_THREADS, at most the
    physical core count, or physical cores minus two for `auto`."""
    configured = settings.worker.threads
    return min(configured, physical_cpu_count()) if configured else automatic_cpu_threads()


def cpu_batch_size() -> int:
    configured = settings.worker.batch_size
    if configured is not None:
        return configured

    cpu_limit = cpu_thread_count()
    if psutil is None:
        return cpu_limit

    available_memory = psutil.virtual_memory().available
    worker = settings.worker
    # Any thread may run an arrangement when they are on.
    per_job = max(worker.cpu_memory_per_job_gib, worker.cpu_arrangement_memory_gib) if worker.arrangement \
        else worker.cpu_memory_per_job_gib
    memory_limit = int(
        max(0, available_memory - worker.cpu_reserve_gib * 1024**3)
        // (per_job * 1024**3)
    )
    return max(1, min(cpu_limit, memory_limit))


def gpu_batch_size(capacity: int) -> tuple[int, str | None]:
    """Batch size for a GPU that fits `capacity` concurrent jobs.

    `auto` uses the capacity, capped by WORKER_MAX_CONCURRENCY. A fixed
    WORKER_BATCH_SIZE is used as given, but never above the capacity; the
    second value is a warning when it was lowered.
    """
    configured = settings.worker.batch_size
    if configured is None:
        size = capacity
        if settings.worker.max_concurrency:
            size = min(size, settings.worker.max_concurrency)
        return max(1, size), None
    if configured > capacity >= 1:
        return capacity, (
            f"WORKER_BATCH_SIZE={configured} is more than this GPU's capacity of "
            f"{capacity} job(s); using {capacity}"
        )
    return configured, None


def cuda_device_names() -> dict[int, str]:
    """Each CUDA device's name, as "NVIDIA GeForce RTX 3060 Ti"; empty
    without torch or CUDA."""
    try:
        import torch

        if not torch.cuda.is_available():
            return {}
        return {index: torch.cuda.get_device_name(index) for index in range(torch.cuda.device_count())}
    except Exception:
        return {}


def cpu_model_name() -> str:
    """The processor's name, as "Intel(R) Core(TM) i7-10700K CPU @ 3.80GHz",
    or "" when it cannot be read."""
    try:
        with open("/proc/cpuinfo", encoding="utf-8") as cpuinfo:
            for line in cpuinfo:
                key, _, value = line.partition(":")
                if key.strip() == "model name" and value.strip():
                    return value.strip()
    except OSError:
        pass
    import platform

    return platform.processor() or ""


def total_ram_bytes() -> int | None:
    return psutil.virtual_memory().total if psutil else None


def nvidia_smi_free_memory() -> dict[str, int]:
    """Free memory in bytes for each GPU UUID, as reported by nvidia-smi."""
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=uuid,memory.free",
                "--format=csv,noheader,nounits",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return {}

    free_memory = {}
    for line in result.stdout.splitlines():
        try:
            uuid, free_mib = (value.strip() for value in line.split(","))
            free_memory[uuid.removeprefix("GPU-").lower()] = int(free_mib) * 1024**2
        except ValueError:
            continue
    return free_memory


def device_free_memory(
    torch,
    device_index: int,
    smi_free_memory: dict[str, int],
    log: bool = True,
) -> tuple[int, int]:
    """Free and total memory of a CUDA device.

    Under WSL, CUDA does not count memory used by Windows programs such as
    the desktop or a browser and can report more than 1 GiB too much free
    memory. nvidia-smi sees that usage, so the lower of the two is used.
    """
    free_memory, total_memory = torch.cuda.mem_get_info(device_index)
    uuid = str(torch.cuda.get_device_properties(device_index).uuid).lower()
    device_smi_free_memory = smi_free_memory.get(uuid)
    if device_smi_free_memory is not None and device_smi_free_memory < free_memory:
        if log:
            logger.info(
                "GPU %s: using nvidia-smi free memory %.2f GiB instead of CUDA's %.2f GiB",
                device_index,
                device_smi_free_memory / 1024**3,
                free_memory / 1024**3,
            )
        free_memory = device_smi_free_memory
    return free_memory, total_memory


def gpu_budget_gib(free_memory: float, config=None) -> float:
    """GiB of free_memory bytes of VRAM left for model steps, after what is
    loaded once (WORKER_GPU_RESERVE_GIB) and the beat tracker of the tempo
    map when it is on. Transcription jobs and arrangement steps share it."""
    worker = (config or settings).worker
    reserved = gpu_reserve_gib(worker) + (TEMPO_MAP_MEMORY_GIB if worker.tempo_map else 0)
    return max(0.0, free_memory / 1024**3 - reserved)


def job_capacity(free_memory: float, config=None) -> int:
    """Transcription jobs that fit in free_memory bytes of VRAM."""
    worker = (config or settings).worker
    # Rounded, so that a budget of exactly n jobs is not lost to float error.
    return int(round(gpu_budget_gib(free_memory, config) / worker.memory_per_job_gib, 9))


def usable_memory(total_memory: float) -> float:
    """Bytes of a GPU with total_memory bytes that the worker could ever use:
    all of it but its CUDA context."""
    return max(0.0, total_memory - GPU_CONTEXT_GIB * 1024**3)


def most_jobs(total_memory: float, config=None) -> int:
    """Transcription jobs a GPU with total_memory bytes holds when no other
    program uses it: its places and batch size."""
    return job_capacity(usable_memory(total_memory), config)


def arrangement_fits(free_memory: float, config=None) -> bool:
    """Whether a GPU with free_memory bytes free takes arrangement jobs: the
    feature is on and one arrangement step fits in its budget."""
    worker = (config or settings).worker
    return worker.arrangement and gpu_budget_gib(free_memory, config) + 1e-9 >= worker.arrangement_memory_gib


def cpu_budget_gib() -> float:
    """GiB of system RAM for CPU steps, after WORKER_CPU_RESERVE_GIB. Steps
    run one at a time on the CPU, so this only limits what one step takes."""
    if psutil is None:
        return float("inf")
    return max(0.0, psutil.virtual_memory().available / 1024**3 - settings.worker.cpu_reserve_gib)


def measured_text(capacities: list[tuple[int, int, int, int]]) -> str:
    return ",".join(f"{index}={free_memory}:{total_memory}" for index, _, free_memory, total_memory in capacities)


def launcher_capacities() -> list[tuple[int, int, int, int]] | None:
    """The job capacities of the launcher's measurement, as
    cuda_job_capacities gives them, or None without one."""
    text = os.environ.get(MEASURED_VARIABLE)
    if not text or settings.worker.enforce_cpu:
        return None
    capacities = []
    try:
        for item in text.split(","):
            index, memory = item.split("=")
            free_memory, total_memory = memory.split(":")
            index, free_memory, total_memory = int(index), int(free_memory), int(total_memory)
            if index < 0 or free_memory < 0 or total_memory < 0:
                return None
            capacities.append((index, most_jobs(total_memory), free_memory, total_memory))
    except ValueError:
        return None
    return capacities or None


def cuda_job_capacities() -> list[tuple[int, int, int, int]] | None:
    """(index, the most jobs it holds, free bytes now, total bytes) of each
    CUDA device, or None without CUDA."""
    if settings.worker.enforce_cpu:
        return None
    try:
        import torch

        if not torch.cuda.is_available():
            return None

        capacities = []
        smi_free_memory = nvidia_smi_free_memory()
        for device_index in range(torch.cuda.device_count()):
            free_memory, total_memory = device_free_memory(
                torch,
                device_index,
                smi_free_memory,
            )
            capacities.append(
                (
                    device_index,
                    most_jobs(total_memory),
                    free_memory,
                    total_memory,
                )
            )

        return capacities
    except (ImportError, RuntimeError):
        return None


class GpuRoom:
    """The VRAM this worker's jobs may use now on its GPUs, in GiB: what no
    other program uses, less the reserves. Read live, so the worker takes
    jobs as other programs free VRAM and stops taking them as they use more,
    without a restart.

    The worker's own memory counts as its own: PyTorch's reserved memory and
    the worker's memory beside it (its CUDA context and kernels) are added
    back to the free memory, so running jobs and the memory PyTorch keeps
    after a step never shut out the next job. Its context is then taken off,
    and the reserves, as for a GPU's capacity. app.worker.places subtracts
    what the jobs holding places will use.

    Readings are at most max_age seconds old; the GPUs are read by one
    caller at a time. If a reading fails, the last one is used, and without
    one the most the GPUs hold, as if no other program used them.
    """

    def __init__(
        self,
        totals: dict[int, int],
        max_age: float = 1.0,
        clock=time.monotonic,
        config=None,
        free_at_start: dict[int, int] | None = None,
    ):
        # GPU index -> total bytes.
        self.totals = dict(totals)
        self.max_age = max_age
        self.clock = clock
        self.config = config
        # GPU index -> free bytes before this process used CUDA (the launcher's).
        self.free_at_start = dict(free_at_start or {})
        # GPU index -> the worker's memory outside PyTorch's, in bytes.
        self.own_memory = {index: OWN_MEMORY_GIB * 1024**3 for index in self.totals}
        self._budget: float | None = None
        self._read_at = 0.0
        self._lock = threading.Lock()
        self._failed = False

    def most_gib(self) -> float:
        """The budget when no other program uses the GPUs."""
        return sum(gpu_budget_gib(usable_memory(total), self.config) for total in self.totals.values())

    def budget_gib(self) -> float:
        with self._lock:
            if self._budget is None or self.clock() - self._read_at >= self.max_age:
                try:
                    self._budget = self._read()
                    self._failed = False
                except Exception:
                    if not self._failed:
                        logger.exception("Could not read the GPUs' free memory; using the last reading")
                    self._failed = True
                    if self._budget is None:
                        self._budget = self.most_gib()
                self._read_at = self.clock()
            return self._budget

    def measure_own_memory(self) -> None:
        """Measure the worker's memory outside PyTorch's, once its models are
        loaded: the launcher's free memory, less the free memory now and
        PyTorch's, and the CUDA context, which the launcher's process had as
        it measured. Kept between the context alone and the context with the
        whole model reserve, so another program that took or freed memory
        meanwhile changes little."""
        import torch

        worker = (self.config or settings).worker
        lowest = OWN_CONTEXT_GIB * 1024**3
        highest = (OWN_CONTEXT_GIB + gpu_reserve_gib(worker)) * 1024**3
        smi_free_memory = nvidia_smi_free_memory()
        with self._lock:
            for index in self.totals:
                if index not in self.free_at_start:
                    continue
                reserved, free_memory, _ = self._reading(torch, index, smi_free_memory)
                measured = self.free_at_start[index] - free_memory - reserved + lowest
                self.own_memory[index] = min(highest, max(lowest, measured))
                logger.info("GPU %s: the worker's memory beside PyTorch's is %.2f GiB (measured %.2f GiB)",
                            index, self.own_memory[index] / 1024**3, measured / 1024**3)
            # The next caller reads again, with it.
            self._budget = None

    @staticmethod
    def _reading(torch, index: int, smi_free_memory: dict[str, int]) -> tuple[int, int, int]:
        """(PyTorch's reserved bytes, free bytes, total bytes) of a GPU.
        Reserved is read before and after the free memory, and the lower of
        the two kept: memory PyTorch takes or gives back meanwhile then never
        counts twice."""
        before = torch.cuda.memory_reserved(index)
        free_memory, total = device_free_memory(torch, index, smi_free_memory, log=False)
        after = torch.cuda.memory_reserved(index)
        return min(before, after), free_memory, total

    def _read(self) -> float:
        import torch

        smi_free_memory = nvidia_smi_free_memory()
        budget = 0.0
        for index, total in self.totals.items():
            reserved, free_memory, _ = self._reading(torch, index, smi_free_memory)
            # What other programs leave this worker, less its context.
            leaves = free_memory + reserved + self.own_memory[index] - OWN_CONTEXT_GIB * 1024**3
            budget += min(
                gpu_budget_gib(leaves, self.config),
                gpu_budget_gib(usable_memory(total), self.config),
            )
        return budget
