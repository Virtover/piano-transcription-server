"""Stage 3: the grouper (kobimusic/dettato-grouper): notes -> instrument-named tracks."""
from .core import Grouper
from .instruments import FAMILIES, INSTRUMENTS, PROGRAM, PROGRAMS

__all__ = ["Grouper", "INSTRUMENTS", "PROGRAM", "PROGRAMS", "FAMILIES"]
