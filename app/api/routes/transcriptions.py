import json
import shutil
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, HttpUrl
from redis import Redis

from app import job_limits, maintenance, video_info
from app.maintenance import NoticeResponse
from app.config import settings
from app.billing import Billing, transcription_cost, InsufficientMinutes
from app.api.routes.billing import check_video, ensure_accepting, ensure_allowed_length
from app.auth import AuthenticatedUser, current_user
from app.media import UnsupportedMedia, probe_audio
from app.outsource import routing
from app.worker.tasks import (
    UPLOAD_FILE_NAME,
    cancel_job,
    job_key,
    record_job,
    send_job,
)


# A placeholder for a job being uploaded or checked expires after this, in
# case the API stops before the job is queued.
PLACEHOLDER_TTL = 60 * 60
# Job IDs derived from a user and an Idempotency-Key.
SUBMISSION_NAMESPACE = uuid.UUID("5d0c2f8e-3a51-4f6b-9a8e-7c1b2d4e6f10")
IDEMPOTENCY_KEY_MAX_LENGTH = 200
# Statuses of a submission that has not been queued yet.
PLACEHOLDER_STATUSES = {"preparing", "uploading"}


router = APIRouter(
    prefix="/transcriptions",
    tags=["transcriptions"],
)

redis = Redis.from_url(
    settings.service.redis_url,
    decode_responses=True,
    **settings.service.redis_options(),
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
    # A stable code for the error, for clients to act on; see docs/api.md.
    error_code: str | None = None
    minutes: int | None = None
    # Where the model runs: local or remote. Absent for jobs queued before
    # outsourcing existed.
    backend: str | None = None
    # A planned server restart or shutdown, if one is pending.
    maintenance: NoticeResponse | None = None


def current_user_balance(user_id: str | None) -> int | None:
    if settings.billing.provider == "none" or not user_id:
        return None
    billing.grant_free_minutes(user_id)
    return billing.get_balance(user_id)


def require_job_owner(job: dict[str, str], authenticated_user_id: str | None) -> None:
    if settings.billing.provider != "none" and job.get("user_id") != authenticated_user_id:
        raise HTTPException(status_code=404, detail="Transcription job not found")


def ensure_submissions_enabled() -> None:
    if not transcription_submissions_enabled:
        raise HTTPException(
            status_code=503,
            detail="The transcription server is still starting",
            headers={"Retry-After": "10"},
        )


def ensure_no_maintenance_pending() -> None:
    """Refuse a job that would not finish before a planned restart or
    shutdown. Nothing is reserved or charged."""
    notice = maintenance.refusal(redis, routing.estimated_job_seconds(redis))
    if notice is None:
        return
    response = notice.as_response()
    paused = (
        "new transcriptions are paused until it is back"
        if notice.action == "restart"
        else "new transcriptions are paused"
    )
    retry_after = notice.retry_after()
    raise HTTPException(
        status_code=503,
        detail={"message": f"{response.message}; {paused}", "maintenance": response.model_dump()},
        headers={"Retry-After": str(retry_after)} if retry_after is not None else None,
    )


def max_length_seconds() -> float | None:
    minutes = settings.limits.max_video_length_minutes
    return minutes * 60 if minutes else None


class SubmissionExists(Exception):
    """A job with this ID already exists: the same submission was sent again."""


def submission_job_id(user_id: str, idempotency_key: str | None) -> str:
    """A new job ID, or with an Idempotency-Key the same ID every time this
    user sends that key, so a retried submission finds its first job."""
    if idempotency_key is None:
        return str(uuid.uuid4())
    if not 0 < len(idempotency_key) <= IDEMPOTENCY_KEY_MAX_LENGTH or not idempotency_key.isprintable():
        raise HTTPException(
            status_code=422,
            detail=f"Idempotency-Key must be 1 to {IDEMPOTENCY_KEY_MAX_LENGTH} printable characters",
        )
    return str(uuid.uuid5(SUBMISSION_NAMESPACE, f"{user_id}\n{idempotency_key}"))


def earlier_submission(job_id: str, user: AuthenticatedUser | None) -> CreateTranscriptionResponse | None:
    """The response for a submission sent again with the same
    Idempotency-Key: its job, without queuing or charging anything again.
    None when there was no earlier submission, or it failed before queuing."""
    job = redis.hgetall(job_key(job_id))
    if job.get("status") in PLACEHOLDER_STATUSES:
        raise HTTPException(
            status_code=409,
            detail="This submission is still being checked or uploaded; try again shortly",
            headers={"Retry-After": "5"},
        )
    if job:
        return CreateTranscriptionResponse(
            job_id=job_id,
            status=job.get("status", "unknown"),
            minutes=current_user_balance(user.user_id if user else None),
        )
    if billing.job_seen(job_id):
        raise HTTPException(
            status_code=409,
            detail="This submission was already processed and its result has expired; "
            "send a new Idempotency-Key to transcribe it again",
        )
    return None


def hold_place(job_id: str, user_id: str, status: str) -> None:
    """Store a placeholder for a job that is not queued yet and count it
    against the job limits. The placeholder expires if the API stops first.
    Raises SubmissionExists if the job already exists."""
    key = job_key(job_id)
    # Claimed atomically, so two copies of a submission cannot both queue it.
    if not redis.hsetnx(key, "status", status):
        raise SubmissionExists()
    redis.hset(key, "user_id", user_id)
    redis.expire(key, PLACEHOLDER_TTL)
    try:
        job_limits.acquire(redis, job_id, user_id, job_key)
    except job_limits.TooManyJobs as error:
        redis.delete(key)
        raise HTTPException(status_code=429, detail=str(error), headers={"Retry-After": "60"}) from error


def claim_submission(job_id: str, user: AuthenticatedUser | None, status: str) -> CreateTranscriptionResponse | None:
    """Hold the place of a new submission. Returns the earlier job instead
    when the same submission arrived at the same time."""
    try:
        hold_place(job_id, user.user_id if user else "anonymous", status)
    except SubmissionExists:
        # The same submission arrived twice at once.
        earlier = earlier_submission(job_id, user)
        if earlier is None:
            raise HTTPException(
                status_code=409,
                detail="This submission is being sent twice at once; try again shortly",
                headers={"Retry-After": "5"},
            ) from None
        return earlier
    return None


IDEMPOTENCY_KEY = Header(
    default=None,
    alias="Idempotency-Key",
    description="A unique ID the client chooses for this submission and sends again when retrying it. "
    "A retry then returns the first job instead of queuing and charging a second one.",
)


def give_back_place(job_id: str, user_id: str) -> None:
    redis.delete(job_key(job_id))
    job_limits.release(redis, job_id, user_id)


def ensure_minimum_balance(user: AuthenticatedUser | None) -> None:
    """Refuse a user who could not pay for even the shortest job, before
    their upload is read."""
    if user is None:
        return
    billing.grant_free_minutes(user.user_id, user.email)
    required = transcription_cost(0)
    if billing.get_balance(user.user_id) < required:
        raise HTTPException(
            status_code=402,
            detail={
                "message": "Insufficient transcription minutes",
                "error_code": "insufficient_minutes",
                "required_minutes": required,
            },
        )


def current_notice() -> NoticeResponse | None:
    notice = maintenance.current(redis)
    return notice.as_response() if notice else None


def queue_job(
    job_id: str,
    user: AuthenticatedUser | None,
    duration: float,
    fields: dict[str, str],
    arguments: tuple[Any, ...] = (),
    options: dict[str, Any] | None = None,
    backend: routing.Backend | None = None,
) -> CreateTranscriptionResponse:
    """Reserve the job's cost, store it as queued, choose the local or
    remote backend unless one is given, and hand it to that backend's queue."""
    ensure_no_maintenance_pending()
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
            detail={
                "message": "Insufficient transcription minutes",
                "error_code": "insufficient_minutes",
                "required_minutes": cost,
            },
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
    if backend is None:
        backend = routing.choose_backend(redis, job_id)
    else:
        routing.mark_active(redis, backend, job_id)
    redis.hset(job_key(job_id), "backend", backend)

    try:
        send_job(job_id, backend, *arguments, **(options or {}))
    except Exception as error:
        routing.mark_finished(redis, backend, job_id)
        redis.delete(job_key(job_id))
        billing.release(job_id)
        raise HTTPException(
            status_code=503,
            detail="The transcription worker is unavailable",
        ) from error
    record_job(job_id)

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
    idempotency_key: str | None = IDEMPOTENCY_KEY,
):
    ensure_submissions_enabled()
    user_id = user.user_id if user else "anonymous"
    job_id = submission_job_id(user_id, idempotency_key)
    # A retry gets its job even if new submissions are refused meanwhile.
    if idempotency_key is not None and (earlier := earlier_submission(job_id, user)) is not None:
        return earlier
    ensure_no_maintenance_pending()
    ensure_accepting(user_id, link=True, source_url=str(request.source_url))
    # The place is held during the video check too, which runs yt-dlp.
    earlier = claim_submission(job_id, user, "preparing")
    if earlier is not None:
        return earlier
    try:
        return submit_link(job_id, str(request.source_url), user)
    except BaseException:
        give_back_place(job_id, user_id)
        shutil.rmtree(job_directory(job_id), ignore_errors=True)
        raise


