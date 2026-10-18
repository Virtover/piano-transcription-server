"""RunPod Serverless provider: /run, /status, and /cancel."""

import logging
from typing import Any

import requests

from app.config import settings
from app.outsource.provider import RemoteFailed, RemoteStatus


logger = logging.getLogger(__name__)

DEFAULT_API_BASE = "https://api.runpod.ai/v2"
REQUEST_TIMEOUT = 30

STATES = {
    "IN_QUEUE": "queued",
    "IN_PROGRESS": "running",
    "COMPLETED": "completed",
    "FAILED": "failed",
    "CANCELLED": "cancelled",
    "TIMED_OUT": "failed",
}


class RunPodProvider:
    def __init__(self, api_base: str, endpoint_id: str, api_key: str, session: Any = None):
        api_base = api_base.rstrip("/")
        # The SDK's local API server (`--rp_serve_api`) serves /run and
        # /status at its root, without the endpoint ID.
        if api_base == DEFAULT_API_BASE:
            self.base_url = f"{api_base}/{endpoint_id}"
        else:
            self.base_url = api_base
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self.session = session or requests.Session()

    @classmethod
    def from_settings(cls) -> "RunPodProvider":
        api_key = settings.secrets.runpod_api_key
        if api_key is None or not settings.runpod.endpoint_id:
            raise RemoteFailed("RunPod is not configured")
        return cls(settings.runpod.api_base, settings.runpod.endpoint_id, api_key.get_secret_value())

    def _request(self, method: str, path: str, timeout: float, **kwargs) -> requests.Response:
        try:
            response = self.session.request(
                method,
                f"{self.base_url}/{path}",
                headers=self._headers,
                timeout=timeout,
                **kwargs,
            )
        except requests.RequestException as error:
            raise RemoteFailed(f"RunPod is unreachable ({type(error).__name__})") from error
        return response

    def _json(self, response: requests.Response, action: str) -> dict[str, Any]:
        if not response.ok:
            raise RemoteFailed(f"RunPod {action} failed with HTTP {response.status_code}")
        try:
            return response.json()
        except ValueError as error:
            raise RemoteFailed(f"RunPod {action} returned invalid JSON") from error

    def submit(self, payload: dict[str, Any]) -> str:
        data = self._json(self._request("POST", "run", REQUEST_TIMEOUT, json={"input": payload}), "submit")
        remote_id = data.get("id")
        if not isinstance(remote_id, str) or not remote_id:
            raise RemoteFailed("RunPod did not return a job ID")
        return remote_id

    def status(self, remote_id: str, timeout: float = REQUEST_TIMEOUT) -> RemoteStatus:
        response = self._request("GET", f"status/{remote_id}", timeout)
        if response.status_code == 405:
            # The local API server only accepts POST, and runs the handler
            # during this request, so it can take as long as the job.
            response = self._request("POST", f"status/{remote_id}", timeout)
        data = self._json(response, "status")
        state = STATES.get(str(data.get("status")))
        if state is None:
            raise RemoteFailed(f"RunPod returned an unknown status {data.get('status')!r}")

        output = data.get("output")
        if state == "running":
            progress = output.get("progress") if isinstance(output, dict) else None
            return RemoteStatus(state, progress=float(progress) if isinstance(progress, (int, float)) else None)
        if state == "completed":
            if not isinstance(output, dict):
                raise RemoteFailed("RunPod returned no output")
            if output.get("error"):
                return RemoteStatus("failed", error=str(output["error"]))
            return RemoteStatus(state, output=output)
        if state == "failed":
            return RemoteStatus(state, error=str(data.get("error") or data.get("status")))
        return RemoteStatus(state)

    def cancel(self, remote_id: str) -> None:
        try:
            response = self._request("POST", f"cancel/{remote_id}", REQUEST_TIMEOUT)
            if not response.ok:
                logger.warning("RunPod cancel of %s returned HTTP %s", remote_id, response.status_code)
        except RemoteFailed as error:
            logger.warning("Could not cancel RunPod job %s: %s", remote_id, error)
