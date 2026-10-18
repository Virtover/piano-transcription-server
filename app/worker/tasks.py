import json
import logging
import shutil
import threading
import time
from pathlib import Path
from typing import Callable

import dramatiq
from dramatiq.brokers.redis import RedisBroker
from redis import Redis
from redis.exceptions import WatchError

from app.config import settings
from app.billing import Billing, cancelled_cost
from app.outsource import routing
from app.transcription.audio import remove_audio_files
from app.transcription.cancellation import TranscriptionCancelled


JOB_TTL = 60 * 60  # 1 hour
# Uploaded files are stored in the job directory under this name.
UPLOAD_FILE_NAME = "upload.input"
LOCAL_QUEUE = "default"
REMOTE_QUEUE = "remote"

logger = logging.getLogger(__name__)


broker = RedisBroker(
    url=settings.service.redis_url,
)

dramatiq.set_broker(broker)


redis = Redis.from_url(
    settings.service.redis_url,
    decode_responses=True,
)
billing = Billing()


def job_key(job_id: str) -> str:
    return f"transcription:{job_id}"


def update_job(job_id: str, **values):
    redis.hset(
        job_key(job_id),
        mapping={
            key: json.dumps(value)
            if not isinstance(value, str)
            else value
            for key, value in values.items()
        },
    )


def is_job_cancelled(job_id: str) -> bool:
    return redis.hget(job_key(job_id), "status") != "processing"


def claim_job(job_id: str) -> bool:
    key = job_key(job_id)

    while True:
        try:
            with redis.pipeline() as pipe:
                pipe.watch(key)
                if pipe.hget(key, "status") != "queued":
                    return False
                pipe.multi()
                # Progress is kept: a job moved between backends continues
                # from what it showed.
                pipe.hset(key, "status", "processing")
                pipe.execute()
                return True
        except WatchError:
            continue


def complete_job(job_id: str, result: Path) -> bool:
    key = job_key(job_id)

    while True:
        try:
            with redis.pipeline() as pipe:
                pipe.watch(key)
                status = pipe.hget(key, "status")
                if status == "cancelled":
                    raise TranscriptionCancelled()
                if status != "processing":
                    return False

                pipe.multi()
                pipe.hset(
                    key,
                    mapping={
                        "status": "completed",
                        "progress": "1.0",
                        "result": str(result),
                    },
                )
                pipe.expire(key, JOB_TTL)
                pipe.execute()
                return True
        except WatchError:
            continue


def cleanup_expired_jobs():
    jobs_dir = Path(settings.service.data_dir) / "jobs"

    if not jobs_dir.exists():
        return

    for job_dir in jobs_dir.iterdir():
        if not job_dir.is_dir():
            continue

        job_id = job_dir.name

        if not redis.exists(job_key(job_id)):
            shutil.rmtree(job_dir)
            print(f"Removed expired job: {job_id}")


def delete_user_jobs(user_id: str) -> None:
    """Remove every job and result file owned by user_id. A running worker
    sees the missing job as cancelled and stops."""
    for key in redis.scan_iter(match="transcription:*"):
        if redis.hget(key, "user_id") != user_id:
            continue
        redis.delete(key)
        shutil.rmtree(
            Path(settings.service.data_dir)
            / "jobs"
            / key.removeprefix("transcription:"),
            ignore_errors=True,
        )


def delete_account(user_id: str) -> bool:
    """Delete a user's jobs, files, and billing data. Jobs go first: a running
    worker then sees its job as cancelled and stops without charging."""
    delete_user_jobs(user_id)
    return billing.delete_user(user_id)


def reconcile_billing() -> None:
    """Finish billing transitions left incomplete by a worker interruption."""
    for job_id in billing.reserved_job_ids():
        job = redis.hgetall(job_key(job_id))

        if not job:
            billing.release(job_id)
            continue

        status = job.get("status")
        if status == "completed":
            billing.settle_success(job_id)
        elif status == "failed":
            billing.release(job_id)
        elif status == "cancelled":
            try:
                full_cost = int(job.get("full_cost", 0))
                progress = float(job.get("progress", 0))
            except (TypeError, ValueError):
                continue
            billing.settle_cancellation(
                job_id,
                cancelled_cost(full_cost, progress),
            )


def fail_active_jobs(reason: str) -> None:
    for key in redis.scan_iter(match="transcription:*"):
        while True:
            try:
                with redis.pipeline() as pipe:
                    pipe.watch(key)
                    job = pipe.hgetall(key)
                    if job.get("status") not in {"queued", "processing"}:
                        break
                    pipe.multi()
                    pipe.hset(
                        key,
                        mapping={
                            "status": "failed",
                            "error": reason,
                        },
                    )
                    pipe.expire(key, JOB_TTL)
                    pipe.execute()
                    if job.get("user_id"):
                        billing.release(key.removeprefix("transcription:"))
                    shutil.rmtree(
                        Path(settings.service.data_dir)
                        / "jobs"
                        / key.removeprefix("transcription:"),
                        ignore_errors=True,
                    )
                    break
            except WatchError:
                continue
    # Every queued and processing job is now failed.
    routing.clear_active(redis)