def job_directory(job_id: str) -> Path:
    return Path(settings.service.data_dir) / "jobs" / job_id


def submit_link(
    job_id: str,
    source_url: str,
    user: AuthenticatedUser | None,
    backend: routing.Backend | None = None,
) -> CreateTranscriptionResponse:
    """Check a video link and queue it, for a job whose place is held."""
    video = check_video(source_url)
    # The worker downloads with this info instead of asking the site again.
    video_info.save_video_info(job_directory(job_id), video.info, video.checked_at)
    fields = {"source_url": source_url}
    # Clients see the title while the job waits; the download updates it.
    metadata = video_info.video_metadata(video.info, source_url)
    if metadata["title"]:
        fields.update(title=metadata["title"], metadata=json.dumps(metadata))
    return queue_job(job_id, user, video.duration, fields, (source_url,), backend=backend)


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
    idempotency_key: str | None = IDEMPOTENCY_KEY,
):
    """Transcribe an audio or video file sent as the raw request body."""
    ensure_submissions_enabled()
    user_id = user.user_id if user else "anonymous"
    job_id = submission_job_id(user_id, idempotency_key)
    # Answered before the body is read: a retried upload is not stored twice.
    if idempotency_key is not None:
        earlier = await run_in_threadpool(earlier_submission, job_id, user)
        if earlier is not None:
            return earlier
    ensure_no_maintenance_pending()
    await run_in_threadpool(ensure_accepting, user.user_id if user else "anonymous", False)

    size_limit = (
        int(settings.limits.max_upload_size_mb * 1024**2)
        if settings.limits.max_upload_size_mb is not None
        else None
    )
    too_large = HTTPException(
        status_code=413,
        detail=f"The file exceeds the {settings.limits.max_upload_size_mb:g} MB upload limit"
        if settings.limits.max_upload_size_mb is not None
        else "The uploaded file exceeds the configured limit",
    )
    content_length = request.headers.get("content-length", "")
    if size_limit is not None and content_length.isdigit() and int(content_length) > size_limit:
        raise too_large
    await run_in_threadpool(ensure_minimum_balance, user)

    job_dir = Path(settings.service.data_dir) / "jobs" / job_id
    upload_path = job_dir / UPLOAD_FILE_NAME
    title = title_from_filename(filename)

    # The placeholder keeps the cleanup service from removing the directory
    # during the upload, and counts the upload against the job limits.
    earlier = await run_in_threadpool(claim_submission, job_id, user, "uploading")
    if earlier is not None:
        return earlier
    try:
        job_dir.mkdir(parents=True, exist_ok=True)
        size = 0
        with upload_path.open("wb") as file:
            async for chunk in request.stream():
                size += len(chunk)
                if size_limit is not None and size > size_limit:
                    raise too_large
                file.write(chunk)
        if size == 0:
            raise HTTPException(status_code=422, detail="The uploaded file is empty")

        try:
            # Measured by decoding, not read from the header, which can understate it.
            duration = await run_in_threadpool(probe_audio, upload_path, True, max_length_seconds())
        except UnsupportedMedia as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        ensure_allowed_length(duration)

        return await run_in_threadpool(
            queue_job,
            job_id,
            user,
            duration,
            {"source": "upload", "title": title, "upload_title": title},
            (),
            {"upload_title": title},
        )
    except BaseException:
        give_back_place(job_id, user_id)
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
        error_code=job.get("error_code"),
        minutes=current_user_balance(job.get("user_id")),
        backend=job.get("backend"),
        maintenance=current_notice(),
    )


@router.delete(
    "/{job_id}",
    response_model=CreateTranscriptionResponse,
)
def cancel_transcription(
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

    status = cancel_job(job_id)
    if status is None:
        raise HTTPException(
            status_code=404,
            detail="Transcription job not found",
        )
    if status not in {"queued", "processing", "cancelled"}:
        raise HTTPException(
            status_code=409,
            detail="Transcription is not running",
        )
    return CreateTranscriptionResponse(
        job_id=job_id,
        status="cancelled",
        minutes=current_user_balance(job.get("user_id")),
    )


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
    data_root = Path(settings.service.data_dir).resolve()

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