import json
import logging
import os
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Callable

import dramatiq
from dramatiq.brokers.redis import RedisBroker
from redis import Redis
from redis.exceptions import WatchError

from app import error_report, job_limits
from app.job_storage import WORK_FOLDER, delete_job_files, job_dir, s3, s3_key, storage, workspace_job_id
from app.config import settings
from app.billing import Billing, cancelled_cost
from app.errors import GENERIC_MESSAGE, detail, error_code, public_message
from app.outsource import routing
from app.transcription.audio import check_midi
from app.errors import UserFacingError
from app.transcription.cancellation import TranscriptionCancelled


# Uploaded files are stored in the job directory under this name.
UPLOAD_FILE_NAME = "upload.input"
LOCAL_QUEUE = "default"
REMOTE_QUEUE = "remote"
# A worker renews its job's lease while it runs. A processing job whose lease
# has expired lost its worker, and is failed and refunded.
LEASE_TTL = 60
LEASE_RENEW_INTERVAL = 15
ABANDONED_JOB_CHECK_INTERVAL = 30
WORKER_STOPPED = "Transcription interrupted because its worker stopped"
# The error_code of a job failed by a restart or a stopped worker.
INTERRUPTED = "transcription_interrupted"
INVALID_RESULT = "result_unavailable"

logger = logging.getLogger(__name__)

# Dramatiq's processes import this module; the API and cleanup install reporting themselves.
if os.environ.get(error_report.SERVICE_ENVIRONMENT):
    error_report.install(os.environ[error_report.SERVICE_ENVIRONMENT])


# A client rather than a URL: Dramatiq ignores a password given beside one.
broker = RedisBroker(
    client=Redis.from_url(settings.service.redis_url, **settings.service.redis_options()),
)

dramatiq.set_broker(broker)


redis = Redis.from_url(
    settings.service.redis_url,
    decode_responses=True,
    **settings.service.redis_options(),
)
billing = Billing()


def job_ttl() -> int:
    """Seconds a finished job, and its MIDI file, stay retrievable."""
    return int(settings.limits.result_retention)


def job_key(job_id: str) -> str:
    return f"transcription:{job_id}"


def lease_key(job_id: str) -> str:
    return f"transcription-lease:{job_id}"


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


def record_job(job_id: str) -> None:
    """Save the job's current state to its diagnostics record, which is kept
    after the Redis job expires. Never fails the caller."""
    try:
        job = redis.hgetall(job_key(job_id))
        if job:
            billing.record_job(job_id, job)
    except Exception:
        logger.exception("Could not record diagnostics for job %s", job_id)


def is_job_cancelled(job_id: str) -> bool:
    return redis.hget(job_key(job_id), "status") != "processing"


def claim_job(job_id: str, lease_token: str = "claimed") -> bool:
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
                # Set together, so no one sees the job processing without a lease.
                pipe.set(lease_key(job_id), lease_token, ex=LEASE_TTL)
                pipe.execute()
                return True
        except WatchError:
            continue


class JobLease:
    """Renews a claimed job's lease until stopped, then removes it. The token
    tells this worker's lease from that of a worker the job moved to."""

    def __init__(self, job_id: str):
        self.job_id = job_id
        self.token = uuid.uuid4().hex
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._renew, name=f"lease-{job_id}", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _renew(self) -> None:
        while not self._stop.wait(LEASE_RENEW_INTERVAL):
            self._update(renew=True)

    def stop(self) -> None:
        self._stop.set()
        self._update(renew=False)

    def _update(self, renew: bool) -> None:
        key = lease_key(self.job_id)
        try:
            with redis.pipeline() as pipe:
                pipe.watch(key)
                if pipe.get(key) not in {self.token, None}:
                    return
                pipe.multi()
                if renew:
                    pipe.set(key, self.token, ex=LEASE_TTL)
                else:
                    pipe.delete(key)
                pipe.execute()
        except WatchError:
            # Another worker took the job meanwhile; its lease is not ours.
            pass
        except Exception:
            logger.exception("Could not update the lease of job %s", self.job_id)


