"""The grouper's instrument vocabulary: 73 instruments, each with its General MIDI programs and one or more families.

General MIDI programs that are the same real instrument share one name (Acoustic Grand / Bright / Electric Grand / Honky-tonk
Piano are all "piano"; the 8 Synth Lead programs are one "synth_lead"). A track is written with the first program of its
instrument. Families are used when the decode picks a track's name: it maximises the expected credit (1 for the right
instrument, 0.5 for another instrument of a shared family), not the raw probability.
"""
from __future__ import annotations

import numpy as np

_TABLE = [
    # name               GM programs (0-based)              families
    ("piano",            [0, 1, 2, 3],                       ["keys"]),
    ("electric_piano",   [4, 5],                             ["keys"]),
    ("harpsichord",      [6],                                ["keys"]),
    ("clavinet",         [7],                                ["keys"]),
    ("harp",             [46],                               ["keys", "plucked"]),
    ("celesta",          [8],                                ["mallets", "keys"]),
    ("glockenspiel",     [9],                                ["mallets"]),
    ("music_box",        [10],                               ["mallets"]),
    ("vibraphone",       [11],                               ["mallets"]),
    ("marimba",          [12],                               ["mallets"]),
    ("xylophone",        [13],                               ["mallets"]),
    ("tubular_bells",    [14],                               ["mallets"]),
    ("dulcimer",         [15],                               ["plucked", "mallets"]),
    ("kalimba",          [108],                              ["mallets"]),
    ("tinkle_bell",      [112],                              ["mallets"]),
    ("steel_drums",      [114],                              ["mallets"]),
    ("organ",            [16, 17, 18],                       ["organ"]),
    ("church_organ",     [19],                               ["organ"]),
    ("reed_organ",       [20],                               ["organ"]),
    ("accordion",        [21, 23],                           ["organ"]),
    ("harmonica",        [22],                               ["organ", "reeds"]),
    ("acoustic_guitar",  [24, 25],                           ["plucked"]),
    ("electric_guitar",  [26, 27, 28, 29, 30, 31],           ["plucked"]),
    ("sitar",            [104],                              ["plucked"]),
    ("banjo",            [105],                              ["plucked"]),
    ("shamisen",         [106],                              ["plucked"]),
    ("koto",             [107],                              ["plucked"]),
    ("double_bass",      [32, 43],                           ["bass", "strings"]),
    ("electric_bass",    [33, 34, 35, 36, 37],               ["bass"]),
    ("synth_bass",       [38, 39],                           ["bass", "synth"]),
    ("violin",           [40, 110],                          ["strings"]),
    ("viola",            [41],                               ["strings"]),
    ("cello",            [42],                               ["strings"]),
    ("string_ensemble",  [44, 45, 48, 49],                   ["strings"]),
    ("synth_strings",    [50, 51],                           ["strings", "synth"]),
    ("voice",            [52, 53],                           ["voice"]),
    ("synth_voice",      [54],                               ["voice", "synth"]),
    ("trumpet",          [56, 59],                           ["brass"]),
    ("trombone",         [57],                               ["brass"]),
    ("tuba",             [58],                               ["brass"]),
    ("french_horn",      [60],                               ["brass"]),
    ("brass_section",    [61],                               ["brass"]),
    ("synth_brass",      [62, 63],                           ["brass", "synth"]),
    ("soprano_sax",      [64],                               ["reeds"]),
    ("alto_sax",         [65],                               ["reeds"]),
    ("tenor_sax",        [66],                               ["reeds"]),
    ("baritone_sax",     [67],                               ["reeds"]),
    ("oboe",             [68],                               ["reeds"]),
    ("english_horn",     [69],                               ["reeds"]),
    ("bassoon",          [70],                               ["reeds"]),
    ("clarinet",         [71],                               ["reeds"]),
    ("bagpipe",          [109],                              ["reeds"]),
    ("shanai",           [111],                              ["reeds"]),
    ("piccolo",          [72],                               ["flutes"]),
    ("flute",            [73],                               ["flutes"]),
    ("recorder",         [74],                               ["flutes"]),
    ("pan_flute",        [75],                               ["flutes"]),
    ("blown_bottle",     [76],                               ["flutes"]),
    ("shakuhachi",       [77],                               ["flutes"]),
    ("whistle",          [78],                               ["flutes"]),
    ("ocarina",          [79],                               ["flutes"]),
    ("orchestra_hit",    [55],                               ["synth"]),
    ("synth_lead",       list(range(80, 88)),                ["synth"]),
    ("synth_pad",        list(range(88, 96)),                ["synth"]),
    ("synth_fx",         list(range(96, 104)),               ["synth"]),
    ("timpani",          [47],                               ["percussion"]),
    ("agogo",            [113],                              ["percussion"]),
    ("woodblock",        [115],                              ["percussion"]),
    ("taiko",            [116],                              ["percussion"]),
    ("melodic_tom",      [117],                              ["percussion"]),
    ("synth_drum",       [118],                              ["percussion", "synth"]),
    ("reverse_cymbal",   [119],                              ["percussion"]),
    ("sound_effect",     list(range(120, 128)),              ["sfx"]),
]

INSTRUMENTS = [name for name, _, _ in _TABLE]
PROGRAMS = {name: tuple(progs) for name, progs, _ in _TABLE}
FAMILIES = {name: tuple(fam) for name, _, fam in _TABLE}
K = len(INSTRUMENTS)
PROGRAM = np.array([progs[0] for _, progs, _ in _TABLE], np.int64)      # the GM program a track of each instrument is written with


def _credit(a, b):
    if a == b:
        return 1.0
    return 0.5 if set(FAMILIES[a]) & set(FAMILIES[b]) else 0.0


CREDIT = np.array([[_credit(a, b) for b in INSTRUMENTS] for a in INSTRUMENTS], np.float64)    # (predicted, true)
