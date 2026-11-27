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

from app import clips, cluster, error_report, job_limits, variants
from app.job_storage import WORK_FOLDER, bucket, delete_job_files, job_dir, stored_result, storage, uses_bucket
from app.job_storage import workspace_job_id
from app.config import settings
from app.billing import Billing, cancelled_cost
from app.errors import GENERIC_MESSAGE, SourceTemporarilyUnavailable, detail, error_code, public_message
from app.outsource import routing
from app.transcription.audio import check_midi
from app.errors import UserFacingError
from app.transcription.cancellation import TranscriptionCancelled
from app.worker.places import Place, Places, configured as configured_places


# Uploaded files are stored in the job directory under this name.
UPLOAD_FILE_NAME = "upload.input"
LOCAL_QUEUE = "default"
REMOTE_QUEUE = "remote"
# Local ensemble jobs: only workers that take them consume this queue. Its
# name, and the mode stored in an ensemble job, are "arrangement", from when
# these jobs were piano arrangements: queued messages and stored jobs keep
# working across an update.
ENSEMBLE_QUEUE = "arrangement"
ENSEMBLE = "arrangement"
# A worker renews its job's lease while it runs. A processing job whose lease
# has expired lost its worker, and is failed and refunded.
LEASE_TTL = 60
LEASE_RENEW_INTERVAL = 15
ABANDONED_JOB_CHECK_INTERVAL = 30
# Whoever ends a job settles its billing at once. The leading API settles
# what is still reserved this long after the job ended, as after a crash.
RECONCILE_AFTER = 300
WORKER_STOPPED = "Transcription interrupted because its worker stopped"
# The error_code of a job failed by a restart or a stopped worker.
INTERRUPTED = "transcription_interrupted"
INVALID_RESULT = "result_unavailable"
# Link checks a node runs for the API while the hub's address is refused.
CHECK_QUEUE = "checks"
# A device hands a job it should not start to another, at most this often,
# this far apart; then it runs the job itself.
MAX_PASSES = 30
PASS_DELAY_MS = 2000
# A job waiting for a place on this device's models looks this often for
# another device with a free one.
PLACE_CHECK_INTERVAL = 2.0
# Moves of one job to another device's address after a video site refused one.
MAX_ADDRESS_MOVES = 3
# The attempt token of a job queued again after its worker vanished: no
# attempt holds it, so a worker that comes back stops without writing.
ABANDONED_TOKEN = "abandoned"

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
# A node never reaches the billing database: the hub settles what it ends.
billing = cluster.HubBilling(redis) if cluster.is_node() else Billing()


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


def is_job_cancelled(job_id: str, token: str | None = None) -> bool:
    """Whether the attempt should stop: the job is no longer processing, or,
    given this attempt's token, another attempt owns it now."""
    status, current = redis.hmget(job_key(job_id), "status", "attempt_token")
    return status != "processing" or (token is not None and current not in (None, token))


def superseded(job_id: str, token: str) -> bool:
    """Whether another attempt owns the job: this one's worker lost it, as
    a device that was cut off while the job was queued again elsewhere.
    Such an attempt writes nothing, settles nothing, and deletes nothing."""
    current = redis.hget(job_key(job_id), "attempt_token")
    return current is not None and current != token


def claim_job(job_id: str, lease_token: str = "claimed", backend: routing.Backend | None = None) -> bool:
    """Take a queued job for this attempt: processing, with its lease and
    token, its attempt counted, and this device added to those that ran it.
    Given the backend whose queue delivered it, a job moved to the other
    backend since is not taken: its message here is a leftover."""
    key = job_key(job_id)

    while True:
        try:
            with redis.pipeline() as pipe:
                pipe.watch(key)
                status, nodes, stored_backend = pipe.hmget(key, "status", "nodes", "backend")
                if status != "queued":
                    return False
                if backend is not None and job_backend({"backend": stored_backend}) != backend:
                    return False
                node = cluster.node_name()
                ran_on = [name for name in (nodes or "").split(",") if name]
                pipe.multi()
                # Progress is kept: a job moved between backends continues
                # from what it showed.
                pipe.hset(key, mapping={
                    "status": "processing",
                    "attempt_token": lease_token,
                    "node": node,
                    "nodes": ",".join([*ran_on, node] if node not in ran_on else ran_on),
                })
                pipe.hincrby(key, "attempt", 1)
                # Set together, so no one sees the job processing without a lease.
                pipe.set(lease_key(job_id), lease_token, ex=LEASE_TTL)
                pipe.execute()
                return True
        except WatchError:
            continue


class JobLease:
    """Renews a claimed job's lease until stopped, then removes it. The token
    tells this worker's lease from that of a worker the job moved to. Once
    another attempt owns the job, renewing stops."""

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
                # Not the job's key: progress changes it all the time, and
                # renewals must not fail for that.
                pipe.watch(key)
                if pipe.get(key) not in {self.token, None}:
                    return
                current = pipe.hget(job_key(self.job_id), "attempt_token")
                if renew and current is not None and current != self.token:
                    # The job was queued again while this worker was away.
                    self._stop.set()
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


def complete_job(job_id: str, result: Path, reference: str | None = None, token: str | None = None,
                 stored_variants: str | None = None) -> bool:
    """Mark the job completed with its MIDI file, checked at result, and
    stored where reference says (by default, at result itself). With the
    attempt's token, only while that attempt owns the job. stored_variants,
    for an ensemble job, is its variants field (app.variants.stored_field)."""
    key = job_key(job_id)

    while True:
        try:
            with redis.pipeline() as pipe:
                pipe.watch(key)
                status, current = pipe.hmget(key, "status", "attempt_token")
                if token is not None and current not in (None, token):
                    return False
                if status == "cancelled":
                    raise TranscriptionCancelled()
                if status != "processing":
                    return False
                check_midi(result)

                pipe.multi()
                mapping = {
                    "status": "completed",
                    "progress": "1.0",
                    "result": reference or str(result),
                }
                if stored_variants is not None:
                    mapping["variants"] = stored_variants
                pipe.hset(key, mapping=mapping)
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
    if not uses_bucket():
        return 0
    removed = 0
    files = bucket()
    for job_id in list(files.job_ids()):
        if not redis.exists(job_key(job_id)):
            files.delete(job_id)
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
            billing.settle_cancellation(job_id, cancellation_charge(job))
        else:
            billing.release(job_id)
    return status


