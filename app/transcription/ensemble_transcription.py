"""Ensemble jobs: every instrument of a recording as its own MIDI track, plus drums, by dettato
(app/transcription/dettato, run in bounded memory by app/transcription/ensemble).

Ensemble jobs replaced piano arrangements (AMT-APC piano covers). Where a name is stored or sent between versions, it
keeps the old word: the queue and the stored mode are "arrangement", the cluster switch "arrange"
(app/worker/tasks.py, app/cluster.py).

On a GPU each job runs in its own worker thread, in three phases, each holding WORKER_ENSEMBLE_MEMORY_GIB of the
device's memory budget (app/transcription/budget.py) beside Transkun's steps:

    1. the transcriber pass, with the evidence repairer and the grouper's features per segment (ensemble/pitched.py);
    2. the grouper network; its decode then runs on the CPU without a hold;
    3. the drums (ensemble/drums.py).

A job holds one hold at a time, so holds never wait for each other. On the CPU the whole job runs dettato.cpp in a
process of its own (app/transcription/ensemble/cpu.py), which is several times faster there than PyTorch; the CPU
budget runs one step at a time.
"""

import gc
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np
import torch

from app.config import settings
from app.errors import UserFacingError
from app.transcription.batching import GpuOutOfMemory
from app.transcription.budget import StepCancelled
from app.transcription.cancellation import TranscriptionCancelled
from app.transcription.device import ensemble_memory_gib
from app.transcription.ensemble import drums as ensemble_drums
from app.transcription.ensemble import midi as ensemble_midi
from app.transcription.ensemble import pitched
from app.transcription import stream_audio
from app.transcription.piano_transcription import (
    CancellationCallback,
    ProgressCallback,
    _cuda_capacities_lock,
    ensemble_offered,
    budget_for_device,
    configure_cpu_threads,
    is_cuda_out_of_memory,
    read_audio,
    select_device,
    write_midi_file,
)
import app.transcription.piano_transcription as piano_transcription


logger = logging.getLogger(__name__)

SAMPLE_RATE = 44100
MODEL_RATE = 16000


class EnsembleUnavailable(UserFacingError, RuntimeError):
    def __init__(self) -> None:
        super().__init__("Ensemble transcriptions are not available on this server")


@dataclass
class Models:
    device: str
    transcriber: object
    repairer: object
    grouper: object
    drums: object
    # The grouper turns PyTorch's deterministic algorithms on for itself, a process-wide switch: one grouper at a time.
    grouper_lock: threading.Lock


_models: dict[str, Models] = {}
_models_lock = threading.Lock()


def get_models(device: str) -> Models:
    """The four networks on device, loaded once per process."""
    with _models_lock:
        models = _models.get(device)
        if models is None:
            from app.transcription.dettato.drums import DrumTranscriber
            from app.transcription.dettato.evidence import EvidenceRepairer
            from app.transcription.dettato.grouper import Grouper
            from app.transcription.dettato.transcriber import Transcriber

            started = time.monotonic()
            models = Models(device, Transcriber.from_pretrained(device), EvidenceRepairer.from_pretrained(device),
                            Grouper.from_pretrained(device), ensemble_drums.make_long(DrumTranscriber.from_pretrained(device)),
                            threading.Lock())
            _models[device] = models
            logger.info("Ensemble models loaded on %s in %.1f s", device, time.monotonic() - started)
        return models


def select_ensemble_device() -> str:
    """The device for an ensemble job: the GPU with the most free memory of those that take them, or the CPU when the
    worker runs on it."""
    device = select_device()
    if device == "cpu":
        if not ensemble_offered("cpu"):
            raise EnsembleUnavailable()
        return device
    with _cuda_capacities_lock:
        candidates = [index for index in piano_transcription._cuda_capacities or {} if ensemble_offered(f"cuda:{index}")]
    if not candidates:
        raise EnsembleUnavailable()
    if len(candidates) == 1:
        return f"cuda:{candidates[0]}"
    return f"cuda:{max(candidates, key=lambda index: torch.cuda.mem_get_info(index)[0])}"


