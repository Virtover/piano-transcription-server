"""Piano arrangements: a piano cover of a song, by AMT-APC (app/transcription/amt_apc).

AMT-APC reads the song in windows of 512 frames (about 8.2 s) that do not
depend on each other. One batcher thread per device owns the model and runs
one window at a time, taking the next window of each job in turn, so every
job progresses and a new job starts at the next turn. Each window runs in
the device's memory budget (app/transcription/budget.py), beside Transkun's
steps.
"""

import gc
import logging
import queue
import threading
from concurrent.futures import Future
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import torch
import torchaudio

from app.config import settings
from app.errors import UserFacingError
from app.transcription import amt_apc
from app.transcription.batching import GpuOutOfMemory, fail, is_cancelled, report_progress, resolve, wait_for
from app.transcription.budget import StepCancelled
from app.transcription.cancellation import TranscriptionCancelled
from app.transcription.piano_transcription import (
    CancellationCallback,
    ProgressCallback,
    _cuda_capacities_lock,
    arrangement_offered,
    budget_for_device,
    configure_cpu_threads,
    is_cuda_out_of_memory,
    read_audio,
    select_device,
    write_midi,
)
import app.transcription.piano_transcription as piano_transcription


logger = logging.getLogger(__name__)

# Put there by the worker and RunPod images, checked against its checksum.
WEIGHTS = Path("/opt/amt-apc/apc.pth")


class ArrangementUnavailable(UserFacingError, RuntimeError):
    def __init__(self) -> None:
        super().__init__("Piano arrangements are not available on this server")


# eq=False: requests are told apart by identity; comparing their arrays
# would raise.
@dataclass(eq=False)
class _ArrangementRequest:
    padded: torch.Tensor
    starts: list[int]
    frame_count: int
    style: np.ndarray
    progress_callback: ProgressCallback | None
    cancellation_callback: CancellationCallback | None
    result: Future
    reported_progress: float = 0.0
    index: int = 0
    # The windows' onset, offset, frame, and velocity outputs, in order.
    outputs: list[tuple[np.ndarray, ...]] = field(default_factory=list)


def _join(request: _ArrangementRequest) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    if not request.outputs:
        # Audio too short for one frame: no notes.
        empty = np.zeros((0, 88), dtype=np.float32)
        return empty, empty, empty, np.zeros((0, 88), dtype=np.int8)
    onset, offset, mpe, velocity = (
        np.concatenate([output[part] for output in request.outputs])[: request.frame_count]
        for part in range(4)
    )
    return onset, offset, mpe, velocity


