"""RunPod Serverless handler: Transkun inference for the piano transcription server.

Input:  {"audio_url": "<presigned GET URL on OUTSOURCE_STORAGE_ENDPOINT>", "job_id": "..."}
Output: {"midi_base64": "...", "timing": {...}} or {"error": "..."}

The audio is FLAC at 44.1 kHz mono, prepared by the server's remote worker.

One worker takes several requests at once and transcribes them together in
the same batcher that local jobs use: the worker's concurrency is the
batcher's batch size, which is the GPU's job capacity from its free VRAM
(WORKER_MEMORY_PER_JOB_GIB, WORKER_BATCH_SIZE, and WORKER_MAX_CONCURRENCY
apply as on the server). The model is loaded once when the worker starts.

Run locally with the SDK's test server: python handler.py --rp_serve_api --rp_api_port 8010
"""

import asyncio
import base64
import logging
import os
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# RunPod sets many RUNPOD_* variables of its own; they are not typos.
logging.getLogger("app.config").setLevel(logging.ERROR)

import runpod  # noqa: E402
import soundfile  # noqa: E402

from app.config import settings  # noqa: E402
from app.outsource.handler_input import InvalidInput, download, validate_input  # noqa: E402
from app.transcription.batching import GpuOutOfMemory  # noqa: E402
from app.transcription.piano_transcription import (  # noqa: E402
    audio_for_model,
    get_batcher,
    select_device,
    write_midi,
)


logger = logging.getLogger("handler")

# Base64 MIDI is returned in the job output. RunPod limits job payloads to
# 10 MB for /run, so larger results are refused instead of being cut off.
MAX_OUTPUT_BYTES = 8 * 1024 * 1024
PROGRESS_INTERVAL = 2.0

DEVICE = select_device()
BATCHER = get_batcher(DEVICE)
MODEL = BATCHER.model
# One thread per request in flight; most of them wait on the batcher.
EXECUTOR = ThreadPoolExecutor(max_workers=max(4, BATCHER.batch_size * 2), thread_name_prefix="request")


class Progress:
    """Sends progress to RunPod at most every PROGRESS_INTERVAL seconds."""

    def __init__(self, job: dict):
        self.job = job
        self.sent_at = 0.0
        # Only set on RunPod; the local test server has nowhere to send it.
        self.enabled = bool(os.environ.get("RUNPOD_WEBHOOK_POST_OUTPUT"))

    def __call__(self, value: float) -> None:
        now = time.monotonic()
        if self.enabled and (now - self.sent_at >= PROGRESS_INTERVAL or value >= 1.0):
            self.sent_at = now
            runpod.serverless.progress_update(self.job, {"progress": round(value, 4)})


def transcribe_job(job: dict) -> dict:
    """Handle one request. Runs in a thread; the batcher combines requests
    that arrive within WORKER_BATCH_TIMEOUT of each other."""
    started = time.monotonic()
    try:
        audio_url, job_id = validate_input(job.get("input"), settings.outsource.storage_endpoint)
        with tempfile.TemporaryDirectory() as directory:
            audio_path = Path(directory) / "audio.flac"
            size = download(audio_url, audio_path, int(settings.outsource.max_audio_mb * 1024 * 1024))
            downloaded = time.monotonic()
            try:
                audio, sample_rate = soundfile.read(audio_path, dtype="float32", always_2d=True)
            except RuntimeError as error:
                raise InvalidInput("The audio file cannot be decoded") from error

            events = BATCHER.submit(
                audio_for_model(MODEL, sample_rate, audio),
                Progress(job),
                None,
            )
            transcribed = time.monotonic()

            midi_path = Path(directory) / "transcription.mid"
            write_midi(events, midi_path)
            midi = midi_path.read_bytes()

        encoded = base64.b64encode(midi).decode("ascii")
        if len(encoded) > MAX_OUTPUT_BYTES:
            return {"error": "The MIDI result is too large to return"}
        audio_seconds = len(audio) / sample_rate
        logger.info("Job %s: %d notes from %.0f s of audio", job_id, len(events), audio_seconds)
        return {
            "midi_base64": encoded,
            "timing": {
                "audio_seconds": round(audio_seconds, 2),
                "download_seconds": round(downloaded - started, 2),
                "inference_seconds": round(transcribed - downloaded, 2),
                "total_seconds": round(time.monotonic() - started, 2),
                "audio_bytes": size,
                "device": DEVICE,
            },
        }
    except (InvalidInput, GpuOutOfMemory) as error:
        return {"error": str(error)}
    except Exception as error:
        logger.exception("Transcription failed")
        return {"error": f"Transcription failed on the remote worker ({type(error).__name__})"}


async def handler(job: dict) -> dict:
    # Async, so the SDK can run several requests on this worker at once.
    return await asyncio.get_running_loop().run_in_executor(EXECUTOR, transcribe_job, job)


def concurrency(current: int) -> int:
    """How many requests this worker takes at once: the batch size, which
    drops if the GPU runs out of memory."""
    return BATCHER.batch_size


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    logger.info("%s: up to %d requests at once", DEVICE, BATCHER.batch_size)
    runpod.serverless.start({"handler": handler, "concurrency_modifier": concurrency})
