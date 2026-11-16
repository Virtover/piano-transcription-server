import json
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field

from app.config import ensure_valid, settings
from app.error_report import SERVICE_ENVIRONMENT
from app.worker.places import GPU_THREADS_PER_PLACE, PLACES_VARIABLE
from app.transcription.device import (
    arrangement_fits,
    cpu_batch_size,
    cpu_model_name,
    cpu_thread_count,
    cuda_device_names,
    cuda_job_capacities,
    gpu_batch_size,
    MEASURED_FREE_VARIABLE,
    measured_free_text,
    physical_cpu_count,
    total_ram_bytes,
)

MiB = 1024**2


# Must match the queue names in app.worker.tasks, which is not imported here
# so that this module starts without connecting to Redis.
LOCAL_QUEUE = "default"
REMOTE_QUEUE = "remote"
ARRANGEMENT_QUEUE = "arrangement"
CHECK_QUEUE = "checks"


@dataclass(frozen=True)
class WorkerPlan:
    """Resolved worker resources, computed once at startup."""

    processes: int
    threads: int
    # Set in CPU mode: threads for PyTorch, ffmpeg, OpenMP, and MKL.
    cpu_threads: int | None
    summary: str
    warnings: tuple[str, ...] = ()
    # Whether it consumes the arrangement queue (WORKER_ARRANGEMENT, and room on a GPU).
    arrangement: bool = False
    # Local jobs before or in their model step at once (app.worker.places);
    # None for the remote dispatcher.
    places: int | None = None
    # What the jobs run on, for the admin page and `admin.py nodes`.
    hardware: dict = field(default_factory=dict)
    # The GPUs' free VRAM as measured here, for Dramatiq (MEASURED_FREE_VARIABLE).
    measured_free: str = ""

    def environment(self) -> dict[str, str]:
        """What Dramatiq's process needs in its environment for this plan."""
        environment = {}
        if self.cpu_threads:
            # Read by the OpenMP and MKL libraries themselves.
            environment.update(OMP_NUM_THREADS=str(self.cpu_threads), MKL_NUM_THREADS=str(self.cpu_threads))
        if self.places:
            environment[PLACES_VARIABLE] = str(self.places)
            # Dramatiq's default, twice the threads per queue, would hold
            # jobs here that a free device elsewhere could run.
            environment["dramatiq_queue_prefetch"] = str(self.threads)
        if self.measured_free:
            environment[MEASURED_FREE_VARIABLE] = self.measured_free
        return environment


# Run by measure_gpus in a process of its own: (capacities, names) as JSON.
MEASURE_SCRIPT = """
import json
from app.transcription.device import cuda_device_names, cuda_job_capacities
print(json.dumps({"capacities": cuda_job_capacities(), "names": cuda_device_names()}))
"""