def ended_recently(job_id: str, min_age: float) -> bool:
    """Whether the job ended less than min_age seconds ago, read from the
    expiry an ended job is given."""
    if min_age <= 0:
        return False
    remaining = redis.ttl(job_key(job_id))
    return remaining >= 0 and job_ttl() - remaining < min_age


def reconcile_billing(min_age: float = 0) -> None:
    """Finish billing transitions left incomplete by a worker interruption.
    With min_age, jobs that ended more recently are left to whoever ended
    them, which may be settling them now: an administrator's refund of a
    cancelled job, for one, must not be charged meanwhile."""
    for job_id in billing.reserved_job_ids():
        job = redis.hgetall(job_key(job_id))

        if job and ended_recently(job_id, min_age):
            continue
        settle_ended_job(job_id, job, billing)


def settle_ended_job(job_id: str, job: dict[str, str], ledger=None) -> bool:
    """Settle a job's reservation by the state it ended in: charged when
    completed, released when failed or gone, the cancellation price when
    cancelled. Returns False, settling nothing, for a job that has not
    ended. Every billing call is idempotent per job."""
    ledger = ledger or billing
    if not job:
        ledger.release(job_id)
        return True
    status = job.get("status")
    if status == "completed":
        ledger.settle_success(job_id)
    elif status == "failed":
        ledger.release(job_id)
    elif status == "cancelled":
        ledger.settle_cancellation(job_id, cancellation_charge(job))
    else:
        return False
    return True


def settle_node_jobs() -> int:
    """Run by the leading API: settle and record the jobs nodes ended, which
    they only name (app.cluster.HubBilling)."""
    hub_billing = Billing()

    def settle(job_id: str, job: dict[str, str]) -> bool:
        if job and not job.get("user_id"):
            return job.get("status") in {"completed", "failed", "cancelled"}
        return settle_ended_job(job_id, job, hub_billing)

    def record(job_id: str) -> None:
        job = redis.hgetall(job_key(job_id))
        if job:
            hub_billing.record_job(job_id, job)

    return cluster.process_node_requests(redis, settle, record)


def retried_by_the_server(job: dict[str, str]) -> bool:
    """Whether the server had to run the job again or move it: a device or
    worker lost mid-job, a move to another device's address, to RunPod, or
    back. Its progress is then partly from an attempt that was lost."""
    return bool(job.get("requeue_reason") or job.get("interruptions") or job.get("address_moves")
                or job.get("outsourced"))


def cancellation_charge(job: dict[str, str]) -> int:
    """What cancelling the job costs its user: a share of the work done, or
    nothing for a job the server had to retry or move, so that no one pays
    for progress an attempt lost before they gave up waiting."""
    if retried_by_the_server(job):
        return 0
    try:
        return cancelled_cost(int(job.get("full_cost", 0)), float(job.get("progress", 0)))
    except (TypeError, ValueError):
        return 0


def job_backend(job: dict[str, str]) -> routing.Backend:
    return "remote" if job.get("backend") == "remote" else "local"


def is_ensemble(job: dict[str, str]) -> bool:
    """Whether a job is an ensemble job, not a piano transcription; jobs
    stored before either mode existed are piano transcriptions. A stored
    style, from when ensemble jobs were piano arrangements with styles, is
    ignored."""
    return job.get("mode") == ENSEMBLE


def attempts_made(job: dict[str, str]) -> int:
    try:
        return max(int(job.get("attempt") or 1), 1)
    except ValueError:
        return 1


def inputs_kept(job_id: str, job: dict[str, str]) -> bool:
    """Whether another attempt has what it needs: an upload's file is still
    kept. A link is read again."""
    if job.get("source") != "upload":
        return True
    try:
        return UPLOAD_FILE_NAME in (storage().names(job_id) or [])
    except Exception:
        logger.exception("Could not look for the kept files of job %s", job_id)
        return False


def fail_abandoned_job(job_id: str, reason: str) -> bool:
    """A processing job whose lease has expired lost its worker, as to a
    crash or a device that lost power or its network. Queued again, keeping
    its reservation, until JOB_MAX_ATTEMPTS attempts were made; then failed
    and refunded. The attempt token is cleared, so the lost worker, should
    it come back, stops without writing anything. Returns False if the job
    is not processing, or a worker still holds it."""
    key = job_key(job_id)
    while True:
        try:
            with redis.pipeline() as pipe:
                pipe.watch(key, lease_key(job_id))
                job = pipe.hgetall(key)
                if job.get("status") != "processing" or pipe.exists(lease_key(job_id)):
                    return False
                retry = attempts_made(job) < settings.jobs.max_attempts and inputs_kept(job_id, job)
                pipe.multi()
                if retry:
                    pipe.hset(key, mapping={
                        "status": "queued",
                        "attempt_token": ABANDONED_TOKEN,
                        "requeue_reason": reason,
                        "queued_at": str(time.time()),
                    })
                    pipe.hincrby(key, "interruptions", 1)
                else:
                    pipe.hset(
                        key,
                        mapping={
                            "status": "failed",
                            "error": reason,
                            "error_code": INTERRUPTED,
                            "attempt_token": ABANDONED_TOKEN,
                        },
                    )
                    pipe.expire(key, job_ttl())
                pipe.execute()
                break
        except WatchError:
            continue
    if retry:
        logger.warning("Job %s lost its worker (%s); queued again, attempt %d of %d",
                       job_id, reason, attempts_made(job) + 1, settings.jobs.max_attempts)
        record_job(job_id)
        backend = job_backend(job)
        routing.mark_active(redis, backend, job_id)
        try:
            send_job(job_id, backend, *job_arguments(job))
            return True
        except Exception:
            logger.exception("Could not queue job %s again", job_id)
            fail_unqueued_job(job_id, job, backend, reason)
            return True
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


# A queued job waiting this long, with no message sent for it this long,
# gets its message again: one may have been lost, as with a device that
# failed a message. Only one delivery can claim a job, so a second is harmless.
STALE_QUEUED = 600


