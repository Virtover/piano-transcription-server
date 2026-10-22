"""RunPod Serverless provider: /run, /status, and /cancel."""

import logging
from typing import Any

import requests

from app.config import settings
from app.outsource.provider import RemoteFailed, RemoteStatus


logger = logging.getLogger(__name__)

DEFAULT_API_BASE = "https://api.runpod.ai/v2"
# Endpoint settings (max workers) come from RunPod's management API.
REST_API_BASE = "https://rest.runpod.io/v1"
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
    def __init__(
        self,
        api_base: str,
        endpoint_id: str,
        api_key: str,
        session: Any = None,
        read_api_key: str | None = None,
    ):
        api_base = api_base.rstrip("/")
        # The SDK's local API server (`--rp_serve_api`) serves /run and
        # /status at its root, without the endpoint ID.
        self.endpoint_id = endpoint_id
        self.local = api_base != DEFAULT_API_BASE
        if self.local:
            self.base_url = api_base
        else:
            self.base_url = f"{api_base}/{endpoint_id}"
        self._headers = {"Authorization": f"Bearer {api_key}"}
        # Endpoint settings come from RunPod's management API, which rejects
        # Restricted keys. Without a separate read key, the main key is tried.
        self.has_read_key = read_api_key is not None
        self._read_headers = {"Authorization": f"Bearer {read_api_key or api_key}"}
        self.session = session or requests.Session()

    @classmethod
    def from_settings(cls, config=None) -> "RunPodProvider":
        config = config or settings
        api_key = config.secrets.runpod_api_key
        if api_key is None or not config.runpod.endpoint_id:
            raise RemoteFailed("RunPod is not configured")
        read_key = config.secrets.runpod_read_api_key
        return cls(
            config.runpod.api_base,
            config.runpod.endpoint_id,
            api_key.get_secret_value(),
            read_api_key=read_key.get_secret_value() if read_key else None,
        )

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

    def endpoint_settings(self) -> dict[str, Any]:
        """The endpoint's configuration from RunPod's management API,
        including its template (image and environment). Restricted keys
        cannot use that API; see runpod_read_api_key."""
        try:
            response = self.session.request(
                "GET",
                f"{REST_API_BASE}/endpoints/{self.endpoint_id}",
                params={"includeTemplate": "true"},
                headers=self._read_headers,
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as error:
            raise RemoteFailed(f"RunPod is unreachable ({type(error).__name__})") from error
        return self._json(response, "endpoint settings")

    def max_workers(self) -> int | None:
        """The endpoint's max workers setting, read from RunPod's management
        API. Restricted keys cannot use that API; see runpod_read_api_key."""
        if self.local:
            # The SDK's local test server runs a single worker.
            return 1
        try:
            response = self.session.request(
                "GET",
                f"{REST_API_BASE}/endpoints/{self.endpoint_id}",
                headers=self._read_headers,
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as error:
            logger.warning("Could not read the RunPod endpoint settings (%s)", type(error).__name__)
            return None
        if not response.ok:
            logger.warning(
                "Could not read the RunPod endpoint settings (HTTP %s). Restricted keys cannot read "
                "them: save a key with read access as secrets/runpod_read_api_key, or set "
                "OUTSOURCE_MAX_CONCURRENCY to a number",
                response.status_code,
            )
            return None
        try:
            workers = int(response.json()["workersMax"])
        except (ValueError, KeyError, TypeError):
            logger.warning("The RunPod endpoint settings have no workersMax")
            return None
        return workers if workers > 0 else None

    def cancel(self, remote_id: str) -> None:
        try:
            response = self._request("POST", f"cancel/{remote_id}", REQUEST_TIMEOUT)
            if not response.ok:
                logger.warning("RunPod cancel of %s returned HTTP %s", remote_id, response.status_code)
        except RemoteFailed as error:
            logger.warning("Could not cancel RunPod job %s: %s", remote_id, error)
