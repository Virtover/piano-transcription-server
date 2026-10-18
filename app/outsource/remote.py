"""The remote dispatcher's work for one job.

Download and conversion run here, as for local jobs, because datacenter IPs
are often blocked by video sites. Only the model inference runs remotely.
"""

import base64
import binascii
import logging
import time
from pathlib import Path
from typing import Any

from app.config import settings
from app.outsource.capacity import record_jobs_per_worker
from app.outsource.provider import RemoteFailed, RemoteProvider, get_provider
from app.outsource.storage import AudioStorage, S3Storage
from app.transcription.audio import check_midi, convert_to_flac, fetch_audio
from app.transcription.cancellation import TranscriptionCancelled


logger = logging.getLogger(__name__)

POLL_INTERVAL = 2.0
# Consecutive status errors tolerated before giving up on a remote job.
MAX_STATUS_ERRORS = 5
# The remote model reports 0 to 1; locally that range is 0.15 to 0.95,
# the same as model progress for local jobs.
PROGRESS_START = 0.15
PROGRESS_END = 0.95
# Presigned URLs stay valid a little longer than the remote job may run.
URL_GRACE_SECONDS = 5 * 60


def redis_client():
    from app.worker.tasks import redis

    return redis


def map_progress(remote_progress: float) -> float:
    remote_progress = min(1.0, max(0.0, remote_progress))
    return round(PROGRESS_START + (PROGRESS_END - PROGRESS_START) * remote_progress, 4)


def decode_midi(output: dict[str, Any]) -> bytes:
    encoded = output.get("midi_base64")
    if not isinstance(encoded, str) or not encoded:
        raise RemoteFailed("Remote transcription returned no MIDI")
    try:
        midi = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as error:
        raise RemoteFailed("Remote transcription returned invalid MIDI") from error
    if not midi.startswith(b"MThd"):
        raise RemoteFailed("Remote transcription returned invalid MIDI")
    return midi


def wait_for_result(context, provider: RemoteProvider, remote_id: str) -> dict[str, Any]:
    try:
        return _poll(context, provider, remote_id)
    except TranscriptionCancelled:
        # Also reached when a progress update notices the cancellation.
        provider.cancel(remote_id)
        raise


def _poll(context, provider: RemoteProvider, remote_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + settings.outsource.timeout
    errors = 0
    while True:
        if context.is_cancelled():
            raise TranscriptionCancelled()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            provider.cancel(remote_id)
            raise RemoteFailed("Remote transcription timed out")

        try:
            status = provider.status(remote_id, timeout=max(30.0, remaining))
        except RemoteFailed as error:
            errors += 1
            if errors >= MAX_STATUS_ERRORS:
                provider.cancel(remote_id)
                raise
            logger.warning("Status of remote job %s unavailable (%s); retrying", remote_id, error)
            time.sleep(POLL_INTERVAL)
            continue
        errors = 0
        if status.state == "completed":
            return status.output or {}
        if status.state in ("failed", "cancelled"):
            logger.warning("Remote job %s %s: %s", remote_id, status.state, status.error)
            raise RemoteFailed(f"Remote transcription {status.state}")
        if status.state == "running" and status.progress is not None:
            context.progress(map_progress(status.progress))
        time.sleep(POLL_INTERVAL)


def transcribe_remotely(
    context,
    source_url: str | None = None,
    upload_path: Path | None = None,
    upload_title: str | None = None,
    provider: RemoteProvider | None = None,
    storage: AudioStorage | None = None,
) -> Path:
    """Fetch and convert audio, transcribe it remotely, and write the MIDI.

    Provider and storage problems raise RemoteFailed, which the job lifecycle
    can turn into a local retry. The storage object is always deleted.
    """
    output_dir = context.output_dir
    audio = fetch_audio(
        output_dir,
        source_url=source_url,
        upload_path=upload_path,
        upload_title=upload_title,
        progress_callback=context.progress,
        metadata_callback=context.metadata,
        cancellation_callback=context.is_cancelled,
    )
    context.progress(0.07)
    flac = convert_to_flac(audio.path, output_dir, context.is_cancelled)

    key = None
    try:
        storage = storage or S3Storage.from_settings()
        provider = provider or get_provider()
        key = storage.upload(flac)
        url = storage.presigned_url(key, int(settings.outsource.timeout) + URL_GRACE_SECONDS)
        context.progress(0.10)

        remote_id = provider.submit({"audio_url": url, "job_id": context.job_id})
        context.set(remote_id=remote_id)
        output = wait_for_result(context, provider, remote_id)

        midi = decode_midi(output)
        context.progress(0.96)
        midi_path = output_dir / "transcription.mid"
        midi_path.write_bytes(midi)
        check_midi(midi_path)
        record_jobs_per_worker(redis_client(), output.get("worker_capacity"))
        timing = output.get("timing")
        if isinstance(timing, dict):
            logger.info("Remote job %s timing: %s", remote_id, timing)
        return midi_path
    finally:
        if key is not None:
            storage.delete(key)
        for path in (audio.path, flac):
            path.unlink(missing_ok=True)