def resend_stale_queued(now: float | None = None) -> int:
    """Run periodically by the leading API."""
    now = time.time() if now is None else now
    resent = 0
    for key in redis.scan_iter(match="transcription:*"):
        job = redis.hgetall(key)
        if job.get("status") != "queued":
            continue
        try:
            last = max(float(job.get("queued_at") or job.get("created_at") or now), float(job.get("resent_at") or 0))
        except ValueError:
            continue
        if now - last < STALE_QUEUED:
            continue
        job_id = key.removeprefix("transcription:")
        redis.hset(key, "resent_at", str(now))
        try:
            send_job(job_id, job_backend(job), *job_arguments(job))
            resent += 1
        except Exception:
            logger.exception("Could not send the message of queued job %s again", job_id)
    if resent:
        logger.warning("Sent the messages of %d long-queued job(s) again", resent)
    return resent


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
        stored = stored_result(reference)
        if stored is not None:
            files, key = stored
            if not files.result_exists(key):
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


ENSEMBLE_OFF = "Ensemble transcriptions were turned off on this server"


def move_files_to_the_bucket(job_id: str) -> None:
    """A job queued while its files were kept in the data volume's folders,
    as before this machine became a cluster's hub or JOB_STORAGE changed:
    its files go to the bucket job files are in now, so any device can run
    it. Run by the API at startup, which has both. Never raises."""
    folder = job_dir(job_id)
    if not uses_bucket() or not folder.is_dir():
        return
    try:
        for path in sorted(folder.iterdir()):
            if path.is_file():
                storage().save(job_id, path)
        shutil.rmtree(folder, ignore_errors=True)
        logger.info("Moved the files of queued job %s to the bucket", job_id)
    except Exception:
        # Left where they are: the hub's own workers still find them there.
        logger.exception("Could not move the files of queued job %s to the bucket", job_id)


def requeue_after_restart(job_id: str, job: dict[str, str], reason: str) -> None:
    move_files_to_the_bucket(job_id)
    backend = job_backend(job)
    if backend == "remote" and not routing.outsourcing_enabled():
        # Outsourcing was turned off while the server was down.
        backend = "local"
        update_job(job_id, backend=backend)
    routing.mark_active(redis, backend, job_id)
    if job.get("mode") == ENSEMBLE and not settings.worker.ensemble:
        # No worker would take it: nothing consumes the ensemble queue.
        logger.warning("Job %s failed after the restart: %s", job_id, ENSEMBLE_OFF)
        fail_unqueued_job(job_id, job, backend, ENSEMBLE_OFF)
        return
    try:
        send_job(job_id, backend, *job_arguments(job))
    except Exception:
        logger.exception("Could not requeue job %s after the restart", job_id)
        fail_unqueued_job(job_id, job, backend, reason)


def fail_unqueued_job(job_id: str, job: dict[str, str], backend: routing.Backend, reason: str,
                      code: str = INTERRUPTED) -> None:
    """Fail and refund a queued job that could not be put in a queue."""
    routing.mark_finished(redis, backend, job_id)
    update_job(job_id, status="failed", error=reason, error_code=code)
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
        ensemble: bool = False,
        token: str | None = None,
        model_done: Callable[[], None] | None = None,
        clip: clips.Clip | None = None,
    ):
        self.job_id = job_id
        self.output_dir = output_dir
        self.progress_floor = progress_floor
        # The duration the job was charged for; audio past it is not transcribed.
        self.max_seconds = max_seconds
        # Set for an ensemble job, which runs dettato instead
        # of Transkun.
        self.ensemble = ensemble
        # The part of the audio the job transcribes; max_seconds is its length.
        self.clip = clip
        # This attempt's token: it stops once another attempt owns the job.
        self.token = token
        # Called once the job's model step has ended: it gives back the
        # job's place on this device's models (app.worker.places).
        self.model_done = model_done or (lambda: None)
        # The piano levels the backend brought with an ensemble result, as
        # files in output_dir (a RunPod endpoint's), or None when it brought
        # none: a local job makes them here, a remote one afterwards.
        self.levels: list[str] | None = None

    def is_cancelled(self) -> bool:
        return is_job_cancelled(self.job_id, self.token)

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


# Set in a worker that consumes a local queue: the places on this device's
# models (app.worker.places). None elsewhere, as in the API.
model_places: Places | None = None
# Set as the worker shuts down: a job still waiting for a place is queued
# again, for another worker.
worker_stopping = threading.Event()
# Set by the heartbeat in a worker that consumes a local queue: how busy
# this device is (app.worker.load). None elsewhere, as in the API.
load_monitor = None
# Set once the worker's models are loaded: the room for jobs then measures
# the worker's own memory.
models_loaded = threading.Event()


def job_memory_gib(job: dict[str, str]) -> float:
    """VRAM one more job of this kind needs, as the worker plans it."""
    worker = settings.worker
    if job.get("mode") == ENSEMBLE:
        from app.transcription.device import ensemble_memory_gib

        return ensemble_memory_gib()
    return worker.memory_per_job_gib


def place_memory_gib(job: dict[str, str]) -> float:
    """The memory a place holds for the job: its VRAM on a GPU; nothing on
    the CPU, whose places are only a count."""
    return job_memory_gib(job) if model_places is not None and model_places.room is not None else 0.0


def busy_here(job: dict[str, str]) -> bool:
    """Whether this device is too busy for one more job: its GPU or CPU at
    WORKER_BUSY_UTILIZATION or more, by any program, or no room on its GPU
    for the job beside the jobs that hold places, as other programs use it."""
    if load_monitor is None:
        return False
    load = load_monitor.current()
    if load.utilization is not None and load.utilization >= settings.worker.busy_utilization:
        return True
    places = model_places
    if places is None or places.room is None:
        return False
    return places.usable(job_memory_gib(job)) <= places.in_use


def roomier_device(job: dict[str, str], task: str) -> str | None:
    """Another device whose worker for task has a free place, with room for
    a job in the VRAM other programs leave it, and is less busy than
    WORKER_BUSY_UTILIZATION. Its heartbeat counts only the places that fit
    now, so a device whose GPU another program uses is passed over."""
    node = cluster.node_name()
    # A busy GPU is still faster than an idle CPU: a GPU device hands jobs
    # to GPU devices only.
    on_gpu = load_monitor is not None and load_monitor.current().gpu
    for worker in routing.local_workers(redis):
        if not worker["node"] or worker["node"] == node or (on_gpu and not worker["gpu"]):
            continue
        if not worker["ensemble" if task == "arrange" else "transcription"]:
            continue
        if worker["running"] >= worker["capacity"] or worker["utilization"] is None:
            continue
        if worker["utilization"] >= settings.worker.busy_utilization:
            continue
        return worker["node"]
    return None


