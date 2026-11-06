"""AMT-APC: piano cover generation from audio, vendored for inference only.

AMT-APC turns the audio of any music into a piano arrangement as notes. It is
a hFT-Transformer fine-tuned for covers, steered by a style vector. Source:
https://github.com/310hz/amt-apc, paper arXiv 2409.14086, MIT license (see
LICENSE here, which also carries the license of Sony's hFT-Transformer code).
Only the network, its features, and the step from output to notes are here;
the weights (apc.pth) are a separate file, loaded with `load_model`.
"""

from app.transcription.amt_apc.constants import SAMPLE_RATE
from app.transcription.amt_apc.features import features
from app.transcription.amt_apc.model import Spec2MIDI, load_model
from app.transcription.amt_apc.notes import notes
from app.transcription.amt_apc.segments import (
    pad_features,
    run_segment,
    segment_input,
    segment_starts,
)
from app.transcription.amt_apc.style import (
    DEFAULT_STYLE,
    STYLE_VECTORS,
    STYLES,
    style_vector,
)

__all__ = [
    "SAMPLE_RATE",
    "DEFAULT_STYLE",
    "STYLES",
    "STYLE_VECTORS",
    "Spec2MIDI",
    "features",
    "load_model",
    "notes",
    "pad_features",
    "run_segment",
    "segment_input",
    "segment_starts",
    "style_vector",
]
