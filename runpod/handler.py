"""RunPod Serverless handler: Transkun inference for the piano transcription server, and
dettato for its ensemble jobs.

Input:  {"audio_url": "<presigned GET URL on STORAGE_ENDPOINT>", "job_id": "...",
         "mode": "transcription" | "ensemble", "source_rate": <Hz, ensemble only>}
Output: {"midi_base64": "...", "timing": {...}} or {"error": "..."}

The audio is FLAC at 44.1 kHz mono, prepared by the server's remote worker.

One worker takes several requests at once and transcribes them together in
the same batcher that local jobs use: the worker's concurrency is the
batcher's batch size, which is the GPU's job capacity from its free VRAM
(WORKER_MEMORY_PER_JOB_GIB, WORKER_BATCH_SIZE, and WORKER_MAX_CONCURRENCY
apply as on the server). The model and the beat tracker are loaded once when the worker starts.

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
from app.outsource.handler_input import InvalidInput, download, requested_ensemble, validate_input  # noqa: E402
from app.transcription.batching import GpuOutOfMemory  # noqa: E402
from app.transcription.piano_transcription import (  # noqa: E402
    audio_for_model,
    get_batcher,
    select_device,
    write_midi,
)
from app.transcription.tempo import preload_tracker  # noqa: E402


logger = logging.getLogger("handler")

# Base64 MIDI is returned in the job output. RunPod limits job payloads to
# 10 MB for /run, so larger results are refused instead of being cut off.
MAX_OUTPUT_BYTES = 8 * 1024 * 1024
PROGRESS_INTERVAL = 2.0

DEVICE = select_device()
BATCHER = get_batcher(DEVICE)
MODEL = BATCHER.model
try:
    preload_tracker(DEVICE)
except Exception:
    # Each request tries again, and keeps its MIDI at 120 BPM if it cannot.
    logger.warning("Could not load the beat tracker", exc_info=True)
# Ensemble jobs (WORKER_ENSEMBLE on the endpoint), beside Transkun on the
# same GPU and in the same memory budget.
ENSEMBLE_DEVICE = None
if settings.worker.ensemble:
    from app.transcription import ensemble_transcription  # noqa: E402

    try:
        ENSEMBLE_DEVICE = ensemble_transcription.select_ensemble_device()
        ensemble_transcription.get_models(ENSEMBLE_DEVICE)
    except ensemble_transcription.EnsembleUnavailable:
        ENSEMBLE_DEVICE = None
        logger.warning("WORKER_ENSEMBLE is on, but this GPU has no room for ensemble jobs")
    except Exception:
        # Transcriptions still run; ensemble requests are refused.
        ENSEMBLE_DEVICE = None
        logger.exception("Could not load the ensemble models; ensemble jobs are off on this worker")
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
        audio_url, job_id = validate_input(job.get("input"), settings.storage.endpoint)
        ensemble, source_rate = requested_ensemble(job["input"])
        if ensemble and ENSEMBLE_DEVICE is None:
            raise InvalidInput("Ensemble jobs are off on this endpoint: set WORKER_ENSEMBLE=true")
        with tempfile.TemporaryDirectory() as directory:
            audio_path = Path(directory) / "audio.flac"
            size = download(audio_url, audio_path, int(settings.outsource.max_audio_mb * 1024 * 1024))
            downloaded = time.monotonic()
            try:
                audio, sample_rate = soundfile.read(audio_path, dtype="float32", always_2d=True)
            except RuntimeError as error:
                raise InvalidInput("The audio file cannot be decoded") from error

            midi_path = Path(directory) / "transcription.mid"
            if ensemble:
                # Its resampled copies as files in the request's folder, not in memory.
                tracks, _ = ensemble_transcription.transcribe_ensemble(
                    audio, sample_rate, source_rate, ENSEMBLE_DEVICE, Progress(job), None, None, Path(directory))
                transcribed = time.monotonic()
                ensemble_transcription.write_result(tracks, midi_path, audio, sample_rate, ENSEMBLE_DEVICE)
                note_count = sum(len(track.notes) for track in tracks)
            else:
                model_audio = audio_for_model(MODEL, sample_rate, audio)
                events = BATCHER.submit(model_audio, Progress(job), None)
                transcribed = time.monotonic()
                # Beat tracking on the GPU, in memory its job capacity reserves.
                write_midi(events, midi_path, model_audio, MODEL.fs, DEVICE)
                note_count = len(events)
            midi = midi_path.read_bytes()

        encoded = base64.b64encode(midi).decode("ascii")
        if len(encoded) > MAX_OUTPUT_BYTES:
            return {"error": "The MIDI result is too large to return"}
        audio_seconds = len(audio) / sample_rate
        logger.info("Job %s: %d notes from %.0f s of audio", job_id, note_count, audio_seconds)
        return {
            "midi_base64": encoded,
            # Requests this worker takes at once, for OUTSOURCE_MAX_CONCURRENCY=auto.
            "worker_capacity": BATCHER.batch_size,
            "timing": {
                "audio_seconds": round(audio_seconds, 2),
                "download_seconds": round(downloaded - started, 2),
                "inference_seconds": round(transcribed - downloaded, 2),
                "total_seconds": round(time.monotonic() - started, 2),
                "audio_bytes": size,
                "device": ENSEMBLE_DEVICE if ensemble else DEVICE,
                "mode": "ensemble" if ensemble else "transcription",
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