def preload() -> None:
    """Load the ensemble models on their device, so that the first job does not wait for them. Nothing when ensemble
    jobs are off or do not fit on this worker's GPUs. On the CPU, checks that dettato.cpp is there."""
    if not settings.worker.ensemble:
        return
    try:
        device = select_ensemble_device()
    except EnsembleUnavailable:
        logger.warning("Ensemble jobs are on, but no GPU of this worker has room for them")
        return
    if device == "cpu":
        from app.transcription.ensemble import cpu

        cpu.check()
        return
    get_models(device)


def _free_memory(device: str) -> None:
    gc.collect()
    if device.startswith("cuda:"):
        torch.cuda.empty_cache()


def _phase(device: str, cancelled: Callable[[], bool], run: Callable[[], object]):
    """run() in the device's budget, holding WORKER_ENSEMBLE_MEMORY_GIB. On a GPU out of memory (another program
    took it), the cache is freed and run() tried once more."""
    budget = budget_for_device(device)
    for attempt in range(2):
        try:
            with budget.use(ensemble_memory_gib(), cancelled), torch.inference_mode():
                return run()
        except StepCancelled:
            raise TranscriptionCancelled()
        except pitched.Cancelled:
            raise TranscriptionCancelled()
        except Exception as error:
            if not is_cuda_out_of_memory(error):
                raise
            _free_memory(device)
            if attempt:
                logger.warning("%s ran out of memory for an ensemble step", device)
                raise GpuOutOfMemory()
    raise AssertionError("unreachable")


def _reader(audio, sample_rate: int):
    """A 44.1 kHz mono reader of audio: a stream_audio reader as it is, or an array (samples, or samples x channels,
    at sample_rate) averaged to mono and resampled as dettato resamples."""
    if hasattr(audio, "read") and getattr(audio, "rate", None) == SAMPLE_RATE:
        return audio
    if hasattr(audio, "read"):
        return stream_audio.ArrayReader(stream_audio.resample(audio, SAMPLE_RATE), SAMPLE_RATE)
    a = np.asarray(audio, np.float32)
    x44 = a.mean(axis=1).astype(np.float32) if a.ndim == 2 else a
    if sample_rate != SAMPLE_RATE:
        import soxr

        x44 = soxr.resample(x44, sample_rate, SAMPLE_RATE, quality="HQ").astype(np.float32)
    return stream_audio.ArrayReader(x44, SAMPLE_RATE)


