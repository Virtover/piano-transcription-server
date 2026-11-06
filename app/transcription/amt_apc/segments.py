"""Cutting a feature into the windows the network reads, and running them.

The original (AMT.transcript) pads the feature with MIN_VALUE, MARGIN_BEFORE
frames at the start and, after the end, enough to fill the last window of
NUM_FRAME frames plus MARGIN_AFTER. Each window is NUM_FRAME frames with the
margins as context, and the windows do not overlap.
"""

import numpy as np
import torch

from app.transcription.amt_apc import constants
from app.transcription.amt_apc.model import Spec2MIDI

WINDOW = constants.MARGIN_BEFORE + constants.NUM_FRAME + constants.MARGIN_AFTER


def pad_features(feature: np.ndarray) -> torch.Tensor:
    """The feature [frames, n_bins] with its margins and the last window filled."""
    feature = np.asarray(feature, dtype=np.float32)
    fill = -feature.shape[0] % constants.NUM_FRAME
    before = np.full((constants.MARGIN_BEFORE, constants.N_BINS), constants.MIN_VALUE, dtype=np.float32)
    after = np.full((fill + constants.MARGIN_AFTER, constants.N_BINS), constants.MIN_VALUE, dtype=np.float32)
    return torch.from_numpy(np.concatenate([before, feature, after], axis=0))


def segment_starts(frame_count: int) -> list[int]:
    """The first frame of each window, in frames of the unpadded feature."""
    return list(range(0, frame_count, constants.NUM_FRAME))


def segment_input(padded: torch.Tensor, start: int) -> torch.Tensor:
    """The model input [1, n_bins, WINDOW] of the window that outputs frames start to start + NUM_FRAME."""
    return padded[start : start + WINDOW].T.unsqueeze(0)


def run_segment(
    model: Spec2MIDI, window_input: torch.Tensor, style: np.ndarray, device: str
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """The "B" (time head) output of one window, as NumPy arrays of NUM_FRAME frames.

    In order: onset, offset, frame (float32 [NUM_FRAME, 88]) and velocity
    (int8, the most likely class). These are the four arrays the original's
    Pipeline.wav2midi turns into notes; its frequency head "A" is not used.
    """
    with torch.inference_mode():
        sv = torch.from_numpy(np.asarray(style, dtype=np.float32)).reshape(1, -1).to(device)
        *_, onset, offset, mpe, velocity = model(window_input.to(device), sv)
        return (
            onset[0].cpu().numpy(),
            offset[0].cpu().numpy(),
            mpe[0].cpu().numpy(),
            velocity[0].argmax(2).to(torch.int8).cpu().numpy(),
        )
