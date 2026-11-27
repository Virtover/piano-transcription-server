"""What a developer's account sees of the server: the live state of its
queues and workers, for testing the app against it. Accounts become a
developer's in the admin web page or with `python -m app.admin user <who>
developer on`. Nothing here names another user, a path, or a host."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from redis import Redis

from app import controls, job_limits, maintenance, metrics
from app.auth import AuthenticatedUser, current_user
from app.billing import Billing
from app.config import settings
from app.maintenance import NoticeResponse
from app.outsource import routing
from app.worker.tasks import job_key

router = APIRouter(prefix="/developer", tags=["developer"])
redis = Redis.from_url(settings.service.redis_url, decode_responses=True, **settings.service.redis_options())
billing = Billing()


class Workers(BaseModel):
    count: int
    capacity: int
    # Workers that take ensemble jobs.
    ensemble: int
    # The same, by its name from when ensemble jobs were piano arrangements,
    # which older apps read.
    arranging: int
    average_job_seconds: float | None = None


class Backend(BaseModel):
    waiting: int
    running: int
    oldest_wait_seconds: float


class ServerStateResponse(BaseModel):
    # Jobs waiting or running, against MAX_ACTIVE_JOBS.
    active_jobs: int
    max_active_jobs: int | None = None
    # local: this server's GPUs; remote: RunPod, when outsourcing is on.
    backends: dict[str, Backend]
    outsourcing: bool
    workers: Workers | None = None
    # Whether ensemble jobs are on (WORKER_ENSEMBLE).
    ensemble: bool
    # The same, for older apps.
    arrangement: bool
    paused: bool
    links_off: bool
    maintenance: NoticeResponse | None = None


def require_developer(user: AuthenticatedUser | None = Depends(current_user)) -> AuthenticatedUser:
    if user is None or not billing.is_developer(user.user_id):
        raise HTTPException(status_code=403, detail="Only for developer accounts")
    return user


@router.get("/server", response_model=ServerStateResponse)
def server_state(user: AuthenticatedUser = Depends(require_developer)) -> ServerStateResponse:
    active = sum(
        1
        for job_id in redis.smembers(job_limits.ALL_JOBS_KEY)
        if redis.hget(job_key(job_id), "status") in job_limits.ACTIVE_STATUSES
    )
    workers = routing.local_workers(redis)
    capacity, average = routing.local_capacity(redis)
    notice = maintenance.current(redis)
    return ServerStateResponse(
        active_jobs=active,
        max_active_jobs=settings.limits.max_active_jobs,
        backends=metrics.job_queues(redis),
        outsourcing=routing.outsourcing_enabled(),
        workers=Workers(
            count=len(workers),
            capacity=capacity,
            ensemble=sum(1 for worker in workers if worker["ensemble"]),
            arranging=sum(1 for worker in workers if worker["ensemble"]),
            average_job_seconds=average if any(worker["average_job_seconds"] for worker in workers) else None,
        ) if workers else None,
        ensemble=settings.worker.ensemble,
        arrangement=settings.worker.ensemble,
        paused=controls.paused(redis) is not None,
        links_off=controls.links_off(redis) is not None,
        maintenance=notice.as_response() if notice else None,
    )
