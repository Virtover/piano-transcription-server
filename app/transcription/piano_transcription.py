import gc
import logging
import math
import queue
import threading
import time
from concurrent.futures import Future
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import moduleconf
import numpy as np
import pkg_resources
import pydub
import soxr
import torch
import torch.nn.functional as F

from app.transcription.cancellation import TranscriptionCancelled
from transkun.Data import writeMidi
from transkun.ModelTransformer import (
    makeFrame,
    resolveOverlapping,
)

from app.transcription.batching import (
    fail,
    is_cancelled,
    report_progress,
    resolve,
    run_splitting_on_out_of_memory,
)
from app.config import settings
from app.transcription.device import (
    cpu_batch_size,
    cpu_thread_count,
    cuda_job_capacities,
    gpu_batch_size,
)


logger = logging.getLogger(__name__)


ProgressCallback = Callable[[float], None]
CancellationCallback = Callable[[], bool]


def configure_cpu_threads() -> None:
    torch.set_num_threads(cpu_thread_count())


@dataclass
class _BatchRequest:
    audio: np.ndarray
    progress_callback: ProgressCallback | None
    cancellation_callback: CancellationCallback | None
    result: Future
    # Highest progress reported, so a retried request never goes backwards.
    reported_progress: float = 0.0


def read_audio(
    path: Path,
) -> tuple[int, np.ndarray]:
    audio = pydub.AudioSegment.from_file(path)

    samples = np.array(
        audio.get_array_of_samples()
    )

    samples = samples.reshape(
        -1,
        audio.channels,
    )

    samples = (
        np.float32(samples)
        / 2**15
    )

    return audio.frame_rate, samples


def load_model(device: str):
    weight_path = pkg_resources.resource_filename(
        "transkun",
        "pretrained/2.0.pt",
    )

    conf_path = pkg_resources.resource_filename(
        "transkun",
        "pretrained/2.0.conf",
    )

    conf_manager = moduleconf.parseFromFile(
        conf_path
    )

    TransKun = conf_manager[
        "Model"
    ].module.TransKun

    conf = conf_manager[
        "Model"
    ].config

    checkpoint = torch.load(
        weight_path,
        map_location=device,
    )

    model = TransKun(
        conf=conf,
    ).to(device)

    if "best_state_dict" in checkpoint:
        model.load_state_dict(
            checkpoint["best_state_dict"],
            strict=False,
        )
    else:
        model.load_state_dict(
            checkpoint["state_dict"],
            strict=False,
        )

    model.eval()

    return model


# Usable GPU index -> job capacity, measured once before this process loads
# a model, like the launcher does.
_cuda_capacities: dict[int, int] | None = None


def select_device() -> str:
    global _cuda_capacities

    # Decide once which GPUs have capacity, before this process loads a model.
    # Checking capacity again later would count the loaded model and running
    # jobs against it and send new jobs to the CPU whenever the GPU is busy.
    # Concurrency is already limited by the worker thread count.
    if _cuda_capacities is None:
        _cuda_capacities = {
            device_index: capacity
            for device_index, capacity, _, _ in cuda_job_capacities() or []
            if capacity > 0
        }

    if not _cuda_capacities:
        return "cpu"
    if len(_cuda_capacities) == 1:
        return f"cuda:{next(iter(_cuda_capacities))}"

    # Jobs share a worker process, so choose a device for each job instead of
    # relying on CUDA's default device 0.
    device_index = max(
        _cuda_capacities,
        key=lambda index: torch.cuda.mem_get_info(index)[0],
    )
    return f"cuda:{device_index}"


def batch_size_for_device(device: str) -> int:
    """WORKER_BATCH_SIZE resolved for one device: on a GPU, `auto` is its
    startup job capacity, and a fixed value is lowered to that capacity."""
    if not device.startswith("cuda:"):
        return cpu_batch_size()
    capacity = (_cuda_capacities or {}).get(int(device.split(":", 1)[1]), 1)
    size, warning = gpu_batch_size(capacity)
    if warning:
        logger.warning("%s: %s", device, warning)
    return size


def is_cuda_out_of_memory(error: BaseException) -> bool:
    return isinstance(error, torch.cuda.OutOfMemoryError)


