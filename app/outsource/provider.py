from dataclasses import dataclass
from typing import Any, Literal, Protocol

from app.config import settings


RemoteState = Literal["queued", "running", "completed", "failed", "cancelled"]


class RemoteFailed(RuntimeError):
    """The remote provider could not transcribe the job. The message is safe
    to show to users: it never contains credentials or presigned URLs."""


@dataclass(frozen=True)
class RemoteStatus:
    state: RemoteState
    # 0 to 1 while running, when the provider reports it.
    progress: float | None = None
    output: dict[str, Any] | None = None
    error: str | None = None


class RemoteProvider(Protocol):
    """What routing and the dispatcher need from a provider. Add another
    provider by implementing these three methods and listing it in
    OUTSOURCE_PROVIDER."""

    def submit(self, payload: dict[str, Any]) -> str:
        """Start a remote job and return its ID."""

    def status(self, remote_id: str, timeout: float) -> RemoteStatus:
        """Current state of a remote job."""

    def cancel(self, remote_id: str) -> None:
        """Ask the provider to stop a remote job. Must not raise."""


def get_provider() -> RemoteProvider:
    if settings.outsource.provider == "runpod":
        from app.outsource.runpod import RunPodProvider

        return RunPodProvider.from_settings()
    raise RemoteFailed("Outsourcing is disabled")