def measure_gpus(run=subprocess.run) -> tuple[list[tuple[int, int, int, int]] | None, dict[int, str]]:
    """Each GPU's job capacity and name, measured in a process of its own:
    a process that once used CUDA holds a context of about 100 MiB of VRAM
    until it exits, which the jobs would then lack. Measured here if that
    process fails."""
    if settings.worker.enforce_cpu:
        return None, {}
    try:
        result = run([sys.executable, "-c", MEASURE_SCRIPT], capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            raise RuntimeError((result.stderr or "").strip()[-500:] or f"exit code {result.returncode}")
        data = json.loads(result.stdout.strip().splitlines()[-1])
        capacities = [tuple(int(value) for value in item) for item in data["capacities"]] \
            if data["capacities"] is not None else None
        return capacities, {int(index): str(name) for index, name in data["names"].items()}
    except Exception as error:
        print(f"Warning: could not measure the GPUs in a process of their own ({error}); measuring here",
              flush=True)
        return cuda_job_capacities(), cuda_device_names()


def gpu_capacity(
    capacities: list[tuple[int, int, int, int]] | None,
    names: dict[int, str] | None = None,
) -> tuple[int, int, str] | None:
    if not capacities:
        return None
    names = names or {}

    device_capacities = [capacity for _, capacity, _, _ in capacities]
    total_capacity = sum(device_capacities)
    if total_capacity < 1:
        return None

    parts = []
    for index, capacity, free_memory, total_memory in capacities:
        batch = f"batch {gpu_batch_size(capacity)[0]}, " if capacity > 0 else ""
        arranges = "arrangements, " if capacity > 0 and arrangement_fits(free_memory) else ""
        name = f" ({names[index]}, {total_memory / 1024**3:.2f} GiB)" if names.get(index) else ""
        parts.append(f"GPU {index}{name}: {capacity} job(s), {batch}{arranges}{free_memory / 1024**3:.2f} GiB free")
    summary = ", ".join(parts)
    return len(device_capacities), total_capacity, summary


def plan_worker(capacities: list[tuple[int, int, int, int]] | None, names: dict[int, str] | None = None) -> WorkerPlan:
    if settings.worker.enforce_cpu:
        capacities = None
    names = names or {}
    gpu = gpu_capacity(capacities, names)
    if gpu:
        device_count, gpu_jobs, gpu_summary = gpu
        warnings = tuple(
            f"GPU {index}: {warning}"
            for index, capacity, _, _ in capacities
            if capacity > 0 and (warning := gpu_batch_size(capacity)[1])
        )
        # Not capped by CPU cores: a job holds its place only up to the end
        # of its model step, which runs in one thread for the whole batch.
        places, limited_by = gpu_jobs, None
        if settings.worker.max_concurrency and settings.worker.max_concurrency < places:
            places, limited_by = settings.worker.max_concurrency, "WORKER_MAX_CONCURRENCY"
        arrangement = any(
            capacity > 0 and arrangement_fits(free_memory) for _, capacity, free_memory, _ in capacities
        )
        if settings.worker.arrangement and not arrangement:
            warnings += ("WORKER_ARRANGEMENT is on, but no GPU has room for arrangements "
                         f"(WORKER_ARRANGEMENT_MEMORY_GIB={settings.worker.arrangement_memory_gib:g} "
                         "beside the reserves); arrangement jobs go to other workers or RunPod",)
        return WorkerPlan(
            processes=1,
            # The others finish jobs past their model step (app.worker.places).
            threads=places * GPU_THREADS_PER_PLACE,
            cpu_threads=None,
            summary=f"{device_count} GPU(s), up to {gpu_jobs} job(s) by VRAM ({gpu_summary})",
            warnings=warnings,
            arrangement=arrangement,
            places=places,
            measured_free=measured_free_text(capacities),
            hardware={
                "kind": "gpu",
                "places": places,
                # Why fewer places than jobs fit in VRAM, if so.
                "limited_by": limited_by,
                "arrangements": arrangement,
                "gpus": [
                    {
                        "index": index,
                        "name": names.get(index) or "",
                        "vram_mib": round(total_memory / MiB),
                        # At the worker's start, before it loaded its models.
                        "free_mib": round(free_memory / MiB),
                        "jobs": capacity,
                        "arrangements": capacity > 0 and arrangement_fits(free_memory),
                    }
                    for index, capacity, free_memory, total_memory in capacities
                ],
            },
        )

    return WorkerPlan(
        processes=1,
        threads=cpu_batch_size(),
        cpu_threads=cpu_thread_count(),
        summary=f"{physical_cpu_count()} CPU(s), no CUDA GPU detected, batch {cpu_batch_size()}",
        arrangement=settings.worker.arrangement,
        # On the CPU, the last steps compete with the model for the same
        # cores: a thread per place.
        places=cpu_batch_size(),
        hardware={
            "kind": "cpu",
            "places": cpu_batch_size(),
            "arrangements": settings.worker.arrangement,
            "name": cpu_model_name(),
            "cores": physical_cpu_count(),
            "threads": cpu_thread_count(),
            "ram_mib": round(ram / MiB) if (ram := total_ram_bytes()) else None,
        },
    )


def remote_plan() -> WorkerPlan:
    from redis import Redis

    from app.outsource import capacity
    from app.outsource.provider import get_provider

    redis = Redis.from_url(settings.service.redis_url, decode_responses=True, **settings.service.redis_options())
    if settings.outsource.max_concurrency is None:
        capacity.refresh_max_workers(redis, get_provider())
    threads = capacity.remote_worker_threads(redis)
    limit = capacity.remote_job_limit(redis)
    mode = "auto: max workers x requests per worker" if settings.outsource.max_concurrency is None else "OUTSOURCE_MAX_CONCURRENCY"
    return WorkerPlan(
        processes=1,
        threads=threads,
        cpu_threads=None,
        summary=f"remote dispatcher for {settings.outsource.provider}, up to {limit} remote job(s) ({mode})",
    )


def idle(reason: str) -> None:
    """Wait without doing anything until the container is stopped. The
    remote-worker service always runs, so that OUTSOURCE_PROVIDER in .env is
    the only switch; exiting instead would keep it from coming back when
    Docker restarts."""
    print(reason, flush=True)
    # As PID 1 in a container, Python ignores SIGTERM unless it handles it.
    signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(0))
    while True:
        time.sleep(3600)


