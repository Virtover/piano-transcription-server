import json
import shutil
from pathlib import Path

import dramatiq
from dramatiq.brokers.redis import RedisBroker
from redis import Redis
from redis.exceptions import WatchError

from app.config import settings
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
    return redis.hget(job_key(job_id), "status") == "cancelled"


def complete_job(job_id: str, result: Path) -> None:
    key = job_key(job_id)

    while True:
        try:
            with redis.pipeline() as pipe:
                pipe.watch(key)
                if pipe.hget(key, "status") == "cancelled":
                    raise TranscriptionCancelled()

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
                return
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

        if is_job_cancelled(job_id):
            return

        update_job(
            job_id,
            status="processing",
            progress="0.0",
        )

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

        complete_job(job_id, midi_path)

    except TranscriptionCancelled:
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
        update_job(
            job_id,
            status="failed",
            error=str(e),
        )

        redis.expire(
            job_key(job_id),
            JOB_TTL,
        )