def transcribe_audio(
    model,
    audio: np.ndarray,
    progress_callback: ProgressCallback | None = None,
    cancellation_callback: CancellationCallback | None = None,
):
    """
    Transcribe audio using the same segment processing logic
    as Transkun's TransKun.transcribe().

    Progress is reported after each actual segment has finished.
    """

    step_in_second = (
        model.segmentHopSizeInSecond
    )

    segment_size_in_second = (
        model.segmentSizeInSecond
    )

    device = next(
        model.parameters()
    ).device

    # Transkun expects:
    #
    #     channels x samples
    #
    x = torch.from_numpy(audio).to(device)
    x = x.transpose(-1, -2)

    pad_time_begin = (
        segment_size_in_second
        - step_in_second
    )

    x = F.pad(
        x,
        (
            math.ceil(
                pad_time_begin * model.fs
            ),
            math.ceil(
                pad_time_begin * model.fs
            ),
        ),
    )

    n_sample = x.shape[-1]

    step_size = (
        math.ceil(
            step_in_second
            * model.fs
            / model.hopSize
        )
        * model.hopSize
    )

    segment_size = math.ceil(
        segment_size_in_second
        * model.fs
    )

    segment_starts = range(
        0,
        n_sample,
        step_size,
    )

    segment_count = math.ceil(
        n_sample / step_size
    )

    events_by_type = defaultdict(list)

    start_frame_idx = math.floor(
        pad_time_begin
        * model.fs
        / model.hopSize
    )

    start_pos = [
        start_frame_idx
    ] * len(model.targetMIDIPitch)

    torch.set_grad_enabled(False)

    for segment_index, i in enumerate(
        segment_starts,
        start=1,
    ):
        if cancellation_callback and cancellation_callback():
            raise TranscriptionCancelled()

        # Report progress BEFORE starting the expensive
        # inference for this segment.
        #
        # This is important: otherwise the UI appears frozen
        # during the first/only segment.
        if progress_callback:
            progress_callback(
                (segment_index - 1)
                / segment_count
            )

        j = min(
            i + segment_size,
            n_sample,
        )

        begin_time = (
            i / model.fs
            - pad_time_begin
        )

        cur_slice = x[:, i:j]

        if (
            cur_slice.shape[-1]
            < segment_size
        ):
            cur_slice = F.pad(
                cur_slice,
                (
                    0,
                    segment_size
                    - cur_slice.shape[-1],
                ),
            )

        cur_frames = makeFrame(
            cur_slice,
            model.hopSize,
            model.windowSize,
        )

        last_frame_idx = round(
            segment_size
            / model.hopSize
        )

        cur_events, last_p = (
            model.transcribeFrames(
                cur_frames.unsqueeze(0),
                forcedStartPos=start_pos,
                velocityCriteron="hamming",
                onsetBound=None,
                lastFrameIdx=last_frame_idx,
            )
        )

        cur_events = cur_events[0]

        start_pos = [
            max(
                k
                - int(
                    step_size
                    / model.hopSize
                ),
                0,
            )
            for k in last_p
        ]

        for event in cur_events:
            event.start += begin_time
            event.end += begin_time

            event.start = max(
                event.start,
                0,
            )

            event.end = max(
                event.end,
                event.start,
            )

        # This is the same merging logic used by
        # Transkun's original transcribe().
        for event in cur_events:
            pitch = event.pitch

            if events_by_type[pitch]:
                last_event = (
                    events_by_type[pitch][-1]
                )

                if event.start < last_event.end:
                    if event.hasOnset:
                        events_by_type[pitch][-1] = (
                            event
                        )
                    else:
                        last_event.hasOffset = (
                            event.hasOffset
                        )

                        last_event.end = max(
                            event.end,
                            last_event.end,
                        )

                    continue

            if event.hasOnset:
                events_by_type[pitch].append(
                    event
                )

        # Segment is now completely processed.
        if progress_callback:
            progress_callback(
                segment_index
                / segment_count
            )

    # Same final-event handling as Transkun.
    for event_type in events_by_type:
        if events_by_type[event_type]:
            events_by_type[event_type][
                -1
            ].hasOffset = True

    events_all = sum(
        events_by_type.values(),
        [],
    )

    events_all = [
        event
        for event in events_all
        if event.hasOffset
    ]

    events_all = resolveOverlapping(
        events_all
    )

    return events_all


