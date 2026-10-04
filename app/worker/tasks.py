import json
import shutil
from pathlib import Path

import dramatiq
from dramatiq.brokers.redis import RedisBroker
from redis import Redis
from redis.exceptions import WatchError

from app.config import settings
from app.billing import Billing, cancelled_cost
from app.transcription.cancellation import TranscriptionCancelled


JOB_TTL = 60 * 60  # 1 hour


broker = RedisBroker(
    url=settings.redis_url,
)

dramatiq.set_broker(broker)


redis = Redis.from_url(
    settings.redis_url,
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
                pipe.hset(
                    key,
                    mapping={
                        "status": "processing",
                        "progress": "0.0",
                    },
                )
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
    jobs_dir = Path(settings.data_dir) / "jobs"

    if not jobs_dir.exists():
        return

    for job_dir in jobs_dir.iterdir():
        if not job_dir.is_dir():
            continue

        job_id = job_dir.name

        if not redis.exists(job_key(job_id)):
            shutil.rmtree(job_dir)
            print(f"Removed expired job: {job_id}")


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
                        Path(settings.data_dir)
                        / "jobs"
                        / key.removeprefix("transcription:"),
                        ignore_errors=True,
                    )
                    break
            except WatchError:
                continue


@dramatiq.actor
def transcribe_job(job_id: str, source_url: str):
    output_dir = (
        Path(settings.data_dir)
        / "jobs"
        / job_id
    )

    try:
        from app.transcription.pipeline import (
            transcribe_source,
        )

        if not claim_job(job_id):
            return

        output_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        def progress(value: float):
            if is_job_cancelled(job_id):
                raise TranscriptionCancelled()

            update_job(
                job_id,
                progress=str(value),
            )

        def metadata(value: dict):
            if is_job_cancelled(job_id):
                raise TranscriptionCancelled()

            update_job(
                job_id,
                title=value.get("title"),
                metadata=value,
            )

        midi_path = transcribe_source(
            source_url=source_url,
            output_dir=output_dir,
            progress_callback=progress,
            metadata_callback=metadata,
            cancellation_callback=lambda: is_job_cancelled(job_id),
        )

        if is_job_cancelled(job_id):
            raise TranscriptionCancelled()

        if not complete_job(job_id, midi_path):
            return
        billing.settle_success(job_id)

    except TranscriptionCancelled:
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
    except Exception as e:
        job = redis.hgetall(job_key(job_id))
        if job.get("user_id"):
            billing.release(job_id)
        update_job(
            job_id,
            status="failed",
            error=str(e),
        )

        redis.expire(
            job_key(job_id),
            JOB_TTL,
        )