# A local job's Dramatiq time limit: a base, for the download and the last
# steps, and a share of its audio's length, generous for the slowest device a
# job may run on (an ensemble job on a CPU runs at about 1.2 times its length).
# Only a stuck job reaches it; a job of any allowed length never does.
LOCAL_TIME_LIMIT_BASE = 10 * 60
LOCAL_TIME_LIMIT_PER_SECOND = 4.0


def local_time_limit_ms(job: dict[str, str]) -> int:
    try:
        seconds = max(0.0, float(job.get("duration") or 0))
    except ValueError:
        seconds = 0.0
    return int((LOCAL_TIME_LIMIT_BASE + LOCAL_TIME_LIMIT_PER_SECOND * seconds) * 1000)


def restart_time_limit(job: dict[str, str] | None = None) -> None:
    """Start Dramatiq's time limit of this thread's message again: the wait
    for a place is not the job's time. The limit is the job's
    (local_time_limit_ms), or Dramatiq's default without a job."""
    from dramatiq.middleware import TimeLimit

    for middleware in broker.middleware:
        manager = getattr(middleware, "manager", None) if isinstance(middleware, TimeLimit) else None
        if manager is not None:
            manager.add_timeout(threading.get_ident(), local_time_limit_ms(job) if job is not None else middleware.time_limit)


def wait_for_place(job_id: str) -> tuple[Place | None, bool]:
    """A place on this device's models for a local job, before it is
    claimed. While none is free, the job stays queued, and goes to another
    device as soon as that one has a free place; it is queued again if this
    worker stops. (the place, None) or (None, whether the job was sent on)."""
    reasons: list[str] = []
    stored: dict[str, str] = {}
    try:
        stored = redis.hgetall(job_key(job_id))
        memory_gib = place_memory_gib(stored)
    except Exception:
        logger.exception("Could not read job %s; planning a transcription's memory", job_id)
        memory_gib = place_memory_gib({})

    def give_up() -> bool:
        if worker_stopping.is_set():
            reasons.append("this worker is stopping")
            return True
        try:
            job = redis.hgetall(job_key(job_id))
            if job.get("status") != "queued":
                # Cancelled, or claimed by another delivery of its message.
                return True
            if not cluster.has_nodes(redis) or int(job.get("passes") or 0) >= MAX_PASSES:
                return False
            other = roomier_device(job, job_task(job, "local"))
            if other:
                reasons.append(f"no place is free here, and {other} has one")
                return True
        except Exception:
            # Waiting here is always possible.
            logger.exception("Could not look for another device for job %s", job_id)
        return False

    place = model_places.take(give_up, PLACE_CHECK_INTERVAL, memory_gib)
    if place is not None:
        restart_time_limit(stored)
        return place, False
    if not reasons:
        return None, False
    try:
        job = redis.hgetall(job_key(job_id))
        if job.get("status") != "queued":
            return None, False
        if worker_stopping.is_set():
            send_job(job_id, "local", *job_arguments(job))
        else:
            redis.hincrby(job_key(job_id), "passes", 1)
            send_job(job_id, "local", *job_arguments(job), delay=PASS_DELAY_MS)
    except Exception:
        # Still queued: the leading API sends it again (resend_stale_queued).
        logger.exception("Could not queue job %s again", job_id)
    logger.info("Job %s left to another worker: %s", job_id, reasons[0])
    return None, True


# A job that finds too little disk space here is offered again this long
# later, here or on another device, at most this many times (half an hour).
DISK_WAIT_DELAY = 60.0
DISK_WAIT_LIMIT = 30


def waits_for_disk(job_id: str, backend: routing.Backend) -> bool:
    """Whether a queued job must wait for disk space instead of starting here:
    its files (app.disk.job_disk_bytes) would leave less than
    MIN_FREE_DISK_GIB free on this device's data disk. It is then sent again
    after DISK_WAIT_DELAY, still queued, so this or another device takes it
    once there is room; after DISK_WAIT_LIMIT waits it fails, refunded."""
    from app import disk

    try:
        job = redis.hgetall(job_key(job_id))
        if job.get("status") != "queued" or disk.room_for(disk.job_disk_bytes(job)):
            return False
        waits = redis.hincrby(job_key(job_id), "disk_waits", 1)
        free = disk.free_bytes()
        if waits > DISK_WAIT_LIMIT:
            logger.error("Job %s failed: no disk space for it after %d waits", job_id, waits - 1)
            fail_unqueued_job(job_id, job, backend, str(disk.StorageFull()), disk.STORAGE_FULL)
            return True
        logger.warning("Job %s waits for disk space: %.1f GiB free, it needs %.1f GiB beside MIN_FREE_DISK_GIB=%g",
                       job_id, (free or 0) / disk.GIB, disk.job_disk_bytes(job) / disk.GIB,
                       settings.limits.min_free_disk_gib)
        send_job(job_id, backend, *job_arguments(job), delay=int(DISK_WAIT_DELAY * 1000))
        return True
    except Exception:
        # The check must never lose a job: it runs here as before.
        logger.exception("Could not check the disk space for job %s", job_id)
        return False


RAM_WAIT_DELAY = 30.0