def main(arguments: list[str] | None = None) -> None:
    """Start Dramatiq for a role: `local` (default) runs the model on this
    machine and consumes the default queue; `remote` dispatches to the
    outsourcing provider and consumes the remote queue, without GPU
    libraries; `node` is a home cluster device's worker, which may do both
    and check links for the hub.

    With Docker Compose, on the hub or a node, a supervisor runs Dramatiq
    and follows the cluster's switches (app.worker.supervisor); elsewhere,
    as on Kubernetes, Dramatiq replaces this process."""
    arguments = sys.argv[1:] if arguments is None else arguments
    role = arguments[0] if arguments else "local"
    if role not in ("local", "remote", "node"):
        raise SystemExit(f"Unknown worker role {role!r}; use local, remote, or node")

    if role == "node" or settings.service.deployment in ("compose", "node"):
        from app.worker import supervisor

        supervisor.main(role, arguments)
        return

    if role == "remote":
        if settings.outsource.provider == "none":
            idle("Outsourcing is off (OUTSOURCE_PROVIDER=none): the remote worker is idle. "
                 "Set OUTSOURCE_PROVIDER in .env and run `docker compose up -d` to turn it on.")
            return
        ensure_valid("remote")
        plan = remote_plan()
        queues = [REMOTE_QUEUE]
    else:
        ensure_valid("worker")
        plan = plan_worker(*measure_gpus())
        queues = [LOCAL_QUEUE, ARRANGEMENT_QUEUE] if plan.arrangement else [LOCAL_QUEUE]

    # Read by app.worker.tasks in each Dramatiq process, which reports its errors.
    os.environ[SERVICE_ENVIRONMENT] = "remote-worker" if role == "remote" else "worker"
    os.environ.update(plan.environment())
    for warning in plan.warnings:
        print(f"Warning: {warning}", flush=True)
    print(
        f"Starting Dramatiq on queue(s) {', '.join(queues)} with {plan.processes} process(es) and "
        f"{plan.threads} thread(s) per process ({plan.summary})",
        flush=True,
    )

    dramatiq = shutil.which("dramatiq")
    if dramatiq is None:
        raise RuntimeError("The dramatiq executable is not installed")

    os.execv(
        dramatiq,
        [
            dramatiq,
            "app.worker.tasks",
            "--processes",
            str(plan.processes),
            "--threads",
            str(plan.threads),
            "--queues",
            *queues,
        ],
    )


if __name__ == "__main__":
    main()