class _ArrangementBatcher:
    def __init__(self, device: str):
        self.device = device
        if not WEIGHTS.is_file():
            raise FileNotFoundError(f"{WEIGHTS} is missing; the worker and RunPod images include it")
        self.model = amt_apc.load_model(WEIGHTS, device)
        self.budget = budget_for_device(device)
        worker = settings.worker
        self.step_memory_gib = (
            worker.arrangement_memory_gib if device.startswith("cuda:") else worker.cpu_arrangement_memory_gib
        )
        self.requests: queue.Queue[_ArrangementRequest] = queue.Queue()
        self.thread = threading.Thread(target=self._run, name=f"arrangement-batcher-{device}", daemon=True)
        self.thread.start()

    def submit(
        self,
        feature: np.ndarray,
        style: str,
        progress_callback: ProgressCallback | None,
        cancellation_callback: CancellationCallback | None,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """The onset, offset, frame, and velocity outputs of every frame of
        feature, once all its windows ran."""
        request = _ArrangementRequest(
            padded=amt_apc.pad_features(feature),
            starts=amt_apc.segment_starts(len(feature)),
            frame_count=len(feature),
            style=amt_apc.style_vector(style),
            progress_callback=progress_callback,
            cancellation_callback=cancellation_callback,
            result=Future(),
        )
        self.requests.put(request)
        return wait_for(request.result, self.thread)

    def _run(self) -> None:
        active: list[_ArrangementRequest] = []
        while True:
            if not active:
                active.append(self.requests.get())
            while True:
                try:
                    active.append(self.requests.get_nowait())
                except queue.Empty:
                    break
            for request in list(active):
                try:
                    self._turn(request)
                except Exception as error:
                    fail(request, error)
                if request.result.done():
                    active.remove(request)
                    request.outputs.clear()

    def _turn(self, request: _ArrangementRequest) -> None:
        """Run the request's next window, or finish it."""
        if is_cancelled(request):
            fail(request, TranscriptionCancelled())
            return
        if not report_progress(request, request.index / max(1, len(request.starts))):
            return
        if request.index >= len(request.starts):
            resolve(request, _join(request))
            return
        window = amt_apc.segment_input(request.padded, request.starts[request.index])
        for attempt in range(2):
            try:
                with self.budget.use(self.step_memory_gib, lambda: is_cancelled(request)), torch.inference_mode():
                    output = amt_apc.run_segment(self.model, window, request.style, self.device)
                break
            except StepCancelled:
                fail(request, TranscriptionCancelled())
                return
            except Exception as error:
                if not is_cuda_out_of_memory(error):
                    raise
                # One window is the smallest step: free what is cached and
                # try once more, as when another program took the memory.
                self._free_memory()
                if attempt:
                    logger.warning("%s ran out of memory for an arrangement window", self.device)
                    fail(request, GpuOutOfMemory())
                    return
        request.outputs.append(output)
        request.index += 1

    def _free_memory(self) -> None:
        gc.collect()
        if self.device.startswith("cuda:"):
            torch.cuda.empty_cache()


_batchers: dict[str, _ArrangementBatcher] = {}
_batchers_lock = threading.Lock()


def get_batcher(device: str) -> _ArrangementBatcher:
    """The process-wide arrangement batcher for a device; it loads the model
    on first use."""
    with _batchers_lock:
        batcher = _batchers.get(device)
        # A batcher whose thread ended is replaced, so the worker recovers.
        if batcher is None or not batcher.thread.is_alive():
            batcher = _ArrangementBatcher(device)
            _batchers[device] = batcher
        return batcher


def select_arrangement_device() -> str:
    """The device for an arrangement job: the GPU with the most free memory
    of those that take arrangements, or the CPU when the worker runs on it."""
    device = select_device()
    if device == "cpu":
        if not arrangement_offered("cpu"):
            raise ArrangementUnavailable()
        return device
    with _cuda_capacities_lock:
        candidates = [index for index in piano_transcription._cuda_capacities or {} if arrangement_offered(f"cuda:{index}")]
    if not candidates:
        raise ArrangementUnavailable()
    if len(candidates) == 1:
        return f"cuda:{candidates[0]}"
    return f"cuda:{max(candidates, key=lambda index: torch.cuda.mem_get_info(index)[0])}"


def preload() -> None:
    """Load the arrangement model on its device, so that the first
    arrangement does not wait for it. Nothing when arrangements are off or
    do not fit on this worker's GPUs."""
    if not settings.worker.arrangement:
        return
    try:
        device = select_arrangement_device()
    except ArrangementUnavailable:
        logger.warning("Arrangements are on, but no GPU of this worker has room for them")
        return
    get_batcher(device)


def model_audio(audio: np.ndarray, sample_rate: int) -> np.ndarray:
    """Audio (samples x channels) as the arrangement model reads it: mono at
    its sample rate, resampled as AMT-APC was trained. Its thresholds are
    sensitive to the resampler: with soxr instead, about 30% of the notes
    of the original's arrangements came out different."""
    signal = torch.from_numpy(np.ascontiguousarray(audio, dtype=np.float32))
    mono = signal.mean(dim=1) if signal.ndim == 2 else signal
    if sample_rate != amt_apc.SAMPLE_RATE:
        mono = torchaudio.transforms.Resample(sample_rate, amt_apc.SAMPLE_RATE)(mono)
    return mono.numpy()


def arrange(
    audio: np.ndarray,
    sample_rate: int,
    style: str,
    device: str,
    progress_callback: ProgressCallback | None = None,
    cancellation_callback: CancellationCallback | None = None,
) -> list:
    """The notes of a piano arrangement of audio (samples x channels), as
    Transkun's note events, so that they are written like a transcription."""
    feature = amt_apc.features(model_audio(audio, sample_rate))
    onset, offset, mpe, velocity = get_batcher(device).submit(
        feature, style, progress_callback, cancellation_callback,
    )
    return amt_apc.notes(onset, offset, mpe, velocity)


def arrange_piano(
    audio_path: Path,
    output_path: Path,
    style: str,
    progress_callback: ProgressCallback | None = None,
    cancellation_callback: CancellationCallback | None = None,
) -> None:
    """Arrange one audio file for piano, as MIDI. Progress as transcribe_piano's:

        0.07 - 0.15   model and audio loading
        0.15 - 0.95   the model's windows
        0.95 - 1.00   beat tracking and MIDI writing
    """
    if style not in amt_apc.STYLES:
        raise ValueError(f"Unknown arrangement style {style!r}")
    device = select_arrangement_device()
    if cancellation_callback and cancellation_callback():
        raise TranscriptionCancelled()
    if progress_callback:
        progress_callback(0.07)
    if device == "cpu":
        configure_cpu_threads()
    get_batcher(device)
    sample_rate, audio = read_audio(audio_path)
    if cancellation_callback and cancellation_callback():
        raise TranscriptionCancelled()
    if progress_callback:
        progress_callback(0.15)

    def report(window_progress: float) -> None:
        if progress_callback:
            progress_callback(max(0.15, min(0.15 + 0.80 * window_progress, 0.95)))

    events = arrange(audio, sample_rate, style, device, report, cancellation_callback)
    if progress_callback:
        progress_callback(0.96)
    # The same writer, tempo map included, as a transcription.
    write_midi(events, output_path, audio, sample_rate, device)
    if progress_callback:
        progress_callback(1.0)