def waits_for_ram(job_id: str, backend: routing.Backend) -> bool:
    """Whether a queued local job must wait for RAM instead of starting here:
    its need (app.memory) beside what this worker's running jobs reserved
    would leave less than WORKER_MIN_FREE_RAM_GIB free. It is then sent again
    after RAM_WAIT_DELAY, still queued, so this or another device takes it
    once there is room. Otherwise its need stays reserved until run_job ends.
    A job with nothing else running here always starts."""
    if backend != "local":
        return False
    from app import memory

    try:
        job = redis.hgetall(job_key(job_id))
        if job.get("status") != "queued":
            return False
        started, need, free = memory.ledger.reserve(job_id, job)
        if started:
            return False
        waits = redis.hincrby(job_key(job_id), "ram_waits", 1)
        log = logger.warning if waits % 10 == 1 else logger.info
        log("Job %s waits for RAM: %.1f GiB free, it needs %.1f GiB beside the running jobs' and WORKER_MIN_FREE_RAM_GIB=%g",
            job_id, (free or 0) / memory.GIB, need / memory.GIB, settings.worker.min_free_ram_gib)
        send_job(job_id, backend, *job_arguments(job), delay=int(RAM_WAIT_DELAY * 1000))
        return True
    except Exception:
        # The check must never lose a job: it runs here as before.
        logger.exception("Could not check the RAM for job %s", job_id)
        return False


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
    if pass_job(job_id, backend):
        return
    if waits_for_disk(job_id, backend):
        return
    if waits_for_ram(job_id, backend):
        return
    output_dir = storage().workspace(job_id)
    requeued = False
    claimed = False
    place = None
    lease = JobLease(job_id)
    try:
        if backend == "local" and model_places is not None:
            place, requeued = wait_for_place(job_id)
            if place is None:
                return
        if not claim_job(job_id, lease.token, backend):
            return
        claimed = True
        if backend == "local":
            # Its own time limit, from its length, instead of Dramatiq's
            # default for the actor (local_time_limit_ms).
            restart_time_limit(redis.hgetall(job_key(job_id)))
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
        context = JobContext(job_id, output_dir, floor, max_seconds, is_ensemble(stored), lease.token,
                             place.release if place is not None else None, clips.stored_clip(stored))

        midi_path = transcribe(context)

        if is_job_cancelled(job_id, lease.token):
            raise TranscriptionCancelled()

        if superseded(job_id, lease.token):
            raise TranscriptionCancelled()
        stored_variants, levels_later = None, False
        if context.ensemble:
            made = context.levels
            if made is None and backend == "local":
                made = variants.make_levels(midi_path, output_dir, job_id)
            # A RunPod endpoint on an older image returned none: the job is
            # completed with its raw result, and they follow (levels_later).
            levels_later = made is None
            stored_variants = variants.stored_field(store_levels(job_id, output_dir, made or []))
        reference = storage().save_result(job_id, midi_path)
        if not complete_job(job_id, midi_path, reference, lease.token, stored_variants):
            if superseded(job_id, lease.token):
                # Another attempt owns the job; it writes its own result.
                requeued = True
            return
        record_job(job_id)
        billing.settle_success(job_id)
        if levels_later:
            start_late_levels(job_id, midi_path, reference)
        storage().discard_inputs(job_id, output_dir)
        if backend == "local":
            local_job_duration.add(time.monotonic() - started)

    except TranscriptionCancelled:
        if claimed and superseded(job_id, lease.token):
            logger.warning("Job %s is run by another attempt now; this one stops without writing", job_id)
            requeued = True
            return
        finish_cancelled(job_id, output_dir)
    except Exception as e:
        if claimed and superseded(job_id, lease.token):
            logger.warning("Job %s is run by another attempt now; this one stops without writing", job_id)
            requeued = True
            return
        target = retry_backend(job_id, backend, e)
        if target and requeue_job(job_id, backend, target, str(e)):
            requeued = True
            return
        job = redis.hgetall(job_key(job_id))
        if job.get("status") == "cancelled":
            # Cancelled while failing: keep it cancelled.
            finish_cancelled(job_id, output_dir)
            return
        from app import disk

        # Out of disk space: its own message and code, not a bug's.
        shown = disk.StorageFull() if disk.is_disk_full(e) else e
        failed = {
            "status": "failed",
            "error": public_message(shown),
            "error_code": error_code(shown),
            "error_detail": detail(e),
        }
        if not fail_attempt(job_id, lease.token if claimed else None, failed):
            # Cancelled or taken over meanwhile; whoever did settles it.
            requeued = claimed and superseded(job_id, lease.token)
            if redis.hget(job_key(job_id), "status") == "cancelled":
                finish_cancelled(job_id, output_dir)
            return
        logger.warning("Job %s failed", job_id, exc_info=e)
        if public_message(shown) == GENERIC_MESSAGE:
            # No known cause such as a private video: probably a bug or a broken setup.
            error_report.report_exception(e, {"Job": job_id, "Backend": backend})
        # After the job is marked failed: a crash between the two leaves a
        # reservation that the reconciliation releases, never a free retry.
        if job.get("user_id"):
            billing.release(job_id)
        record_job(job_id)
        storage().discard_inputs(job_id, output_dir)
    finally:
        if backend == "local":
            from app import memory

            memory.ledger.release(job_id)
        if place is not None:
            place.release()
        if claimed:
            lease.stop()
            # With S3, the attempt's scratch folder; the kept files stay.
            storage().release(output_dir)
        # A second delivery of a job that is running elsewhere claims nothing
        # and must not remove the running job from the routing count.
        if not requeued and (claimed or redis.hget(job_key(job_id), "status") != "processing"):
            routing.mark_finished(redis, backend, job_id)


def store_levels(job_id: str, folder: Path, levels: list[str]) -> list[str]:
    """Keep the piano levels made in folder beside the job's result; the
    ones kept. A level that cannot be stored is left out, never the job."""
    kept = []
    for level in variants.ordered(levels):
        try:
            storage().save_variant(job_id, folder / variants.file_name(level))
            kept.append(level)
        except Exception as error:
            logger.warning("Job %s: piano level %s not stored (%s)", job_id, level, error)
    return kept


# Levels for results of RunPod endpoints that return none, made by this
# remote worker after their jobs complete: one at a time per process.
late_levels = variants.FallbackLevels(limit=1)


def start_late_levels(job_id: str, midi_path: Path, reference: str) -> None:
    """Make an ensemble job's piano levels after it completed with its raw
    result, in the background (variants.FallbackLevels), from a copy of the
    result in a scratch folder of its own, which cleanup removes after a
    crash. Never raises; the job keeps its raw result whatever happens."""
    folder = Path(settings.service.data_dir) / WORK_FOLDER / f"{job_id}.levels-{uuid.uuid4().hex[:8]}"
    try:
        folder.mkdir(parents=True)
        source = folder / variants.RAW_FILE_NAME
        shutil.copyfile(midi_path, source)
    except OSError:
        logger.exception("Job %s: no piano levels; its result could not be copied", job_id)
        shutil.rmtree(folder, ignore_errors=True)
        return

    def work() -> None:
        try:
            stored = store_levels(job_id, folder, variants.make_levels(source, folder, job_id))
            if stored:
                add_variants(job_id, reference, stored)
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    if not late_levels.submit(job_id, work):
        shutil.rmtree(folder, ignore_errors=True)