def _transcribe_audio_batch(
    model,
    requests: list[_BatchRequest],
) -> None:
    device = next(model.parameters()).device
    step_in_second = model.segmentHopSizeInSecond
    segment_size_in_second = model.segmentSizeInSecond
    pad_time_begin = segment_size_in_second - step_in_second
    segment_size = math.ceil(segment_size_in_second * model.fs)
    step_size = math.ceil(
        step_in_second * model.fs / model.hopSize
    ) * model.hopSize
    start_frame_idx = math.floor(
        pad_time_begin * model.fs / model.hopSize
    )

    states = []
    for request in requests:
        try:
            # The full audio stays in CPU memory; only the current segment is
            # moved to the device, so VRAM per job does not grow with length.
            x = torch.from_numpy(request.audio)
            x = x.transpose(-1, -2)
            x = F.pad(
                x,
                (
                    math.ceil(pad_time_begin * model.fs),
                    math.ceil(pad_time_begin * model.fs),
                ),
            )
            n_sample = x.shape[-1]
            states.append(
                {
                    "request": request,
                    "x": x,
                    "n_sample": n_sample,
                    "segment_count": math.ceil(n_sample / step_size),
                    "segment_index": 0,
                    "events": defaultdict(list),
                    "start_pos": [
                        start_frame_idx
                    ] * len(model.targetMIDIPitch),
                }
            )
        except Exception as error:
            fail(request, error)

    while states:
        active = []
        frames = []
        forced_start_positions = []

        for state in states:
            request = state["request"]
            if is_cancelled(request):
                fail(request, TranscriptionCancelled())
                continue

            segment_index = state["segment_index"] + 1
            if not report_progress(
                request,
                (segment_index - 1) / state["segment_count"],
            ):
                continue

            i = state["segment_index"] * step_size
            j = min(i + segment_size, state["n_sample"])
            cur_slice = state["x"][:, i:j].to(device)
            if cur_slice.shape[-1] < segment_size:
                cur_slice = F.pad(
                    cur_slice,
                    (0, segment_size - cur_slice.shape[-1]),
                )

            frames.append(
                makeFrame(
                    cur_slice,
                    model.hopSize,
                    model.windowSize,
                )
            )
            forced_start_positions.extend(state["start_pos"])
            active.append(state)

        if not active:
            states = []
            continue

        try:
            with torch.no_grad():
                batch_events, last_positions = model.transcribeFrames(
                    torch.stack(frames),
                    forcedStartPos=forced_start_positions,
                    velocityCriteron="hamming",
                    onsetBound=None,
                    lastFrameIdx=round(segment_size / model.hopSize),
                )
        except Exception as error:
            # Out of memory is retried by the batcher with smaller batches,
            # so it must not resolve any request here.
            if is_cuda_out_of_memory(error):
                raise
            for state in active:
                fail(state["request"], error)
            return

        next_states = []
        last_position_index = 0
        for state, cur_events in zip(active, batch_events):
            request = state["request"]
            segment_index = state["segment_index"] + 1
            begin_time = (
                state["segment_index"] * step_size / model.fs
                - pad_time_begin
            )
            for event in cur_events:
                event.start = max(event.start + begin_time, 0)
                event.end = max(event.end + begin_time, event.start)
                pitch = event.pitch
                events = state["events"][pitch]
                if events and event.start < events[-1].end:
                    if event.hasOnset:
                        events[-1] = event
                    else:
                        events[-1].hasOffset = event.hasOffset
                        events[-1].end = max(event.end, events[-1].end)
                elif event.hasOnset:
                    events.append(event)

            symbol_count = len(model.targetMIDIPitch)
            state["start_pos"] = [
                max(
                    position - int(step_size / model.hopSize),
                    0,
                )
                for position in last_positions[
                    last_position_index:last_position_index + symbol_count
                ]
            ]
            last_position_index += symbol_count
            state["segment_index"] = segment_index
            if not report_progress(
                request,
                segment_index / state["segment_count"],
            ):
                continue

            if segment_index < state["segment_count"]:
                next_states.append(state)
            else:
                for events in state["events"].values():
                    if events:
                        events[-1].hasOffset = True
                events_all = [
                    event
                    for events in state["events"].values()
                    for event in events
                    if event.hasOffset
                ]
                resolve(request, resolveOverlapping(events_all))

        states = next_states


