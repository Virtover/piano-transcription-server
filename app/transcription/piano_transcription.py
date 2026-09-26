import math
from collections import defaultdict
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


ProgressCallback = Callable[[float], None]
CancellationCallback = Callable[[], bool]


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


def select_device() -> str:
    if not torch.cuda.is_available():
        return "cpu"

    # Jobs share a worker process, so choose a device for each job instead of
    # relying on CUDA's default device 0.
    candidates = []
    for device_index in range(torch.cuda.device_count()):
        try:
            free_memory, _ = torch.cuda.mem_get_info(device_index)
        except RuntimeError:
            continue

        candidates.append((free_memory, device_index))

    if not candidates:
        return "cuda:0"

    _, device_index = max(candidates)
    return f"cuda:{device_index}"


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


def transcribe_piano(
    audio_path: Path,
    output_path: Path,
    progress_callback: ProgressCallback | None = None,
    cancellation_callback: CancellationCallback | None = None,
):
    """
    Transcribe one WAV file to MIDI.

    Progress range:

        0.00 - 0.10   model loading
        0.10 - 0.15   audio loading
        0.15 - 0.95   Transkun segment processing
        0.95 - 1.00   MIDI writing
    """

    device = select_device()

    if cancellation_callback and cancellation_callback():
        raise TranscriptionCancelled()

    if progress_callback:
        progress_callback(0.02)

    model = load_model(device)

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

    events = transcribe_audio(
        model=model,
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