def add_variants(job_id: str, reference: str, levels: list[str]) -> bool:
    """Announce levels stored after the job completed: only while it is still
    completed with the same result."""
    key = job_key(job_id)
    while True:
        try:
            with redis.pipeline() as pipe:
                pipe.watch(key)
                status, result = pipe.hmget(key, "status", "result")
                if status != "completed" or result != reference:
                    return False
                pipe.multi()
                pipe.hset(key, "variants", variants.stored_field(levels))
                pipe.execute()
                return True
        except WatchError:
            continue


def fail_attempt(job_id: str, token: str | None, values: dict[str, str]) -> bool:
    """Mark a processing job failed with values, and give it its expiry, in
    one step, only while this attempt (token) owns it. False if it is no
    longer processing or another attempt owns it."""
    key = job_key(job_id)
    while True:
        try:
            with redis.pipeline() as pipe:
                pipe.watch(key)
                status, current = pipe.hmget(key, "status", "attempt_token")
                if status != "processing" or (token is not None and current not in (None, token)):
                    return False
                pipe.multi()
                pipe.hset(key, mapping=values)
                pipe.expire(key, job_ttl())
                pipe.execute()
                return True
        except WatchError:
            continue


def finish_cancelled(job_id: str, output_dir: Path) -> None:
    job = redis.hgetall(job_key(job_id))
    if job.get("status") == "failed":
        shutil.rmtree(output_dir, ignore_errors=True)
        delete_job_files(job_id)
        return
    if job.get("user_id"):
        billing.settle_cancellation(job_id, cancellation_charge(job))
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

    from app import disk

    if disk.is_disk_full(error) and redis.hincrby(job_key(job_id), "disk_retries", 1) <= 1:
        # This device's disk filled up during the job: once more, here once
        # there is room, or on another device (waits_for_disk).
        return backend
    if isinstance(error, SourceTemporarilyUnavailable) and another_address_can_run(job_id, backend):
        # A video site refused this device's address: the same backend on a
        # device that reaches the site from another one.
        return backend
    if backend == "remote":
        if isinstance(error, RemoteFailed) and settings.outsource.fallback_local:
            if redis.hget(job_key(job_id), "mode") == ENSEMBLE:
                # Only to a worker that takes them, or it would wait in vain.
                _, ensemble_workers = routing.ensemble_workers(redis)
                return "local" if ensemble_workers else None
            return "local"
        return None
    if (
        isinstance(error, GpuOutOfMemory)
        and routing.outsourcing_enabled()
        and may_outsource(redis.hgetall(job_key(job_id)))
        and routing.reserve_remote_slot(redis, job_id)
    ):
        return "remote"
    return None


def may_outsource(job: dict[str, str]) -> bool:
    """Whether the server may still send a local job to RunPod: never twice
    (outsourced), nor one kept on its own workers (local_only: heavy use of
    an unlimited plan, or a job an administrator or e2e put there)."""
    return job.get("outsourced") != "1" and job.get("local_only") != "1"


STRANDED = "No device that runs this kind of job is up; sent to RunPod"
# Seconds without a live local worker for a kind of job before its queued
# jobs count as stranded: longer than a worker takes to start again, as in
# a restart, and counted from this process's start, since a worker that
# restarts with the server publishes its heartbeat only once it is up.
STRANDED_AFTER = 3 * routing.HEARTBEAT_TTL
# When this process last saw a live local worker, by ensemble or not.
_local_seen: dict[bool, float] = {}


def outsource_stranded_jobs(now: float | None = None) -> int:
    """Run periodically by the leading API. A queued local job that no live
    local worker takes, as when the only device that runs its kind of job
    lost power or its network mid-job, or a node is off and the hub's worker
    is on a CPU, goes to RunPod once no such worker was seen for
    STRANDED_AFTER and the job waited OUTSOURCE_LOCAL_FIRST_MAX_WAIT, as a
    new job would. Never without outsourcing: then it waits for a
    device, its message sent again by resend_stale_queued. A job that only
    waits behind others for a live worker stays, so the queue keeps its order.
    Reads only the local jobs' set, and nothing while every kind has a worker."""
    if not routing.outsourcing_enabled():
        return 0
    now = time.time() if now is None else now
    stranded = set()
    for ensemble in (False, True):
        if routing.local_capacity(redis, ensemble)[0] > 0 or ensemble not in _local_seen:
            _local_seen[ensemble] = now
        elif now - _local_seen[ensemble] >= STRANDED_AFTER:
            stranded.add(ensemble)
    if not stranded:
        return 0
    moved = 0
    for job_id in list(redis.sscan_iter(routing.ACTIVE_KEYS["local"])):
        job = redis.hgetall(job_key(job_id))
        if job.get("status") != "queued" or job_backend(job) != "local" or not may_outsource(job):
            continue
        if is_ensemble(job) not in stranded:
            continue
        try:
            queued_at = float(job.get("queued_at") or job.get("created_at") or now)
        except ValueError:
            queued_at = now
        if now - queued_at < settings.outsource.local_first_max_wait:
            continue
        if not routing.reserve_remote_slot(redis, job_id):
            # RunPod is full: the rest wait for the next round too.
            break
        moved += outsource_queued_job(job_id, STRANDED)
    return moved