def complete_job(job_id: str, result: Path, reference: str | None = None) -> bool:
    """Mark the job completed with its MIDI file, checked at result, and
    stored where reference says (by default, at result itself)."""
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
                check_midi(result)

                pipe.multi()
                pipe.hset(
                    key,
                    mapping={
                        "status": "completed",
                        "progress": "1.0",
                        "result": reference or str(result),
                    },
                )
                pipe.expire(key, job_ttl())
                pipe.execute()
                return True
        except WatchError:
            continue


def cleanup_expired_jobs():
    """Remove job folders on the data volume whose job expired, and scratch
    folders left by a worker that crashed. Run by the cleanup service."""
    data_dir = Path(settings.service.data_dir)
    for folder, job_id_of in ((data_dir / "jobs", lambda path: path.name), (data_dir / WORK_FOLDER, workspace_job_id)):
        if not folder.exists():
            continue
        for path in folder.iterdir():
            if not path.is_dir():
                continue
            job_id = job_id_of(path)
            if not redis.exists(job_key(job_id)):
                shutil.rmtree(path, ignore_errors=True)
                print(f"Removed expired job: {job_id}")


def cleanup_stored_jobs() -> int:
    """With JOB_STORAGE=s3, delete the stored files of jobs that expired.
    Run periodically by the leading API, which has the storage keys."""
    if settings.jobs.storage != "s3":
        return 0
    removed = 0
    for job_id in list(s3().job_ids()):
        if not redis.exists(job_key(job_id)):
            s3().delete(job_id)
            removed += 1
    if removed:
        logger.info("Removed the stored files of %d expired jobs", removed)
    return removed


def delete_user_jobs(user_id: str) -> None:
    """Remove every job and result file owned by user_id. A running worker
    sees the missing job as cancelled and stops."""
    for key in redis.scan_iter(match="transcription:*"):
        if redis.hget(key, "user_id") != user_id:
            continue
        redis.delete(key)
        delete_job_files(key.removeprefix("transcription:"))
    job_limits.forget_user(redis, user_id)


def delete_account(user_id: str, actor: str | None = None, reason: str | None = None) -> bool:
    """Delete a user's jobs, files, and billing data. Jobs go first: a running
    worker then sees its job as cancelled and stops without charging. With
    an actor, an administrator's audit entry is written with the data."""
    delete_user_jobs(user_id)
    return billing.delete_user(user_id, actor, reason)


def cancel_job(job_id: str, charge: bool = True) -> str | None:
    """Cancel a queued or processing job, as its owner does; its worker stops
    at the next progress update. The reservation is settled at the
    cancellation price, or released with charge=False. Returns the status
    the job had, or None if there is no such job. Only queued and processing
    jobs change."""
    key = job_key(job_id)
    while True:
        try:
            with redis.pipeline() as pipe:
                pipe.watch(key)
                job = pipe.hgetall(key)
                status = job.get("status", "unknown") if job else None
                if status not in {"queued", "processing"}:
                    return status
                pipe.multi()
                pipe.hset(
                    key,
                    mapping={
                        "status": "cancelled",
                        "error": "Transcription cancelled",
                        "progress": job.get("progress", "0.0"),
                    },
                )
                pipe.expire(key, job_ttl())
                pipe.execute()
                break
        except WatchError:
            continue
    record_job(job_id)
    if job.get("user_id"):
        if charge:
            billing.settle_cancellation(
                job_id,
                cancelled_cost(int(job.get("full_cost", 0)), float(job.get("progress", 0))),
            )
        else:
            billing.release(job_id)
    return status


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


def job_backend(job: dict[str, str]) -> routing.Backend:
    return "remote" if job.get("backend") == "remote" else "local"


