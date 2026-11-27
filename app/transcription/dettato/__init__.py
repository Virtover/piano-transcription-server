"""Dettato: multi-instrument audio-to-MIDI transcription by KobiMusic, for ensemble jobs (app/transcription/ensemble).

Vendored from https://github.com/kobimusic/dettato at commit 51ade0f09f2272f4674166026582185d26baaeaf (version 2.0.0),
Apache-2.0 (LICENSE, NOTICE here). Changed from the original:

- hub.py: weights only from the images' checked copies, never downloaded at run time;
- precision.py (new), used by the three networks' autocast and the lane GRU: bfloat16 only on GPUs where it is native,
  float32 elsewhere (the original uses bfloat16 on every CUDA GPU, which older GPUs cannot run).
- grouper/decode.py _agglo: the same merges with each row's smallest distance kept between merges, instead of a search
  of the whole matrix after each (cubic in the number of groups: hours for a ten-hour recording); above LARGE_GROUPS,
  distances computed as needed instead of a matrix of 8 M^2 bytes (equal up to the last bits, so on such recordings a
  few tracks may differ, as they do under any rounding change).

The server does not run Dettato.transcribe: app/transcription/ensemble runs the same models in bounded memory. The package's
own pipeline stays as the reference that tests compare with.

    >>> import dettato
    >>> result = dettato.transcribe("song.mp3")
    >>> result.save_midi("song.mid")
"""
from .pipeline import Dettato, Track, Transcription, transcribe

__version__ = "2.0.0"
__all__ = ["Dettato", "Track", "Transcription", "transcribe", "__version__"]