def outsource_queued_job(job_id: str, reason: str) -> bool:
    """Move a queued local job, whose remote slot is reserved, to the remote
    queue. Its local message, if a device still delivers it, is a leftover
    that claim_job refuses. Returns False, freeing the slot, if the job was
    taken, cancelled, or changed meanwhile."""
    key = job_key(job_id)
    while True:
        try:
            with redis.pipeline() as pipe:
                pipe.watch(key)
                job = pipe.hgetall(key)
                if job.get("status") != "queued" or job_backend(job) != "local" or not may_outsource(job):
                    pipe.unwatch()
                    if job_backend(job) != "remote":
                        routing.mark_finished(redis, "remote", job_id)
                    return False
                pipe.multi()
                pipe.hset(key, mapping={
                    "backend": "remote",
                    "outsourced": "1",
                    "requeue_reason": reason,
                    "queued_at": str(time.time()),
                })
                pipe.execute()
                break
        except WatchError:
            continue
    # The remote set has it already: reserve_remote_slot added it.
    routing.mark_finished(redis, "local", job_id)
    record_job(job_id)
    logger.warning("Job %s moved from local to remote: %s", job_id, reason)
    try:
        send_job(job_id, "remote", *job_arguments(job))
    except Exception:
        logger.exception("Could not send job %s to the remote queue; it stays local", job_id)
        update_job(job_id, backend="local", outsourced=job.get("outsourced") or "")
        routing.move(redis, job_id, "remote", "local")
        record_job(job_id)
        return False
    return True


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
                    if target == "remote" and source != "remote":
                        routing.mark_finished(redis, "remote", job_id)
                    return False
                pipe.multi()
                pipe.hset(
                    key,
                    mapping={
                        "status": "queued",
                        "backend": target,
                        "requeue_reason": reason,
                        "queued_at": str(time.time()),
                    },
                )
                if source == target:
                    # To another device's address: counted, never outsourced.
                    pipe.hincrby(key, "address_moves", 1)
                else:
                    pipe.hset(key, "outsourced", "1")
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


def job_task(job: dict[str, str], backend: routing.Backend) -> str:
    """The cluster task a job needs from a device (app.cluster.TASKS)."""
    if backend == "remote":
        return "dispatch"
    return "arrange" if job.get("mode") == ENSEMBLE else "transcribe"


def job_site(job: dict[str, str]) -> str | None:
    """The video site a link job reads, or None for an upload."""
    from app import video_sites

    url = job.get("source_url")
    if job.get("source") == "upload" or not url:
        return None
    return video_sites.site_of(url)


def free_addresses(site: str, task: str) -> list[str]:
    """Addresses, other than this device's, of devices that run task now
    and that site does not refuse."""
    from app import video_sites

    own = cluster.egress(redis)
    return [
        address for address in cluster.live_egresses(redis, task)
        if address != own and video_sites.refusal(site, address) is None
    ]


def another_address_can_run(job_id: str, backend: routing.Backend) -> bool:
    """Whether a link job that a video site refused here can move to a
    device the site does not refuse, MAX_ADDRESS_MOVES times at most."""
    if not cluster.has_nodes(redis):
        return False
    job = redis.hgetall(job_key(job_id))
    site = job_site(job)
    if site is None or int(job.get("address_moves") or 0) >= MAX_ADDRESS_MOVES:
        return False
    return bool(free_addresses(site, job_task(job, backend)))


def pass_reason(job: dict[str, str], backend: routing.Backend) -> str | None:
    """Why this device should leave a queued job to another, if it should:
    the job's video site refuses this device's address but not another
    device's, or the job was interrupted here and another device runs such
    jobs. Never without invited nodes."""
    from app import video_sites

    if not cluster.has_nodes(redis):
        return None
    task = job_task(job, backend)
    site = job_site(job)
    if site is not None and video_sites.refusal(site) is not None and free_addresses(site, task):
        return f"{site} refuses this device's address"
    node = cluster.node_name()
    ran_on = [name for name in (job.get("nodes") or "").split(",") if name]
    if job.get("interruptions") and node in ran_on:
        others = [name for name, tasks in cluster.running_tasks(redis).items() if task in tasks and name not in ran_on]
        if others:
            return "it was interrupted on this device"
    if backend == "local" and busy_here(job):
        other = roomier_device(job, task)
        if other:
            return f"this device is busy, and {other} has room"
    return None


def pass_job(job_id: str, backend: routing.Backend) -> bool:
    """Hand a queued job to another device before claiming it, if
    pass_reason says to; at most MAX_PASSES times, then it runs here. The
    message goes back to the same queue, a moment later."""
    try:
        job = redis.hgetall(job_key(job_id))
        if job.get("status") != "queued":
            return False
        reason = pass_reason(job, backend)
        if reason is None or int(job.get("passes") or 0) >= MAX_PASSES:
            return False
        redis.hincrby(job_key(job_id), "passes", 1)
        send_job(job_id, backend, *job_arguments(job), delay=PASS_DELAY_MS)
    except Exception:
        # Running it here is always possible.
        logger.exception("Could not hand job %s to another device", job_id)
        return False
    logger.info("Job %s left to another device: %s", job_id, reason)
    return True


def job_arguments(job: dict[str, str]) -> tuple:
    """Actor arguments that recreate a job from its stored fields."""
    if job.get("source") == "upload":
        return (None, job.get("upload_title") or job.get("title"))
    return (job.get("source_url"),)


def send_job(job_id: str, backend: routing.Backend, *arguments, **options) -> None:
    """Hand a stored job to its backend's queue. A local ensemble job goes to
    the queue only workers that take them consume; the mode is read from
    the job, so every requeue keeps it."""
    if backend == "remote":
        actor = transcribe_remote_job
    elif redis.hget(job_key(job_id), "mode") == ENSEMBLE:
        actor = ensemble_job
    else:
        actor = transcribe_job
    # delay is Dramatiq's; send() would hand it to the actor as an argument.
    # Every other keyword, such as upload_title, is the actor's.
    delay = options.pop("delay", None)
    if delay is not None:
        actor.send_with_options(args=(job_id, *arguments), kwargs=options or None, delay=delay)
    else:
        actor.send(job_id, *arguments, **options)


def run_local_job(job_id: str, source_url: str | None, upload_title: str | None) -> None:
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
            clip=context.clip,
            ensemble=context.ensemble,
            model_done_callback=context.model_done,
        )

    run_job(job_id, "local", transcribe)


@dramatiq.actor(max_retries=0)
def transcribe_job(
    job_id: str,
    source_url: str | None = None,
    upload_title: str | None = None,
):
    """Transcribe a video URL, or the uploaded file in the job directory
    when source_url is None, on this machine's GPU or CPU."""
    run_local_job(job_id, source_url, upload_title)