class JobContext:
    """What a backend needs while transcribing one job: its directory, and
    progress, metadata, and cancellation hooks. Progress never goes below what
    the job already showed, so a job moved between backends does not appear
    to restart."""

    def __init__(self, job_id: str, output_dir: Path, progress_floor: float = 0.0):
        self.job_id = job_id
        self.output_dir = output_dir
        self.progress_floor = progress_floor

    def is_cancelled(self) -> bool:
        return is_job_cancelled(self.job_id)

    def progress(self, value: float) -> None:
        if self.is_cancelled():
            raise TranscriptionCancelled()
        if value > self.progress_floor:
            update_job(self.job_id, progress=str(value))

    def metadata(self, value: dict) -> None:
        if self.is_cancelled():
            raise TranscriptionCancelled()
        update_job(self.job_id, title=value.get("title"), metadata=value)

    def set(self, **values: str) -> None:
        update_job(self.job_id, **values)


class AverageDuration:
    """Rolling average of local job durations, published in the heartbeat."""

    def __init__(self, weight: float = 0.2):
        self.weight = weight
        self.value: float | None = None
        self._lock = threading.Lock()

    def add(self, seconds: float) -> None:
        with self._lock:
            if self.value is None:
                self.value = seconds
            else:
                self.value = (1 - self.weight) * self.value + self.weight * seconds


local_job_duration = AverageDuration()


def job_dir(job_id: str) -> Path:
    return Path(settings.service.data_dir) / "jobs" / job_id


def run_job(
    job_id: str,
    backend: routing.Backend,
    transcribe: Callable[[JobContext], Path],
) -> None:
    """The lifecycle shared by local and remote jobs: claim, transcribe,
    complete, and settle, release, or requeue the billing reservation.

    Every billing call is idempotent per job, so a job is charged at most
    once whichever path it takes.
    """
    output_dir = job_dir(job_id)
    requeued = False
    try:
        if not claim_job(job_id):
            return
        started = time.monotonic()
        output_dir.mkdir(parents=True, exist_ok=True)
        try:
            floor = float(redis.hget(job_key(job_id), "progress") or 0)
        except ValueError:
            floor = 0.0
        context = JobContext(job_id, output_dir, floor)

        midi_path = transcribe(context)

        if is_job_cancelled(job_id):
            raise TranscriptionCancelled()

        if not complete_job(job_id, midi_path):
            return
        billing.settle_success(job_id)
        remove_audio_files(output_dir)
        if backend == "local":
            local_job_duration.add(time.monotonic() - started)

    except TranscriptionCancelled:
        finish_cancelled(job_id, output_dir)
    except Exception as e:
        target = retry_backend(job_id, backend, e)
        if target and requeue_job(job_id, backend, target, str(e)):
            requeued = True
            return
        job = redis.hgetall(job_key(job_id))
        if job.get("status") == "cancelled":
            # Cancelled while failing: keep it cancelled.
            finish_cancelled(job_id, output_dir)
            return
        if job.get("user_id"):
            billing.release(job_id)
        update_job(
            job_id,
            status="failed",
            error=str(e),
        )
        remove_audio_files(output_dir)

        redis.expire(
            job_key(job_id),
            JOB_TTL,
        )
    finally:
        if not requeued:
            routing.mark_finished(redis, backend, job_id)


def finish_cancelled(job_id: str, output_dir: Path) -> None:
    job = redis.hgetall(job_key(job_id))
    if job.get("status") == "failed":
        shutil.rmtree(output_dir, ignore_errors=True)
        return
    if job.get("user_id"):
        charged = int(job.get("full_cost", 0))
        progress = float(job.get("progress", 0))
        billing.settle_cancellation(job_id, cancelled_cost(charged, progress))
    shutil.rmtree(output_dir, ignore_errors=True)
    update_job(
        job_id,
        status="cancelled",
    )
    redis.expire(
        job_key(job_id),
        JOB_TTL,
    )


def retry_backend(job_id: str, backend: routing.Backend, error: Exception) -> routing.Backend | None:
    """Where a failed job can run instead, if anywhere: a failed remote job
    goes back to the local queue (OUTSOURCE_FALLBACK_LOCAL), and a local job
    that does not fit in GPU memory goes remote once, if outsourcing is on."""
    from app.outsource.provider import RemoteFailed
    from app.transcription.batching import GpuOutOfMemory

    if backend == "remote":
        if isinstance(error, RemoteFailed) and settings.outsource.fallback_local:
            return "local"
        return None
    if (
        isinstance(error, GpuOutOfMemory)
        and routing.outsourcing_enabled()
        and redis.hget(job_key(job_id), "outsourced") != "1"
        and routing.reserve_remote_slot(redis, job_id)
    ):
        return "remote"
    return None


