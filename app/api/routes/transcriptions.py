import json
import shutil
import uuid
from collections.abc import Callable
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, HttpUrl
from redis import Redis
from redis.exceptions import WatchError

from app.config import settings
from app.billing import Billing, cancelled_cost, transcription_cost, InsufficientMinutes
from app.api.routes.billing import ensure_allowed_length, video_duration
from app.auth import AuthenticatedUser, current_user
from app.media import UnsupportedMedia, probe_audio
from app.worker.tasks import (
    JOB_TTL,
    UPLOAD_FILE_NAME,
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
billing = Billing()
transcription_submissions_enabled = False


def enable_transcription_submissions() -> None:
    global transcription_submissions_enabled
    transcription_submissions_enabled = True


class CreateTranscriptionRequest(BaseModel):
    source_url: HttpUrl


class CreateTranscriptionResponse(BaseModel):
    job_id: str
    status: str
    minutes: int | None = None


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
    minutes: int | None = None


def current_user_balance(user_id: str | None) -> int | None:
    if settings.billing_provider == "none" or not user_id:
        return None
    billing.grant_free_minutes(user_id)
    return billing.get_balance(user_id)


def require_job_owner(job: dict[str, str], authenticated_user_id: str | None) -> None:
    if settings.billing_provider != "none" and job.get("user_id") != authenticated_user_id:
        raise HTTPException(status_code=404, detail="Transcription job not found")


def ensure_submissions_enabled() -> None:
    if not transcription_submissions_enabled:
        raise HTTPException(
            status_code=503,
            detail="The transcription server is still starting",
        )


def queue_job(
    job_id: str,
    user: AuthenticatedUser | None,
    duration: float,
    fields: dict[str, str],
    send: Callable[[], object],
) -> CreateTranscriptionResponse:
    """Reserve the job's cost, store it as queued, and hand it to the worker."""
    current_user = user.user_id if user else "anonymous"
    email = user.email if user else None
    cost = transcription_cost(duration)
    if email:
        billing.grant_free_minutes(current_user, email)
    current_user_balance(current_user)
    try:
        current_balance = billing.reserve(current_user, job_id, cost, email)
    except InsufficientMinutes as error:
        raise HTTPException(
            status_code=402,
            detail={"message": "Insufficient transcription minutes", "required_minutes": cost},
        ) from error

    redis.hset(
        job_key(job_id),
        mapping={
            "status": "queued",
            "progress": "0.0",
            "user_id": current_user,
            "duration": str(duration),
            "full_cost": str(cost),
            **fields,
        },
    )
    # Queued jobs do not expire; an upload placeholder had a TTL.
    redis.persist(job_key(job_id))

    try:
        send()
    except Exception as error:
        redis.delete(job_key(job_id))
        billing.release(job_id)
        raise HTTPException(
            status_code=503,
            detail="The transcription worker is unavailable",
        ) from error

    return CreateTranscriptionResponse(
        job_id=job_id,
        status="queued",
        minutes=current_balance,
    )


@router.post(
    "",
    response_model=CreateTranscriptionResponse,
    status_code=202,
)
def create_transcription(
    request: CreateTranscriptionRequest,
    user: AuthenticatedUser | None = Depends(current_user),
):
    ensure_submissions_enabled()
    duration = video_duration(request.source_url)
    job_id = str(uuid.uuid4())
    source_url = str(request.source_url)
    return queue_job(
        job_id,
        user,
        duration,
        {"source_url": source_url},
        lambda: transcribe_job.send(job_id, source_url),
    )


def title_from_filename(filename: str) -> str:
    name = filename.replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(character for character in name if character.isprintable()).strip()
    title = name.rsplit(".", 1)[0].strip() if "." in name[1:] else name
    return title[:200] or "Uploaded file"


@router.post(
    "/upload",
    response_model=CreateTranscriptionResponse,
    status_code=202,
)
async def upload_transcription(
    request: Request,
    filename: str = Query(
        min_length=1,
        max_length=255,
        description="Original file name; used as the transcription title.",
    ),
    user: AuthenticatedUser | None = Depends(current_user),
):
    """Transcribe an audio or video file sent as the raw request body."""
    ensure_submissions_enabled()

    size_limit = int(settings.max_upload_size_mb * 1024**2)
    too_large = HTTPException(
        status_code=413,
        detail=f"The file exceeds the {settings.max_upload_size_mb:g} MB upload limit",
    )
    content_length = request.headers.get("content-length", "")
    if content_length.isdigit() and int(content_length) > size_limit:
        raise too_large

    job_id = str(uuid.uuid4())
    key = job_key(job_id)
    job_dir = Path(settings.data_dir) / "jobs" / job_id
    upload_path = job_dir / UPLOAD_FILE_NAME
    title = title_from_filename(filename)

    # The placeholder keeps the cleanup service from removing the directory
    # during the upload, and expires if the API stops before the job is queued.
    redis.hset(key, mapping={"status": "uploading", "user_id": user.user_id if user else "anonymous"})
    redis.expire(key, JOB_TTL)
    try:
        job_dir.mkdir(parents=True, exist_ok=True)
        size = 0
        with upload_path.open("wb") as file:
            async for chunk in request.stream():
                size += len(chunk)
                if size > size_limit:
                    raise too_large
                file.write(chunk)
        if size == 0:
            raise HTTPException(status_code=422, detail="The uploaded file is empty")

        try:
            duration = await run_in_threadpool(probe_audio, upload_path)
        except UnsupportedMedia as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        ensure_allowed_length(duration)

        return await run_in_threadpool(
            queue_job,
            job_id,
            user,
            duration,
            {"source": "upload", "title": title},
            lambda: transcribe_job.send(job_id, upload_title=title),
        )
    except BaseException:
        redis.delete(key)
        shutil.rmtree(job_dir, ignore_errors=True)
        raise


@router.get(
    "/{job_id}",
    response_model=TranscriptionStatusResponse,
)
def get_transcription(
    job_id: str,
    user: AuthenticatedUser | None = Depends(current_user),
):
    job = redis.hgetall(job_key(job_id))

    if not job:
        raise HTTPException(
            status_code=404,
            detail="Transcription job not found",
        )
    require_job_owner(job, user.user_id if user else None)

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
        minutes=current_user_balance(job.get("user_id")),
    )


@router.delete(
    "/{job_id}",
    response_model=CreateTranscriptionResponse,
)
def cancel_transcription(
    job_id: str,
    user: AuthenticatedUser | None = Depends(current_user),
):
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
                require_job_owner(job, user.user_id if user else None)

                status = job.get("status", "unknown")

                if status == "cancelled":
                    return CreateTranscriptionResponse(
                        job_id=job_id,
                        status=status,
                        minutes=current_user_balance(job.get("user_id")),
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
                user = job.get("user_id")
                charged = int(job.get("full_cost", 0))
                progress = float(job.get("progress", 0))
                if user:
                    final_cost = cancelled_cost(charged, progress)
                    billing.settle_cancellation(job_id, final_cost)
                return CreateTranscriptionResponse(
                    job_id=job_id,
                    status="cancelled",
                    minutes=current_user_balance(user),
                )
        except WatchError:
            continue


@router.get("/{job_id}/midi")
def get_midi(
    job_id: str,
    user: AuthenticatedUser | None = Depends(current_user),
):
    job = redis.hgetall(job_key(job_id))

    if not job:
        raise HTTPException(
            status_code=404,
            detail="Transcription job not found",
        )
    require_job_owner(job, user.user_id if user else None)

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