# Named arrange_job in Dramatiq, as before ensemble jobs replaced piano
# arrangements: messages queued before an update still find it.
@dramatiq.actor(actor_name="arrange_job", queue_name=ENSEMBLE_QUEUE, max_retries=0)
def ensemble_job(
    job_id: str,
    source_url: str | None = None,
    upload_title: str | None = None,
):
    """Transcribe every instrument of a video URL, or the uploaded file (an
    ensemble job), on this machine's GPU or CPU. Only workers that take
    ensemble jobs consume its queue."""
    run_local_job(job_id, source_url, upload_title)


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
                clip=context.clip,
                ensemble=context.ensemble,
            )
        finally:
            routing.release_remote_slot(redis, job_id)

    run_job(job_id, "remote", transcribe)


@dramatiq.actor(queue_name=CHECK_QUEUE, max_retries=0, time_limit=120 * 1000)
def check_video_link(request: str, safe_url: str):
    """Run on a node that checks links: read a video's length for the API,
    whose own address the video site refuses (app.video_check)."""
    from app import video_check

    video_check.answer(redis, request, safe_url)


def gpu_room():
    """The room for jobs on this worker's GPUs (GpuRoom), from the launcher's
    measurement, or None on the CPU."""
    from app.transcription.device import GpuRoom, launcher_capacities

    measured = [(index, free, total) for index, capacity, free, total in launcher_capacities() or [] if capacity > 0]
    if not measured:
        return None
    return GpuRoom({index: total for index, _, total in measured},
                   free_at_start={index: free for index, free, _ in measured})


def consumes(worker, queue: str) -> bool:
    """Whether a Dramatiq worker consumes queue: every queue without --queues."""
    queues = worker.consumer_whitelist
    return not queues or queue in queues


class LocalWorkerHeartbeat(dramatiq.Middleware):
    """Publishes the worker's capacity and average job duration while it
    consumes a local queue, so the API can estimate the local wait. A worker
    a cluster switch keeps to ensemble jobs says it takes no piano jobs.

    On a GPU, the capacity is the places that can be held now, in the VRAM
    other programs leave: the API and other devices then see a GPU that
    another program uses as full, and send jobs elsewhere. It is read every
    ROOM_CHECK_INTERVAL seconds and published as soon as it changes."""

    interval = 15
    room_check_interval = 5

    def __init__(self):
        self._stop: threading.Event | None = None

    def after_worker_boot(self, broker, worker):
        transcribes = consumes(worker, LOCAL_QUEUE)
        ensembles = consumes(worker, ENSEMBLE_QUEUE) and settings.worker.ensemble
        if not (transcribes or ensembles):
            return
        stop = self._stop = threading.Event()
        # Set when a place is taken or given back: other devices then see
        # this one's free places at once, not up to an interval late.
        changed = threading.Event()
        global load_monitor, model_places
        from app.worker import load

        room = gpu_room()
        places = model_places = Places(configured_places(worker.worker_threads), on_change=changed.set,
                                       room=room.budget_gib if room is not None else None)
        monitor = load_monitor = load.LoadMonitor(gpu=not settings.worker.enforce_cpu and load.sample_gpus() is not None)
        monitor.start()
        job_memory = settings.worker.memory_per_job_gib

        def beat():
            published = None
            published_at = 0.0
            measured = room is None
            while not stop.is_set():
                changed.clear()
                if not measured and models_loaded.is_set():
                    measured = True
                    try:
                        room.measure_own_memory()
                    except Exception:
                        logger.exception("Could not measure the worker's own GPU memory")
                try:
                    capacity = places.usable(job_memory)
                    running = places.in_use
                    if (capacity, running) != published or time.monotonic() - published_at >= self.interval:
                        current = monitor.current()
                        routing.publish_heartbeat(redis, capacity, local_job_duration.value, ensemble=ensembles,
                                                  transcription=transcribes, node=cluster.node_name(),
                                                  running=running, utilization=current.utilization,
                                                  free_mib=current.free_mib, gpu=current.gpu)
                        published, published_at = (capacity, running), time.monotonic()
                except Exception:
                    logger.exception("Could not publish the worker heartbeat")
                changed.wait(self.room_check_interval if room is not None else self.interval)

        threading.Thread(target=beat, name="worker-heartbeat", daemon=True).start()

    def before_worker_shutdown(self, broker, worker):
        worker_stopping.set()
        if load_monitor is not None:
            load_monitor.stop()
        if self._stop is not None:
            self._stop.set()
            # Ends the wait of the heartbeat thread, which then stops.
            if model_places is not None:
                model_places.notify_change()
            try:
                routing.clear_heartbeat(redis)
            except Exception:
                logger.exception("Could not clear the worker heartbeat")


class LocalModelPreload(dramatiq.Middleware):
    """Loads the models as the worker starts consuming the local queue, so
    that the first job does not wait for them. A job that arrives meanwhile
    waits for the same load instead of starting its own."""

    def after_worker_boot(self, broker, worker):
        transcribes = consumes(worker, LOCAL_QUEUE)
        ensembles = consumes(worker, ENSEMBLE_QUEUE) and settings.worker.ensemble
        if not (transcribes or ensembles):
            return

        def preload():
            started = time.monotonic()
            try:
                from app.transcription.piano_transcription import preload

                if transcribes:
                    preload()
                if ensembles:
                    from app.transcription.ensemble_transcription import preload as preload_ensemble

                    preload_ensemble()
            except Exception:
                # The first job loads them again, and fails if they still cannot.
                logger.exception("Could not load the models before the first job")
                return
            logger.info("Models loaded in %.1f s", time.monotonic() - started)
            models_loaded.set()

        threading.Thread(target=preload, name="model-preload", daemon=True).start()


class RemoteCapacityRefresh(dramatiq.Middleware):
    """With OUTSOURCE_MAX_CONCURRENCY=auto, rereads the provider's max
    workers while the remote queue is consumed, so console changes apply."""

    def after_worker_boot(self, broker, worker):
        if not consumes(worker, REMOTE_QUEUE) or settings.outsource.max_concurrency is not None:
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
broker.add_middleware(LocalModelPreload())
broker.add_middleware(RemoteCapacityRefresh())