class _GpuBatcher:
    def __init__(self, device: str):
        self.device = device
        self.model = load_model(device)
        self.requests: queue.Queue[_BatchRequest] = queue.Queue()
        self.batch_size = batch_size_for_device(device)
        self.timeout = settings.worker.batch_timeout
        logger.info("%s: batch size %d", device, self.batch_size)
        self.thread = threading.Thread(
            target=self._run,
            name=f"transcription-batcher-{device}",
            daemon=True,
        )
        self.thread.start()

    def submit(
        self,
        audio: np.ndarray,
        progress_callback: ProgressCallback | None,
        cancellation_callback: CancellationCallback | None,
    ) -> list:
        result = Future()
        self.requests.put(
            _BatchRequest(
                audio=audio,
                progress_callback=progress_callback,
                cancellation_callback=cancellation_callback,
                result=result,
            )
        )
        return result.result()

    def _run(self) -> None:
        while True:
            batch = [self.requests.get()]
            end_time = time.monotonic() + self.timeout
            while len(batch) < self.batch_size:
                remaining = end_time - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    batch.append(self.requests.get(timeout=remaining))
                except queue.Empty:
                    break
            try:
                run_splitting_on_out_of_memory(
                    batch,
                    lambda requests: _transcribe_audio_batch(self.model, requests),
                    is_cuda_out_of_memory,
                    self._free_memory,
                    self._lower_batch_size,
                )
            except Exception as error:
                for request in batch:
                    fail(request, error)
            finally:
                batch.clear()
                self._free_memory()

    def _free_memory(self) -> None:
        gc.collect()
        if self.device.startswith("cuda:"):
            torch.cuda.empty_cache()

    def _lower_batch_size(self, size: int) -> None:
        if size < self.batch_size:
            logger.warning(
                "%s ran out of memory; batch size lowered from %d to %d",
                self.device,
                self.batch_size,
                size,
            )
            self.batch_size = size


_gpu_batchers: dict[str, _GpuBatcher] = {}
_gpu_batchers_lock = threading.Lock()


def _get_gpu_batcher(device: str) -> _GpuBatcher:
    with _gpu_batchers_lock:
        batcher = _gpu_batchers.get(device)
        if batcher is None:
            batcher = _GpuBatcher(device)
            _gpu_batchers[device] = batcher
        return batcher


def transcribe_piano(
    audio_path: Path,
    output_path: Path,
    progress_callback: ProgressCallback | None = None,
    cancellation_callback: CancellationCallback | None = None,
):
    """
    Transcribe one WAV file to MIDI.

    Progress range:

        0.07 - 0.10   model loading
        0.10 - 0.15   audio loading
        0.15 - 0.95   Transkun segment processing
        0.95 - 1.00   MIDI writing
    """

    device = select_device()

    if cancellation_callback and cancellation_callback():
        raise TranscriptionCancelled()

    if progress_callback:
        progress_callback(0.07)

    if device == "cpu":
        configure_cpu_threads()

    batcher = _get_gpu_batcher(device)
    model = batcher.model

    if cancellation_callback and cancellation_callback():
        raise TranscriptionCancelled()

    if progress_callback:
        progress_callback(0.10)

    sample_rate, audio = read_audio(
        audio_path
    )

    if cancellation_callback and cancellation_callback():
        raise TranscriptionCancelled()

    if sample_rate != model.fs:
        audio = soxr.resample(
            audio,
            sample_rate,
            model.fs,
        )

    if progress_callback:
        progress_callback(0.15)

    def report_progress(
        segment_progress: float,
    ):
        progress = (
            0.15
            + 0.80 * segment_progress
        )

        if progress_callback:
            progress_callback(
                max(
                    0.15,
                    min(progress, 0.95),
                )
            )

    events = batcher.submit(
        audio=audio,
        progress_callback=report_progress,
        cancellation_callback=cancellation_callback,
    )

    if progress_callback:
        progress_callback(0.96)

    output_midi = writeMidi(events)

    output_midi.write(
        str(output_path)
    )

    if progress_callback:
        progress_callback(1.0)