def transcribe_ensemble(
    audio,
    sample_rate: int,
    source_rate: int | None,
    device: str,
    progress_callback: ProgressCallback | None = None,
    cancellation_callback: CancellationCallback | None = None,
    stats: dict | None = None,
    work_dir: Path | None = None,
) -> tuple[list[ensemble_midi.Track], dict]:
    """The tracks of audio (samples, or samples x channels, at sample_rate; or a stream_audio reader, such as the
    converted WAV on disk), whose source was recorded at source_rate (for the drums; None: unknown). progress_callback
    gets 0..1. With work_dir (the job's folder), every large copy of the audio is a file in a folder there, removed
    when the job ends, so memory does not grow with the recording's length."""
    stats = stats if stats is not None else {}
    cancelled = cancellation_callback or (lambda: False)
    report = progress_callback or (lambda value: None)
    reader = _reader(audio, sample_rate)
    if device == "cpu":
        from app.transcription.ensemble import cpu

        with budget_for_device(device).use(settings.worker.cpu_ensemble_memory_gib, cancelled):
            return cpu.transcribe(reader, SAMPLE_RATE, source_rate, report, cancelled, stats, work_dir)
    models = get_models(device)
    started = time.monotonic()
    with stream_audio.scratch(work_dir, "ensemble-") as directory:
        work = Path(directory) if work_dir is not None else None
        try:
            # as dettato.audio.Audio.at resamples
            x16 = stream_audio.resample(reader, MODEL_RATE, work / "audio16k.f32" if work else None)
            state = _phase(device, cancelled, lambda: pitched.transcribe(
                models, x16, lambda value: report(0.85 * value), cancelled, stats, work))
            if cancelled():
                raise TranscriptionCancelled()

            def predict():
                with models.grouper_lock:
                    return pitched.predict(models, state)

            pred = _phase(device, cancelled, predict)
            report(0.88)
            groups, info = pitched.group(models, state, pred)
            del state, pred, x16
            if cancelled():
                raise TranscriptionCancelled()
            report(0.9)
            hits, drum_info = _phase(device, cancelled,
                                     lambda: ensemble_drums.transcribe(models.drums, reader, source_rate, work))
            report(1.0)
        finally:
            _free_memory(device)
    tracks = [ensemble_midi.Track(g["instrument"].replace("_", " "), int(g["program"]), False,
                                  [(float(a), float(b), int(p), int(v)) for a, b, p, v in g["notes"]]) for g in groups]
    if hits:
        tracks.append(ensemble_midi.Track("drums", 0, True, [(h["onset"], h["offset"], h["pitch"], h["velocity"]) for h in hits]))
    info["drums"] = drum_info
    stats["model_s"] = time.monotonic() - started
    return tracks, info


def write_result(tracks: list[ensemble_midi.Track], output_path: Path, audio, sample_rate: int,
                 device: str) -> None:
    """The MIDI file of an ensemble result, with the tempo map of audio (an array, or a stream_audio reader), written
    atomically."""
    write_midi_file(lambda path: ensemble_midi.build(tracks).save(str(path)), output_path, audio, sample_rate,
                    "cpu" if device == "cpu" else device)


def transcribe_ensemble_file(
    audio_path: Path,
    output_path: Path,
    source_rate: int | None = None,
    progress_callback: ProgressCallback | None = None,
    cancellation_callback: CancellationCallback | None = None,
    model_done_callback: Callable[[], None] | None = None,
) -> None:
    """Transcribe every instrument of one audio file as MIDI; model_done_callback as transcribe_piano's. Progress as
    transcribe_piano's:

        0.07 - 0.15   model and audio loading
        0.15 - 0.95   the models
        0.95 - 1.00   beat tracking and MIDI writing
    """
    device = select_ensemble_device()
    if cancellation_callback and cancellation_callback():
        raise TranscriptionCancelled()
    if progress_callback:
        progress_callback(0.07)
    if device == "cpu":
        configure_cpu_threads()
    else:
        get_models(device)
    try:
        # read from disk a block at a time: memory does not grow with the length
        audio = stream_audio.Pcm16Wav(audio_path)
        sample_rate = audio.rate
    except ValueError:
        sample_rate, audio = read_audio(audio_path)
    if cancellation_callback and cancellation_callback():
        raise TranscriptionCancelled()
    if progress_callback:
        progress_callback(0.15)

    def report(model_progress: float) -> None:
        if progress_callback:
            progress_callback(max(0.15, min(0.15 + 0.80 * model_progress, 0.95)))

    stats: dict = {}
    tracks, info = transcribe_ensemble(audio, sample_rate, source_rate, device, report, cancellation_callback, stats,
                                       output_path.parent)
    if model_done_callback:
        model_done_callback()
    if progress_callback:
        progress_callback(0.96)
    write_result(tracks, output_path, audio, sample_rate, device)
    logger.info("Ensemble on %s: %d tracks, %d notes, %.0f s of audio in %.1f s (%s)", device, len(tracks),
                sum(len(t.notes) for t in tracks), len(audio) / sample_rate, stats.get("model_s", 0.0),
                {k: (round(v, 2) if isinstance(v, float) else v) for k, v in stats.items() if k != "model_s"})
    if progress_callback:
        progress_callback(1.0)
