import json
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, HttpUrl
from redis import Redis
from redis.exceptions import WatchError

from app.config import settings
from app.worker.tasks import (
    JOB_TTL,
    job_key,
    transcribe_job,
)


router = APIRouter(
    prefix="/transcriptions",
    tags=["transcriptions"],
)

redis = Redis.from_url(
    settings.redis_url,
    decode_responses=True,
)


class CreateTranscriptionRequest(BaseModel):
    source_url: HttpUrl


class CreateTranscriptionResponse(BaseModel):
    job_id: str
    status: str


class TranscriptionMetadataResponse(BaseModel):
    title: str
    author: str | None = None
    channel: str | None = None
    channel_id: str | None = None
    channel_url: str | None = None
    upload_date: str | None = None
    duration: float | None = None
    thumbnail: HttpUrl | None = None
    webpage_url: HttpUrl | None = None
    view_count: int | None = None
    like_count: int | None = None


class TranscriptionStatusResponse(BaseModel):
    job_id: str
    status: str
    progress: float = Field(ge=0, le=1)
    title: str | None = None
    metadata: TranscriptionMetadataResponse | None = None
    error: str | None = None


@router.post(
    "",
    response_model=CreateTranscriptionResponse,
    status_code=202,
)
def create_transcription(
    request: CreateTranscriptionRequest,
):
    job_id = str(uuid.uuid4())

    redis.hset(
        job_key(job_id),
        mapping={
            "status": "queued",
            "progress": "0.0",
            "source_url": str(request.source_url),
        },
    )

    try:
        transcribe_job.send(
            job_id,
            str(request.source_url),
        )
    except Exception as error:
        redis.delete(job_key(job_id))
        raise HTTPException(
            status_code=503,
            detail="The transcription worker is unavailable",
        ) from error

    return CreateTranscriptionResponse(
        job_id=job_id,
        status="queued",
    )


@router.get(
    "/{job_id}",
    response_model=TranscriptionStatusResponse,
)
def get_transcription(job_id: str):
    job = redis.hgetall(job_key(job_id))

    if not job:
        raise HTTPException(
            status_code=404,
            detail="Transcription job not found",
        )

    try:
        progress = float(job.get("progress", 0))
    except ValueError:
        progress = 0

    metadata = None
    if job.get("metadata"):
        try:
            metadata = TranscriptionMetadataResponse(**json.loads(job["metadata"]))
        except (TypeError, ValueError):
            metadata = None

    return TranscriptionStatusResponse(
        job_id=job_id,
        status=job.get("status", "unknown"),
        progress=max(0, min(1, progress)),
        title=job.get("title"),
        metadata=metadata,
        error=job.get("error"),
    )


@router.delete(
    "/{job_id}",
    response_model=CreateTranscriptionResponse,
)
def cancel_transcription(job_id: str):
    key = job_key(job_id)

    while True:
        try:
            with redis.pipeline() as pipe:
                pipe.watch(key)
                job = pipe.hgetall(key)

                if not job:
                    raise HTTPException(
                        status_code=404,
                        detail="Transcription job not found",
                    )

                status = job.get("status", "unknown")

                if status == "cancelled":
                    return CreateTranscriptionResponse(
                        job_id=job_id,
                        status=status,
                    )

                if status not in {"queued", "processing"}:
                    raise HTTPException(
                        status_code=409,
                        detail="Transcription is not running",
                    )

                pipe.multi()
                pipe.hset(
                    key,
                    mapping={
                        "status": "cancelled",
                        "error": "Transcription cancelled",
                        "progress": job.get("progress", "0.0"),
                    },
                )
                pipe.expire(key, JOB_TTL)
                pipe.execute()
                return CreateTranscriptionResponse(
                    job_id=job_id,
                    status="cancelled",
                )
        except WatchError:
            continue


@router.get("/{job_id}/midi")
def get_midi(job_id: str):
    job = redis.hgetall(job_key(job_id))

    if not job:
        raise HTTPException(
            status_code=404,
            detail="Transcription job not found",
        )

    if job.get("status") != "completed":
        raise HTTPException(
            status_code=409,
            detail="Transcription is not completed",
        )

    path = job.get("result")

    if not path:
        raise HTTPException(
            status_code=500,
            detail="Result path missing",
        )

    result_path = Path(path)
    data_root = Path(settings.data_dir).resolve()

    try:
        result_path.resolve().relative_to(data_root)
    except ValueError as error:
        raise HTTPException(
            status_code=500,
            detail="Invalid result path",
        ) from error

    if not result_path.is_file():
        raise HTTPException(
            status_code=404,
            detail="MIDI result is no longer available",
        )

    return FileResponse(
        result_path,
        media_type="audio/midi",
        filename="transcription.mid",
    )