def fail_abandoned_job(job_id: str, reason: str) -> bool:
    """Fail and refund a processing job whose lease has expired: its worker
    stopped mid-job. Returns False if the job is not processing, or a worker
    still holds it."""
    key = job_key(job_id)
    while True:
        try:
            with redis.pipeline() as pipe:
                pipe.watch(key, lease_key(job_id))
                job = pipe.hgetall(key)
                if job.get("status") != "processing" or pipe.exists(lease_key(job_id)):
                    return False
                pipe.multi()
                pipe.hset(
                    key,
                    mapping={
                        "status": "failed",
                        "error": reason,
                        "error_code": INTERRUPTED,
                    },
                )
                pipe.expire(key, job_ttl())
                pipe.execute()
                break
        except WatchError:
            continue
    logger.warning("Job %s failed: %s", job_id, reason)
    record_job(job_id)
    if job.get("user_id"):
        billing.release(job_id)
    delete_job_files(job_id)
    routing.mark_finished(redis, job_backend(job), job_id)
    return True


def fail_abandoned_jobs(reason: str = WORKER_STOPPED) -> int:
    """Run periodically by the API, so a job whose worker crashed does not
    stay processing, with its minutes reserved, forever."""
    failed = 0
    for key in redis.scan_iter(match="transcription:*"):
        if redis.hget(key, "status") == "processing":
            failed += fail_abandoned_job(key.removeprefix("transcription:"), reason)
    return failed


def recover_active_jobs(reason: str) -> None:
    """Run at API startup. Processing jobs without a lease are failed and
    refunded: their worker stopped mid-job. Those with one keep running, as
    when only the API restarted. Queued jobs are sent to their queue again,
    so a planned restart does not lose them; a leftover message from before
    the restart is harmless, because only one delivery can claim the job."""
    queued = []
    running = []
    for key in redis.scan_iter(match="transcription:*"):
        job_id = key.removeprefix("transcription:")
        job = redis.hgetall(key)
        if job.get("status") == "queued":
            queued.append((job_id, job))
        elif job.get("status") == "processing" and not fail_abandoned_job(job_id, reason):
            running.append((job_id, job))
        elif job.get("status") == "completed":
            check_completed_result(job_id, job)
    # Correct the routing counts from the jobs that are still queued or
    # running. Pruning instead of clearing keeps IDs a replica added meanwhile.
    routing.prune_active(redis, job_key)
    for job_id, job in running:
        routing.mark_active(redis, job_backend(job), job_id)
    for job_id, job in queued:
        requeue_after_restart(job_id, job, reason)


def check_completed_result(job_id: str, job: dict[str, str]) -> bool:
    """Fail a completed job whose durable result is missing or invalid."""
    reference = job.get("result", "")
    try:
        key = s3_key(reference)
        if key is not None:
            if not s3().result_exists(key):
                raise OSError(f"{key} is not in the bucket")
        else:
            result = Path(reference)
            result.resolve().relative_to(Path(settings.service.data_dir).resolve())
            check_midi(result)
    except (OSError, ValueError, UserFacingError) as error:
        reason = "The completed MIDI result is missing or invalid"
        logger.error("Job %s has no usable completed result: %s", job_id, error)
        update_job(job_id, status="failed", error=reason, error_code=INVALID_RESULT)
        redis.expire(job_key(job_id), job_ttl())
        record_job(job_id)
        if job.get("user_id"):
            billing.release(job_id)
        return False
    return True


def requeue_after_restart(job_id: str, job: dict[str, str], reason: str) -> None:
    backend = job_backend(job)
    if backend == "remote" and not routing.outsourcing_enabled():
        # Outsourcing was turned off while the server was down.
        backend = "local"
        update_job(job_id, backend=backend)
    routing.mark_active(redis, backend, job_id)
    try:
        send_job(job_id, backend, *job_arguments(job))
    except Exception:
        logger.exception("Could not requeue job %s after the restart", job_id)
        routing.mark_finished(redis, backend, job_id)
        update_job(job_id, status="failed", error=reason, error_code=INTERRUPTED)
        redis.expire(job_key(job_id), job_ttl())
        record_job(job_id)
        if job.get("user_id"):
            billing.release(job_id)
        delete_job_files(job_id)


