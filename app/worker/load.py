"""How busy this device is, for placing jobs across a home cluster's
devices (app/cluster.py): a GPU worker's utilization and free memory, from
nvidia-smi, which counts every program on the GPU, not only this server; a
CPU worker's CPU use. Sampled every few seconds and averaged, so one spike
does not move jobs. Without nvidia-smi or psutil the load is unknown, which
never moves a job."""

import os
import shutil
import subprocess
import threading
from collections import deque
from dataclasses import dataclass


SAMPLE_INTERVAL = 5.0
SAMPLES = 4


@dataclass(frozen=True)
class Load:
    # Percent, 0 to 100, or None when unknown.
    utilization: float | None
    # MiB free on the roomiest GPU, or None for a CPU worker or unknown.
    free_mib: float | None
    gpu: bool


def visible_gpus() -> list[str] | None:
    """The GPU indexes this container may use, or None for all."""
    value = os.environ.get("CUDA_VISIBLE_DEVICES") or os.environ.get("NVIDIA_VISIBLE_DEVICES")
    if not value or value in ("all", "void", "none"):
        return None
    return [item.strip() for item in value.split(",") if item.strip().isdigit()] or None


def sample_gpus(run=subprocess.run) -> tuple[float, float] | None:
    """(the busiest visible GPU's utilization, the most free MiB), or None."""
    if shutil.which("nvidia-smi") is None:
        return None
    try:
        result = run(
            ["nvidia-smi", "--query-gpu=index,utilization.gpu,memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    wanted = visible_gpus()
    rows = []
    for line in result.stdout.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) != 3:
            continue
        try:
            index, utilization, free = parts[0], float(parts[1]), float(parts[2])
        except ValueError:
            continue
        if wanted is None or index in wanted:
            rows.append((utilization, free))
    if not rows:
        return None
    # The job goes to the roomiest GPU; the device counts as busy as its busiest.
    return max(row[0] for row in rows), max(row[1] for row in rows)


def sample_cpu() -> float | None:
    try:
        import psutil
    except ImportError:
        return None
    return float(psutil.cpu_percent(interval=None))


class LoadMonitor:
    """Samples in a thread of its own; current() averages the last samples."""

    def __init__(self, gpu: bool, gpu_sampler=sample_gpus, cpu_sampler=sample_cpu):
        self.gpu = gpu
        self.gpu_sampler = gpu_sampler
        self.cpu_sampler = cpu_sampler
        self.utilization: deque[float] = deque(maxlen=SAMPLES)
        self.free: deque[float] = deque(maxlen=SAMPLES)
        self._lock = threading.Lock()
        self._stop = threading.Event()

    def sample(self) -> None:
        if self.gpu:
            sampled = self.gpu_sampler()
            if sampled is None:
                return
            utilization, free = sampled
        else:
            utilization, free = self.cpu_sampler(), None
            if utilization is None:
                return
        with self._lock:
            self.utilization.append(utilization)
            if free is not None:
                self.free.append(free)

    def current(self) -> Load:
        with self._lock:
            utilization = sum(self.utilization) / len(self.utilization) if self.utilization else None
            # The least free memory seen lately: what a new job can count on.
            free = min(self.free) if self.free else None
        return Load(utilization, free, self.gpu)

    def start(self) -> None:
        def run():
            while not self._stop.is_set():
                try:
                    self.sample()
                except Exception:
                    pass
                self._stop.wait(SAMPLE_INTERVAL)

        threading.Thread(target=run, name="load-monitor", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