def requeue_job(job_id: str, source: routing.Backend, target: routing.Backend, reason: str) -> bool:
    """Put a processing job back in another backend's queue, keeping its
    billing reservation. The `outsourced` flag stops a job from being sent
    remote twice. Returns False if the job is no longer processing, for
    example because it was cancelled."""
    key = job_key(job_id)
    while True:
        try:
            with redis.pipeline() as pipe:
                pipe.watch(key)
                job = pipe.hgetall(key)
                if job.get("status") != "processing":
                    if target == "remote":
                        routing.mark_finished(redis, "remote", job_id)
                    return False
                pipe.multi()
                pipe.hset(key, mapping={"status": "queued", "backend": target, "outsourced": "1"})
                pipe.execute()
                break
        except WatchError:
            continue

    routing.move(redis, job_id, source, target)
    logger.warning("Job %s moved from %s to %s: %s", job_id, source, target, reason)
    try:
        send_job(job_id, target, *job_arguments(job))
    except Exception:
        logger.exception("Could not requeue job %s", job_id)
        update_job(job_id, status="processing", backend=source)
        routing.move(redis, job_id, target, source)
        return False
    return True


def job_arguments(job: dict[str, str]) -> tuple:
    """Actor arguments that recreate a job from its stored fields."""
    if job.get("source") == "upload":
        return (None, job.get("upload_title") or job.get("title"))
    return (job.get("source_url"),)


def send_job(job_id: str, backend: routing.Backend, *arguments, **options) -> None:
    actor = transcribe_job if backend == "local" else transcribe_remote_job
    actor.send(job_id, *arguments, **options)


@dramatiq.actor(max_retries=0)
def transcribe_job(
    job_id: str,
    source_url: str | None = None,
    upload_title: str | None = None,
):
    """Transcribe a video URL, or the uploaded file in the job directory
    when source_url is None, on this machine's GPU or CPU."""

    def transcribe(context: JobContext) -> Path:
        from app.transcription.pipeline import transcribe_source

        return transcribe_source(
            source_url=source_url,
            upload_path=None if source_url else context.output_dir / UPLOAD_FILE_NAME,
            upload_title=upload_title,
            output_dir=context.output_dir,
            progress_callback=context.progress,
            metadata_callback=context.metadata,
            cancellation_callback=context.is_cancelled,
        )

    run_job(job_id, "local", transcribe)


@dramatiq.actor(
    queue_name=REMOTE_QUEUE,
    max_retries=0,
    # The dispatcher enforces OUTSOURCE_TIMEOUT itself; this only stops a
    # stuck download or upload.
    time_limit=int((settings.outsource.timeout + 60 * 60) * 1000),
)
def transcribe_remote_job(
    job_id: str,
    source_url: str | None = None,
    upload_title: str | None = None,
):
    """Download and convert here, then run the model on the remote provider."""
    from app.outsource.remote import transcribe_remotely

    def transcribe(context: JobContext) -> Path:
        return transcribe_remotely(
            context,
            source_url=source_url,
            upload_path=None if source_url else context.output_dir / UPLOAD_FILE_NAME,
            upload_title=upload_title,
        )

    run_job(job_id, "remote", transcribe)


class LocalWorkerHeartbeat(dramatiq.Middleware):
    """Publishes the worker's capacity and average job duration while it
    consumes the local queue, so the API can estimate the local wait."""

    interval = 15

    def __init__(self):
        self._stop: threading.Event | None = None

    def after_worker_boot(self, broker, worker):
        queues = getattr(worker, "queues", None)
        if queues is not None and LOCAL_QUEUE not in queues:
            return
        capacity = worker.worker_threads
        stop = self._stop = threading.Event()

        def beat():
            while not stop.is_set():
                try:
                    routing.publish_heartbeat(redis, capacity, local_job_duration.value)
                except Exception:
                    logger.exception("Could not publish the worker heartbeat")
                stop.wait(self.interval)

        threading.Thread(target=beat, name="worker-heartbeat", daemon=True).start()

    def before_worker_shutdown(self, broker, worker):
        if self._stop is not None:
            self._stop.set()
            try:
                routing.clear_heartbeat(redis)
            except Exception:
                logger.exception("Could not clear the worker heartbeat")


class RemoteCapacityRefresh(dramatiq.Middleware):
    """With OUTSOURCE_MAX_CONCURRENCY=auto, rereads the provider's max
    workers while the remote queue is consumed, so console changes apply."""

    def after_worker_boot(self, broker, worker):
        queues = getattr(worker, "queues", None)
        if queues is None or REMOTE_QUEUE not in queues or settings.outsource.max_concurrency is not None:
            return

        def refresh():
            from app.outsource import capacity
            from app.outsource.provider import get_provider

            while True:
                time.sleep(capacity.REFRESH_INTERVAL)
                try:
                    capacity.refresh_max_workers(redis, get_provider())
                except Exception:
                    logger.exception("Could not refresh the remote capacity")

        threading.Thread(target=refresh, name="remote-capacity", daemon=True).start()


broker.add_middleware(LocalWorkerHeartbeat())
broker.add_middleware(RemoteCapacityRefresh())