class JobContext:
    """What a backend needs while transcribing one job: its directory, and
    progress, metadata, and cancellation hooks. Progress never goes below what
    the job already showed, so a job moved between backends does not appear
    to restart."""

    def __init__(
        self,
        job_id: str,
        output_dir: Path,
        progress_floor: float = 0.0,
        max_seconds: float | None = None,
    ):
        self.job_id = job_id
        self.output_dir = output_dir
        self.progress_floor = progress_floor
        # The duration the job was charged for; audio past it is not transcribed.
        self.max_seconds = max_seconds

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
    output_dir = storage().workspace(job_id)
    requeued = False
    claimed = False
    lease = JobLease(job_id)
    try:
        if not claim_job(job_id, lease.token):
            return
        claimed = True
        lease.start()
        record_job(job_id)
        started = time.monotonic()
        output_dir.mkdir(parents=True, exist_ok=True)
        storage().fetch_inputs(job_id, output_dir)
        stored = redis.hgetall(job_key(job_id))
        try:
            floor = float(stored.get("progress") or 0)
        except ValueError:
            floor = 0.0
        try:
            max_seconds = float(stored["duration"]) if stored.get("duration") else None
        except ValueError:
            max_seconds = None
        context = JobContext(job_id, output_dir, floor, max_seconds)

        midi_path = transcribe(context)

        if is_job_cancelled(job_id):
            raise TranscriptionCancelled()

        reference = storage().save_result(job_id, midi_path)
        if not complete_job(job_id, midi_path, reference):
            return
        record_job(job_id)
        billing.settle_success(job_id)
        storage().discard_inputs(job_id, output_dir)
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
        logger.warning("Job %s failed", job_id, exc_info=e)
        if public_message(e) == GENERIC_MESSAGE:
            # No known cause such as a private video: probably a bug or a broken setup.
            error_report.report_exception(e, {"Job": job_id, "Backend": backend})
        update_job(
            job_id,
            status="failed",
            error=public_message(e),
            error_code=error_code(e),
            error_detail=detail(e),
        )
        record_job(job_id)
        storage().discard_inputs(job_id, output_dir)

        redis.expire(
            job_key(job_id),
            job_ttl(),
        )
    finally:
        if claimed:
            lease.stop()
            # With S3, the attempt's scratch folder; the kept files stay.
            storage().release(output_dir)
        # A second delivery of a job that is running elsewhere claims nothing
        # and must not remove the running job from the routing count.
        if not requeued and (claimed or redis.hget(job_key(job_id), "status") != "processing"):
            routing.mark_finished(redis, backend, job_id)


def finish_cancelled(job_id: str, output_dir: Path) -> None:
    job = redis.hgetall(job_key(job_id))
    if job.get("status") == "failed":
        shutil.rmtree(output_dir, ignore_errors=True)
        delete_job_files(job_id)
        return
    if job.get("user_id"):
        charged = int(job.get("full_cost", 0))
        progress = float(job.get("progress", 0))
        billing.settle_cancellation(job_id, cancelled_cost(charged, progress))
    shutil.rmtree(output_dir, ignore_errors=True)
    delete_job_files(job_id)
    update_job(
        job_id,
        status="cancelled",
    )
    record_job(job_id)
    redis.expire(
        job_key(job_id),
        job_ttl(),
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
                pipe.hset(
                    key,
                    mapping={"status": "queued", "backend": target, "outsourced": "1", "requeue_reason": reason},
                )
                pipe.execute()
                break
        except WatchError:
            continue
    record_job(job_id)

    routing.move(redis, job_id, source, target)
    logger.warning("Job %s moved from %s to %s: %s", job_id, source, target, reason)
    try:
        send_job(job_id, target, *job_arguments(job))
    except Exception:
        logger.exception("Could not requeue job %s", job_id)
        update_job(job_id, status="processing", backend=source)
        record_job(job_id)
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
            max_seconds=context.max_seconds,
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
        if not routing.wait_for_remote_slot(redis, job_id, context.is_cancelled):
            raise TranscriptionCancelled()
        try:
            return transcribe_remotely(
                context,
                source_url=source_url,
                upload_path=None if source_url else context.output_dir / UPLOAD_FILE_NAME,
                upload_title=upload_title,
                max_seconds=context.max_seconds,
            )
        finally:
            routing.release_remote_slot(redis, job_id)

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
