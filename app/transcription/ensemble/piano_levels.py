"""Piano versions of an ensemble result: a multi-track MIDI file played by two hands, at five levels.

    python -m app.transcription.ensemble.piano_levels ensemble.mid piano.mid [--level intermediate] [--chord-notes 3]
    python -m app.transcription.ensemble.piano_levels ensemble.mid --all-levels out/  # out/<source> - piano <level>.mid

Levels, from a tune and its bass to every note:
    beginner      the melody in the right hand, one note at a time, at most one attack a beat; single bass notes
                  on strong beats and chord changes in the left; narrow ranges, no wide leaps
    easy          melody, bass, and up to two chord notes; attacks at most every half beat
    intermediate  melody, bass, and three-note chords between the hands; sixteenth-note rhythm
    advanced      fuller chords (five notes), octave bass and open voicings in the left hand; thirty-second rhythm
    full          every pitched note of every part, arranged for the piano's sound rather than for two hands:
                  folded into the keyboard, unisons merged, voices balanced, low mud cleared, and pedalled

Melody (the most lead-like part per 4 beats), bass, and chords from the other parts, reduced to two hands within the
level's limits. Drums and effects are dropped; timing (every note starts on a source note's tick) and the tempo map
are kept exactly, and the piano version lasts exactly as long as the source.

The standalone script multitrack_to_piano.py, brought into the server: for the same input, every level's file is
byte-identical to the script's (tests/test_piano_levels.py keeps it so). What differs is speed, not output: the file is
read once and its analysis (parts, roles, the melody's path) shared by the levels (all_levels); MIDI bytes are read and
written directly rather than through mido's message objects; and no step is quadratic in the number of notes. Only
mido and the standard library: no torch, no GPU, and nothing of the server's settings.
"""
from __future__ import annotations

import argparse
import bisect
import io
import statistics
import struct
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import NamedTuple

import mido
from mido.messages import SPEC_BY_STATUS
from mido.midifiles.meta import build_meta_message, meta_charset

# Notes that start within this of each other are one attack.
CLUSTER_SECONDS = 0.035
# The melody's part is chosen per window of this many beats.
WINDOW_BEATS = 4
# Choosing another part for the melody costs this much of a window's score.
SWITCH_COST = 0.6
# A chord keeps pitch classes with at least this share of the strongest one's weight.
CHORD_SHARE = 0.2
LOWEST, HIGHEST = 21, 108                # the piano's keys, A0 to C8
FALLBACK_BASS_RANGE = (36, 50)           # where a bass taken from the harmony is placed
DEFAULT_SPLIT = 60
# Below this, a chord note closer than a fifth to the bass muddies the sound.
MUDDY_BELOW = 48
MUDDY_INTERVAL = 7
# Notes shorter than this are transcription specks, never a melody's.
MIN_MELODY_SECONDS = 0.05
# Without a bass part, the lowest notes under this (an octave or more below the melody) are the bass line.
DERIVED_BASS_BELOW = 53
# The full level strikes at most this many keys at one attack.
FULL_MAX_KEYS = 12
# The full level splits its hands at the widest gap between these pitches, or at the default split.
FULL_SPLIT_RANGE = (52, 68)
# A harmony shorter than this many beats is not pedalled; one with more melody attacks a beat than this neither.
PEDAL_MIN_BEATS = 1
PEDAL_MAX_MELODY_RATE = 4


class Note(NamedTuple):
    """A note; a tuple, because the levels make and change hundreds of thousands of them."""
    start: int          # ticks
    end: int            # ticks
    pitch: int
    velocity: int
    onset: float        # seconds
    offset: float       # seconds
    key: int = 0        # unique per source note, so equal notes of two parts stay two notes
    role: str = ""      # in the piano version: "melody", "bass", "octave" (the bass's lower octave), or "chord"


@dataclass
class Part:
    name: str
    program: int        # General MIDI, 0-127
    is_drum: bool
    notes: list[Note] = field(default_factory=list)


@dataclass(frozen=True)
class Level:
    """What a difficulty level allows. Gaps and legato are in beats; ranges and spans in semitones."""
    name: str
    chord_notes: int                    # the most accompaniment notes at one attack, both hands together
    right_hand_notes: int               # the most notes the right hand holds at once, the melody included
    left_hand_notes: int                # the same for the left hand, the bass included
    melody_gap: float                   # the least time between two melody attacks
    bass_gap: float                     # between two bass attacks
    chord_gap: float                    # between two attacks of one hand's accompaniment
    melody_range: tuple[int, int]       # every melody note is moved within, by octaves
    melody_center: tuple[int, int]      # where a melody passage's median is moved, by octaves
    bass_range: tuple[int, int]
    hand_span: int                      # how far one hand reaches at once
    max_leap: int | None = None         # a wider melody leap is folded by octaves
    legato: float = 0.0                 # a shorter rest before a line's next note is filled
    left_extra: int = 0                 # chord notes the left hand may add above the bass (a fifth, a tenth)
    octave_bass: bool = False           # long bass notes on strong beats doubled an octave below
    doubling: bool = False              # chord tones doubled in another octave while the right hand has room
    all_notes: bool = False             # every note kept, for the sound: the full level
    pedal: bool = False                 # sustain pedal at harmony changes


LEVELS: dict[str, Level] = {level.name: level for level in (
    Level("beginner", chord_notes=0, right_hand_notes=1, left_hand_notes=1,
          melody_gap=1, bass_gap=2, chord_gap=1, melody_range=(60, 79), melody_center=(62, 74),
          bass_range=(40, 57), hand_span=12, max_leap=9, legato=1),
    Level("easy", chord_notes=2, right_hand_notes=2, left_hand_notes=2,
          melody_gap=0.5, bass_gap=1, chord_gap=0.5, melody_range=(57, 84), melody_center=(60, 79),
          bass_range=(36, 55), hand_span=12, max_leap=12, legato=0.5),
    Level("intermediate", chord_notes=3, right_hand_notes=4, left_hand_notes=3,
          melody_gap=0.25, bass_gap=0.25, chord_gap=0.25, melody_range=(55, 91), melody_center=(60, 84),
          bass_range=(28, 54), hand_span=14, left_extra=1),
    Level("advanced", chord_notes=5, right_hand_notes=5, left_hand_notes=4,
          melody_gap=0.125, bass_gap=0.125, chord_gap=0.125, melody_range=(52, 96), melody_center=(60, 86),
          bass_range=(24, 52), hand_span=16, left_extra=2, octave_bass=True, doubling=True),
    Level("full", chord_notes=FULL_MAX_KEYS, right_hand_notes=FULL_MAX_KEYS, left_hand_notes=FULL_MAX_KEYS,
          melody_gap=0, bass_gap=0, chord_gap=0, melody_range=(LOWEST, HIGHEST), melody_center=(60, 84),
          bass_range=(LOWEST, HIGHEST), hand_span=HIGHEST - LOWEST, all_notes=True, pedal=True),
)}


@dataclass(frozen=True)
class Options:
    level: str = "intermediate"
    # each overrides the level's own when given
    chord_notes: int | None = None
    right_hand_notes: int | None = None
    left_hand_notes: int | None = None
    pedal: bool | None = None

    def resolved(self) -> Level:
        if self.level not in LEVELS:
            raise ValueError(f"no level {self.level!r}; the levels are {', '.join(LEVELS)}")
        changes = {name: value for name in ("chord_notes", "right_hand_notes", "left_hand_notes", "pedal")
                   if (value := getattr(self, name)) is not None}
        level = replace(LEVELS[self.level], **changes)
        if level.chord_notes < 0 or level.right_hand_notes < 1 or level.left_hand_notes < 1:
            raise ValueError("chord notes must be 0 or more, and each hand must hold at least one note")
        return level


@dataclass
class Hands:
    right: list[Note]
    left: list[Note]
    pedal: list[tuple[int, int]] = field(default_factory=list)     # (down, up) ticks, on both hands' channels


class Meter:
    """Bars and beats at ticks, by the file's time signatures (4/4 until the first)."""

    def __init__(self, ticks_per_beat: int, signatures: list[tuple[int, int, int]] = ()):
        self.ticks_per_beat = ticks_per_beat
        self.starts = [0]
        self.signatures = [(4, 4)]
        for tick, numerator, denominator in sorted(signatures):
            if tick == self.starts[-1]:
                self.signatures[-1] = (numerator, denominator)
                continue
            self.starts.append(tick)
            self.signatures.append((numerator, denominator))
        # transcribed notes fall a little off the grid
        self.tolerance = max(1, ticks_per_beat // 12)
        # strength by tick: the levels ask for the same ticks again and again; at most one entry per source tick
        self._strengths: dict[int, int] = {}

    def strength(self, tick: int) -> int:
        """4 on a bar's downbeat, 3 on its middle (or a compound meter's beat), 2 on other beats, 1 halfway, else 0."""
        known = self._strengths.get(tick)
        if known is None:
            known = self._strengths[tick] = self._strength(tick)
        return known

    def _strength(self, tick: int) -> int:
        i = bisect.bisect_right(self.starts, tick) - 1
        numerator, denominator = self.signatures[i]
        beat = self.ticks_per_beat * 4 / max(denominator, 1)
        bar = beat * max(numerator, 1)
        position = (tick - self.starts[i]) % bar

        def on(unit: float) -> bool:
            rest = position % unit
            return min(rest, unit - rest) <= self.tolerance

        if on(bar):
            return 4
        if (numerator % 3 == 0 and numerator > 3 and on(3 * beat)) or (numerator % 2 == 0 and numerator >= 4
                                                                       and on(bar / 2)):
            return 3
        if on(beat):
            return 2
        return 1 if on(beat / 2) else 0


# --- roles ------------------------------------------------------------------------------------------------------


def is_left_out(part: Part) -> bool:
    """Drums, unpitched percussion, and sound effects: nothing a piano can play."""
    p = part.program
    return part.is_drum or p >= 120 or p in (47, 113, 115, 116, 117, 118, 119) or not part.notes


def is_bass_program(program: int) -> bool:
    return 32 <= program <= 39 or program in (43, 58)


def melody_prior(program: int) -> float:
    """How likely an instrument is to carry the tune, from its General MIDI program."""
    if 52 <= program <= 54:
        return 1.0                                  # voices
    if 80 <= program <= 87:
        return 0.9                                  # synth leads
    if 56 <= program <= 79 or program in (104, 105, 106, 107, 109, 110, 111):
        return 0.8                                  # brass, reeds, flutes, solo plucked and blown
    if program in (40, 41):
        return 0.7                                  # violin, viola
    if 8 <= program <= 15 or program in (108, 112, 114):
        return 0.5                                  # mallets
    if 24 <= program <= 31 or program == 42:
        return 0.4                                  # guitars, cello
    if 0 <= program <= 7 or program == 46:
        return 0.35                                 # keys, harp
    if 16 <= program <= 23:
        return 0.3                                  # organs
    if 44 <= program <= 51 or program == 55:
        return 0.25                                 # string ensembles
    if 88 <= program <= 95:
        return 0.1                                  # pads
    if 96 <= program <= 103:
        return 0.05                                 # synth effects
    return 0.0


def harmony_weight(program: int) -> float:
    """How much an instrument's notes count when choosing chords: pads and effects colour more than they define."""
    if 88 <= program <= 103:
        return 0.6
    return 1.0


def full_gain(program: int, bass: bool) -> float:
    """The full level's balance: how loud a part's notes sound against the others (the melody's are raised apart).
    Sustained washes are softened so the piano's decay does not pile them up; the bass kept clear, not boomy."""
    if bass:
        return 0.9
    if 88 <= program <= 103 or 48 <= program <= 51 or program == 55:
        return 0.7                                  # pads, synth effects, string ensembles
    if 16 <= program <= 23 or 52 <= program <= 54:
        return 0.8                                  # organs; voices off the tune
    return 0.85


# Which notes give way first when a hand is too full: chords, then the bass's octave; the tune and bass hold.
PRIORITY = {"melody": 3, "bass": 3, "octave": 2, "chord": 1}
PROTECTED = 2


def _priority(note: Note) -> int:
    return PRIORITY.get(note.role, 0)


# --- lines ------------------------------------------------------------------------------------------------------


def _clusters(notes: list[Note]) -> list[list[Note]]:
    """Notes in onset order, grouped into attacks."""
    groups: list[list[Note]] = []
    first = 0.0
    for note in sorted(notes, key=lambda n: (n.onset, n.pitch, n.key)):
        if groups and note.onset - first <= CLUSTER_SECONDS:
            groups[-1].append(note)
        else:
            groups.append([note])
            first = note.onset
    return groups


def _monophonic(line: list[Note]) -> list[Note]:
    """A line in onset order in which each note ends where the next starts, if it lasts longer."""
    out: list[Note] = []
    for note in line:
        if out and out[-1].end > note.start:
            if note.start <= out[-1].start:
                continue
            out[-1] = out[-1]._replace(end=note.start, offset=note.onset)
        out.append(note)
    return out


def top_line(notes: list[Note]) -> list[Note]:
    return _monophonic([max(group, key=lambda n: (n.pitch, n.velocity)) for group in _clusters(notes)])


def bottom_line(notes: list[Note]) -> list[Note]:
    return _monophonic([min(group, key=lambda n: (n.pitch, -n.velocity)) for group in _clusters(notes)])


def _polyphony(notes: list[Note]) -> float:
    """The mean number of notes sounding while any does."""
    total = sum(n.offset - n.onset for n in notes)
    union, until = 0.0, float("-inf")
    for note in sorted(notes, key=lambda n: n.onset):
        if note.offset > until:
            union += note.offset - max(note.onset, until)
            until = note.offset
    return total / union if union > 0 else 1.0


def _fold(pitch: int, low: int, high: int) -> int:
    while pitch < low:
        pitch += 12
    while pitch > high:
        pitch -= 12
    return pitch


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _thin(line: list[Note], gap: int, meter: Meter) -> list[Note]:
    """A monophonic line with at most one attack per gap ticks: the notes, spaced at least gap apart, of the greatest
    total weight (a weighted interval schedule). A note weighs more on a strong beat, the longer and louder it is, and
    when it changes pitch (a new harmony under a bass, a new step of a tune); each kept note lasts over the notes
    left out after it, so the line keeps sounding where the source did."""
    if gap <= 1 or len(line) < 2:
        return line
    tpb = meter.ticks_per_beat
    starts = [n.start for n in line]
    weights = []
    for i, n in enumerate(line):
        weight = 1.0 + 0.25 * meter.strength(n.start) + 0.2 * min((n.end - n.start) / tpb, 2.0) + 0.3 * n.velocity / 127
        if i == 0 or line[i - 1].pitch != n.pitch:
            weight += 0.5
        weights.append(weight)
    best = [0.0] * (len(line) + 1)
    take = [False] * len(line)
    earlier = []                # per note: how many notes start far enough before it
    for i, n in enumerate(line):
        j = bisect.bisect_right(starts, n.start - gap)
        earlier.append(j)
        if weights[i] + best[j] > best[i]:
            best[i + 1], take[i] = weights[i] + best[j], True
        else:
            best[i + 1] = best[i]
    kept: list[int] = []
    i = len(line) - 1
    while i >= 0:
        if take[i]:
            kept.append(i)
            i = earlier[i] - 1
        else:
            i -= 1
    kept.reverse()
    out = []
    for k, i in enumerate(kept):
        following = kept[k + 1] if k + 1 < len(kept) else len(line)
        end = max(line[j].end for j in range(i, following))
        if following < len(line):
            end = min(end, line[following].start)
        out.append(line[i]._replace(end=end))
    return out


def _fold_line(line: list[Note], low: int, high: int, max_leap: int | None) -> list[Note]:
    """A line within low..high by octaves. A note out of range, or a leap wider than max_leap, takes the octave
    nearest the note before, so the line moves on rather than jumping where the range ends."""
    out: list[Note] = []
    previous: int | None = None
    for n in line:
        pitch = n.pitch
        if not low <= pitch <= high or (max_leap is not None and previous is not None
                                        and abs(pitch - previous) > max_leap):
            choices = [p for p in range(low, high + 1) if p % 12 == n.pitch % 12]
            reference = previous if previous is not None else n.pitch
            pitch = min(choices, key=lambda p: (abs(p - reference), p)) if choices else _fold(pitch, low, high)
        out.append(n if pitch == n.pitch else n._replace(pitch=pitch))
        previous = pitch
    return out


def _shape_line(line: list[Note], gap_beats: float, pitch_range: tuple[int, int], max_leap: int | None,
                legato_beats: float, meter: Meter, role: str) -> list[Note]:
    """A melody or bass line within a level: thinned to its rhythm, folded into its range, rests shorter than
    legato filled."""
    tpb = meter.ticks_per_beat
    line = _fold_line(_thin(_monophonic(line), round(gap_beats * tpb), meter), *pitch_range, max_leap)
    legato = round(legato_beats * tpb)
    out = []
    for i, n in enumerate(line):
        end = n.end
        if i + 1 < len(line):
            following = line[i + 1].start
            end = following if following - end < legato else min(end, following)
        out.append(n._replace(end=end, role=role))
    return out


# --- the reduction ----------------------------------------------------------------------------------------------


def _normalized(parts: list[Part]) -> list[Part]:
    """Each part's velocities scaled towards one median, so a quiet transcribed part is not lost and a loud one does
    not cover the others; keys numbered once over all parts."""
    out, key = [], 0
    for part in parts:
        median = statistics.median(n.velocity for n in part.notes)
        scale = _clamp(72 / max(median, 1), 0.7, 1.4)
        notes = []
        for note in part.notes:
            notes.append(note._replace(velocity=int(_clamp(round(note.velocity * scale), 1, 127)), key=key))
            key += 1
        out.append(Part(part.name, part.program, part.is_drum, notes))
    return out


def _melody_line(part: Part) -> list[Note]:
    """A part's top line without its specks, and without the low notes that show through its rests (a pianist's
    left hand under a silent right): those more than an octave under the line's median, below middle C's fifth."""
    line = top_line([n for n in part.notes if n.offset - n.onset >= MIN_MELODY_SECONDS])
    if not line:
        return []
    floor = min(55, statistics.median(n.pitch for n in line) - 12)
    return [n for n in line if n.pitch >= floor]


def _melody_path(candidates: list[Part], window: int, windows: int) -> tuple[list[dict[int, list[Note]]], list[int]]:
    """Per candidate its top line by window, and per window the candidate that carries the tune (len(candidates):
    none), by a Viterbi path over the windows. Independent of a level: the levels differ only in where the passages
    are placed (_place_melody)."""
    lines = []
    for part in candidates:
        by_window: dict[int, list[Note]] = defaultdict(list)
        for note in _melody_line(part):
            by_window[note.start // window].append(note)
        lines.append(by_window)
    priors = [melody_prior(p.program) for p in candidates]
    monophony = [1 / max(_polyphony(p.notes), 1.0) for p in candidates]

    def score(c: int, w: int) -> float | None:
        line = lines[c].get(w)
        if not line:
            return None
        end = (w + 1) * window
        coverage = sum(min(n.end, end) - n.start for n in line) / window
        height = _clamp((statistics.median(n.pitch for n in line) - 48) / 36, 0, 1)
        loudness = statistics.fmean(n.velocity for n in line) / 127
        return 1.2 * priors[c] + 0.8 * height + 0.5 * loudness + 0.8 * min(1.0, 1.5 * coverage) + 0.3 * monophony[c]

    # state len(candidates): no melody in this window
    states = len(candidates) + 1
    none = len(candidates)
    best = [0.0] * states
    back: list[list[int]] = []
    for w in range(windows):
        scores = [score(c, w) for c in range(len(candidates))]
        silent = all(s is None for s in scores)
        scores.append(0.0 if silent else None)
        new, pointers = [float("-inf")] * states, [none] * states
        for s, gain in enumerate(scores):
            if gain is None:
                continue
            for previous in range(states):
                value = best[previous] - (SWITCH_COST if previous not in (s, none) and s != none else 0.0)
                if value > new[s]:
                    new[s], pointers[s] = value, previous
            new[s] += gain
        best = new
        back.append(pointers)
    path = [max(range(states), key=lambda s: best[s])]
    for pointers in reversed(back[1:]):
        path.append(pointers[path[-1]])
    path.reverse()
    return lines, path


def _place_melody(lines: list[dict[int, list[Note]]], path: list[int], none: int,
                  center: tuple[int, int]) -> list[Note]:
    """The melody of a path: the chosen candidates' passages, each moved by octaves into the level's center."""
    melody: list[Note] = []
    run: list[Note] = []

    def close_run() -> None:
        """A passage of one part, moved by octaves so its median is in the center; of two such octaves, the one that
        joins the passage before more closely, unless that is the farther move by more than an octave's leap."""
        if not run:
            return
        median = int(statistics.median(n.pitch for n in run))
        shifts = [s for s in range(-48, 49, 12) if center[0] <= median + s <= center[1]] or \
            [_fold(median, *center) - median]
        if melody:
            joined = melody[-1].pitch
            shift = min(shifts, key=lambda s: (abs(s) + abs(run[0].pitch + s - joined), abs(s)))
        else:
            shift = min(shifts, key=abs)
        for note in run:
            melody.append(note._replace(pitch=_fold(note.pitch + shift, LOWEST, HIGHEST)))
        run.clear()

    previous = none
    for w, state in enumerate(path):
        if state != previous:
            close_run()
            previous = state
        if state != none:
            run.extend(lines[state].get(w, []))
    close_run()
    return _monophonic(sorted(melody, key=lambda n: (n.start, -n.pitch)))


def _choose_melody(candidates: list[Part], window: int, windows: int, center: tuple[int, int]) -> list[Note]:
    """The melody: per window the top line of one candidate, chosen by a Viterbi path over the windows."""
    lines, path = _melody_path(candidates, window, windows)
    return _place_melody(lines, path, len(candidates), center)


def _sounding(line: list[Note], starts: list[int], tick: int) -> Note | None:
    """The note of a monophonic line that sounds at tick, or None."""
    i = bisect.bisect_right(starts, tick) - 1
    if i >= 0 and line[i].end > tick:
        return line[i]
    return None


def _bass_nearby(bass: list[Note], starts: list[int], tick: int, ticks_per_beat: int) -> bool:
    """Whether the bass part plays within a beat of tick: a rest that short is the bassist's, not a gap to fill."""
    i = bisect.bisect_right(starts, tick + ticks_per_beat) - 1
    while i >= 0 and bass[i].end >= tick - ticks_per_beat:
        if bass[i].start <= tick + ticks_per_beat:
            return True
        i -= 1
    return False


def _derived_bass(candidates: list[Part], melody: list[Note]) -> list[Note]:
    """Without a bass part, the bottom line of the low notes an octave or more under the melody: a pianist's or a
    guitarist's bass notes. Where none play, the harmony's lowest strong pitch fills in later."""
    keys = {n.key for n in melody}
    starts = [n.start for n in melody]
    low = []
    for part in candidates:
        for n in part.notes:
            if n.key in keys or n.pitch >= DERIVED_BASS_BELOW:
                continue
            top = _sounding(melody, starts, n.start)
            if top is None or top.pitch - n.pitch >= 12:
                low.append(n)
    return bottom_line(low)


def _attacks(pool: list[Note], weights: dict[int, float], min_gap: int, meter: Meter) -> list[list[Note]]:
    """The accompaniment's attacks: its onset clusters, at most one per min_gap ticks, the heavier one kept (one on a
    stronger beat weighs more)."""
    kept: list[tuple[list[Note], float]] = []
    for group in _clusters(pool):
        weight = sum(n.velocity * weights[n.key] for n in group) * (1 + 0.15 * meter.strength(group[0].start))
        if kept and group[0].start - kept[-1][0][0].start < min_gap:
            if weight > kept[-1][1] and (len(kept) < 2 or group[0].start - kept[-2][0][0].start >= min_gap):
                kept[-1] = (group, weight)
            continue
        kept.append((group, weight))
    return [group for group, _ in kept]


class _Song:
    """What every level of one source shares: its pitched parts (normalized), the bass parts, the melody's
    candidates and path, the harmony weights, and per melody center the melody and its derived bass. Made once for
    all levels; each level then only reduces."""

    def __init__(self, parts: list[Part], ticks_per_beat: int, meter: Meter):
        self.ticks_per_beat = ticks_per_beat
        self.meter = meter
        self.pitched = _normalized([p for p in parts if not is_left_out(p)])
        self._melodies: dict[tuple[int, int], list[Note]] = {}
        self._derived: dict[tuple[int, int], list[Note]] = {}
        self._bass_source: list[Note] | None = None
        self._weights: dict[int, float] | None = None
        if not self.pitched:
            return
        pitched = self.pitched
        window = WINDOW_BEATS * ticks_per_beat
        windows = max(n.end for p in pitched for n in p.notes) // window + 1
        bass_parts = [p for p in pitched if is_bass_program(p.program)]
        if not bass_parts and len(pitched) > 1:
            # a part playing low all the time is the bass, whatever its instrument; a lone part also carries the tune
            low = [p for p in pitched if statistics.median(n.pitch for n in p.notes) < 45 and _polyphony(p.notes) < 1.3]
            bass_parts = sorted(low, key=lambda p: statistics.median(n.pitch for n in p.notes))[:1]
        bass_ids = {id(p) for p in bass_parts}
        self.bass_parts = bass_parts
        self.candidates = [p for p in pitched if id(p) not in bass_ids]
        self._path = _melody_path(self.candidates, window, windows) if self.candidates else None

    def melody(self, center: tuple[int, int]) -> list[Note]:
        if center not in self._melodies:
            if self._path is None:
                self._melodies[center] = []
            else:
                lines, path = self._path
                self._melodies[center] = _place_melody(lines, path, len(self.candidates), center)
        return self._melodies[center]

    def bass_source(self, center: tuple[int, int]) -> list[Note]:
        if self.bass_parts:
            if self._bass_source is None:
                self._bass_source = bottom_line([n for p in self.bass_parts for n in p.notes])
            return self._bass_source
        if center not in self._derived:
            self._derived[center] = _derived_bass(self.candidates, self.melody(center))
        return self._derived[center]

    def weights(self) -> dict[int, float]:
        if self._weights is None:
            self._weights = {n.key: harmony_weight(p.program) for p in self.pitched for n in p.notes}
        return self._weights

    def arrange(self, level: Level) -> Hands:
        if not self.pitched:
            return Hands([], [])
        melody = self.melody(level.melody_center)
        if level.all_notes:
            hands = _full(self.pitched, self.bass_parts, melody)
        else:
            hands = _reduce(self, melody, level)
        if level.pedal:
            hands.pedal = _pedal(hands.right + hands.left, self.ticks_per_beat)
        return hands


def arrange(parts: list[Part], ticks_per_beat: int, options: Options = Options(), meter: Meter | None = None) -> Hands:
    """The piano version of parts (a source's notes, by part), at the options' level."""
    level = options.resolved()
    return _Song(parts, ticks_per_beat, meter or Meter(ticks_per_beat)).arrange(level)


def _reduce(song: _Song, melody: list[Note], level: Level) -> Hands:
    """Melody, bass, and chords within the level's limits."""
    ticks_per_beat, meter = song.ticks_per_beat, song.meter
    bass_source = song.bass_source(level.melody_center)
    # every note of the source lines is spoken for, also those the level's rhythm leaves out: none returns as a chord
    used = {n.key for n in melody} | {n.key for n in bass_source}
    melody = _shape_line(melody, level.melody_gap, level.melody_range, level.max_leap, level.legato, meter, "melody")
    bass_line = _shape_line(bass_source, level.bass_gap, level.bass_range, None, level.legato, meter, "bass")

    weights = song.weights()
    # the bass parts' other notes (double stops, octaves) colour the chords too
    pool = sorted((n for p in song.candidates + song.bass_parts for n in p.notes if n.key not in used),
                  key=lambda n: (n.onset, n.key))

    chords, fallback_bass = _chords(pool, weights, melody, bass_line, ticks_per_beat, level, meter)
    if fallback_bass:
        merged = sorted(bass_line + fallback_bass, key=lambda n: (n.start, n.pitch))
        bass_line = _shape_line(merged, level.bass_gap, level.bass_range, None, level.legato, meter, "bass")
    if level.octave_bass:
        bass_line += [n._replace(pitch=n.pitch - 12, role="octave") for n in bass_line
                      if n.pitch - 12 >= LOWEST and n.end - n.start >= ticks_per_beat and meter.strength(n.start) >= 3]
    return _hands(melody, bass_line, chords, level, meter)


def _chords(pool: list[Note], weights: dict[int, float], melody: list[Note], bass: list[Note], ticks_per_beat: int,
            level: Level, meter: Meter) -> tuple[list[tuple[Note, str]], list[Note]]:
    """The accompaniment's notes with the hand of each, and the bass notes made where no bass part plays."""
    if not pool:
        return [], []
    attacks = _attacks(pool, weights, max(1, round(level.chord_gap * ticks_per_beat)), meter)
    melody_starts = [n.start for n in melody]
    bass_starts = [n.start for n in bass]
    out: list[tuple[Note, str]] = []
    fallback: list[Note] = []
    held: dict[int, int] = {}            # pitch class -> index in out of the note now held for it
    previous_pitch: dict[int, int] = {}  # pitch class -> where it was voiced last
    order = sorted(pool, key=lambda n: n.onset)
    next_note = 0
    active: list[Note] = []
    last_offset = max(n.offset for n in pool)
    last_end = max(n.end for n in pool)

    for i, group in enumerate(attacks):
        t, tick = group[0].onset, group[0].start
        if i + 1 < len(attacks):
            t_next, tick_next = attacks[i + 1][0].onset, attacks[i + 1][0].start
        else:
            t_next, tick_next = last_offset, last_end
        while next_note < len(order) and order[next_note].onset < t_next:
            active.append(order[next_note])
            next_note += 1
        active = [n for n in active if n.offset > t]
        fresh = {n.key for n in group}

        pc_weight: dict[int, float] = defaultdict(float)
        pc_register: dict[int, list[float]] = defaultdict(lambda: [0.0, 0.0])
        pc_notes: dict[int, list[Note]] = defaultdict(list)
        pc_fresh: set[int] = set()
        for n in active:
            overlap = min(n.offset, t_next) - max(n.onset, t)
            if overlap <= 0:
                continue
            w = n.velocity * overlap * weights[n.key] * (1.5 if n.key in fresh else 1.0)
            pc = n.pitch % 12
            pc_weight[pc] += w
            pc_register[pc][0] += w * n.pitch
            pc_register[pc][1] += w
            pc_notes[pc].append(n)
            if n.key in fresh:
                pc_fresh.add(pc)
        if not pc_weight:
            held.clear()
            continue

        top = _sounding(melody, melody_starts, tick)
        if top is None:
            # a melody note that starts within this chord still bounds it
            j = bisect.bisect_left(melody_starts, tick)
            if j < len(melody) and melody[j].start < tick_next:
                top = melody[j]
        low = _sounding(bass, bass_starts, tick)
        if low is None and bass:
            j = bisect.bisect_left(bass_starts, tick)
            if j < len(bass) and bass[j].start < tick_next:
                low = bass[j]

        if low is None and (not bass or not _bass_nearby(bass, bass_starts, tick, ticks_per_beat)):
            # no bass part here: the lowest strong pitch becomes the bass, held while it does not change
            strongest = max(pc_weight.values())
            lowest = min((n for n in active if pc_weight.get(n.pitch % 12, 0.0) >= CHORD_SHARE * strongest),
                         key=lambda n: (n.pitch, n.key))
            pc = lowest.pitch % 12
            pitch = _fold(lowest.pitch, *FALLBACK_BASS_RANGE)
            end = min(tick_next, max(n.end for n in pc_notes[pc]))
            if end <= tick:
                end = tick_next
            velocity = int(_clamp(statistics.fmean(n.velocity for n in pc_notes[pc]) * 0.9, 30, 110))
            if fallback and fallback[-1].pitch == pitch and fallback[-1].end >= tick and pc not in pc_fresh:
                fallback[-1] = fallback[-1]._replace(end=max(fallback[-1].end, end), offset=t_next)
            else:
                fallback.append(Note(tick, end, pitch, velocity, t, t_next, role="bass"))
            low = fallback[-1]

        bass_pc = low.pitch % 12 if low else None
        top_pc = top.pitch % 12 if top else None
        ranked = sorted(pc_weight, key=lambda pc: (-pc_weight[pc] * (0.5 if pc in (bass_pc, top_pc) else 1.0), pc))
        strongest = pc_weight[ranked[0]]
        chosen = [pc for pc in ranked if pc_weight[pc] >= CHORD_SHARE * strongest][:level.chord_notes]

        voiced = _voice(chosen, top, low, previous_pitch, pc_register, level)
        lefts = sum(1 for _, hand in voiced if hand == "left")
        extras = min(level.left_extra - lefts, level.left_hand_notes - 1 - lefts)
        if low is not None and extras > 0:
            # the left hand adds the fifth above the bass, a tenth (an open voicing), or the octave, from the chord,
            # under the right hand
            ceiling = min([p for p, hand in voiced if hand == "right"] + ([top.pitch] if top else []) or [HIGHEST])
            for interval in (7, 16, 15, 12):
                pitch = low.pitch + interval
                if extras <= 0:
                    break
                if pc_weight.get(pitch % 12, 0.0) >= CHORD_SHARE * strongest and pitch not in (p for p, _ in voiced) \
                        and interval <= level.hand_span and pitch < ceiling - 2 \
                        and (top is None or interval == 7 or pitch < top.pitch - level.hand_span):
                    voiced.append((pitch, "left"))
                    extras -= 1

        new_held: dict[int, int] = {}
        for pitch, hand in voiced:
            pc = pitch % 12
            # as long as the instruments hold it, until the next chord at most
            end = min(tick_next, max(n.end for n in pc_notes[pc]))
            if end <= tick:
                end = max(tick_next, tick + 1)
            velocity = int(_clamp(statistics.fmean(n.velocity for n in pc_notes[pc]) * 0.8, 25, 100))
            index = held.get(pc)
            if (index is not None and pc not in pc_fresh and out[index][0].pitch == pitch and out[index][1] == hand
                    and out[index][0].end >= tick):
                # held from the chord before: tied, not struck again
                out[index] = (out[index][0]._replace(end=max(out[index][0].end, end), offset=t_next), hand)
                new_held[pc] = index
                continue
            out.append((Note(tick, end, pitch, velocity, t, t_next, role="chord"), hand))
            new_held[pc] = len(out) - 1
            previous_pitch[pc] = pitch
        held = new_held
    return out, fallback


def _voice(chosen: list[int], top: Note | None, low: Note | None, previous: dict[int, int],
           register: dict[int, list[float]], level: Level) -> list[tuple[int, str]]:
    """Pitches for a chord's pitch classes (strongest first), each with its hand, "right" or "left".

    The right hand plays within its span below the melody, never a second or a unison against it; the left hand
    within its span above the bass, never a close interval low down. A pitch class stays where it was in the chord
    before when it can (common tones held, smooth inner voices); otherwise it goes near where the instruments played it,
    in the right hand when both hands can take it, as a pianist fills chords under the tune. A level with doubling
    fills the right hand's remaining room with the strongest tones in another octave."""
    span = level.hand_span
    if top is not None:
        right_low, right_high = top.pitch - span, top.pitch - 2
    else:
        right_low, right_high = DEFAULT_SPLIT, DEFAULT_SPLIT + span
    if low is not None:
        right_low = max(right_low, low.pitch + 3)
        left_low, left_high = low.pitch + 3, low.pitch + span
    else:
        left_low, left_high = DEFAULT_SPLIT - span, DEFAULT_SPLIT - 1
    left_high = min(left_high, right_low - 1)          # the hands do not cross
    room = {"right": level.right_hand_notes - (top is not None),
            "left": level.left_hand_notes - (low is not None)}

    def muddy(pitch: int) -> bool:
        return low is not None and pitch < MUDDY_BELOW and pitch - low.pitch < MUDDY_INTERVAL

    used: set[int] = set()
    voiced: list[tuple[int, str]] = []
    for pc in chosen:
        target = previous.get(pc)
        if target is None:
            target = register[pc][0] / register[pc][1]
        candidates = []
        for hand, lo, hi, cost in (("right", right_low, right_high, 0), ("left", left_low, left_high, 2)):
            if room[hand] <= 0:
                continue
            lo, hi = max(lo, LOWEST), min(hi, HIGHEST)
            # only the pitches of this class: every twelfth from the first at or above lo
            for pitch in range(lo + (pc - lo) % 12, hi + 1, 12):
                if pitch not in used and not muddy(pitch):
                    candidates.append((abs(pitch - target) + cost, pitch, hand))
        if not candidates:
            continue
        _, pitch, hand = min(candidates)
        used.add(pitch)
        room[hand] -= 1
        voiced.append((pitch, hand))
    if level.doubling:
        for pc in chosen:
            if room["right"] <= 0:
                break
            placed = [p for p, _ in voiced if p % 12 == pc]
            if not placed:
                continue
            for pitch in (placed[0] - 12, placed[0] + 12):
                # never a second against a tone already there: a doubling opens the chord, not clusters it
                if right_low <= pitch <= right_high and LOWEST <= pitch <= HIGHEST and pitch not in used \
                        and all(abs(pitch - other) > 2 for other in used):
                    used.add(pitch)
                    room["right"] -= 1
                    voiced.append((pitch, "right"))
                    break
    return voiced


def _velocity(note: Note, meter: Meter) -> Note:
    """Phrasing by role: the melody brought forward over the chords (softened when made), the bass a little under
    it; downbeats a touch stronger than the beats between."""
    v = float(note.velocity)
    if note.role == "melody":
        v = v * 1.05 + 6
    elif note.role == "bass":
        v *= 0.9
    elif note.role == "octave":
        v *= 0.8
    v += {4: 4, 3: 2}.get(meter.strength(note.start), 0)
    return note._replace(velocity=int(_clamp(round(v), 20, 120)))


def _hands(melody: list[Note], bass: list[Note], chords: list[tuple[Note, str]], level: Level, meter: Meter) -> Hands:
    """Melody and right-hand chord notes; bass and left-hand chord notes, within the level's limits. A chord note held
    while the melody moves to its key, next to it, or under it ends there, so the tune stays on top and clear."""
    starts = [n.start for n in melody]
    right = list(melody)
    for note, hand in chords:
        if hand != "right":
            continue
        i = bisect.bisect_right(starts, note.start)
        while i < len(melody) and melody[i].start < note.end:
            if melody[i].pitch <= note.pitch + 1:
                note = note._replace(end=melody[i].start, offset=melody[i].onset)
                break
            i += 1
        right.append(note)
    left = list(bass) + [n for n, hand in chords if hand == "left"]
    tpb = meter.ticks_per_beat
    gap = round(level.chord_gap * tpb)
    snap = min(gap // 2, tpb // 8)
    right = _clean(_attack_limit(_clean([_velocity(n, meter) for n in right]), gap, snap))
    left = _clean(_attack_limit(_clean([_velocity(n, meter) for n in left]), gap, snap))
    right, left = _hold_limits(right, left, level, min_length=max(1, tpb // 8))
    return Hands(right, left)


def _attack_limit(notes: list[Note], gap: int, snap: int) -> list[Note]:
    """One hand's accompaniment attacks at least gap ticks from each other and from the hand's tune or bass attacks,
    unless struck with them. An attack within snap of a tune or bass attack is struck with it (a flam between
    instruments is no rhythm to keep); others too close are left out."""
    lines = [n for n in notes if _priority(n) >= PROTECTED]
    if gap <= 1:
        return notes
    line_ticks = sorted({n.start for n in lines})
    line_set = set(line_ticks)
    by_tick: dict[int, list[Note]] = defaultdict(list)
    for n in notes:
        if _priority(n) < PROTECTED:
            by_tick[n.start].append(n)
    out = list(lines)
    last: int | None = None
    for tick in sorted(by_tick):
        j = bisect.bisect_left(line_ticks, tick)
        near = line_ticks[max(0, j - 1):j + 1]
        nearest = min(near, key=lambda p: (abs(p - tick), p)) if near else None
        target = nearest if nearest is not None and abs(nearest - tick) <= snap else tick
        if target not in line_set and nearest is not None and abs(nearest - target) < gap:
            continue
        if last is not None and target != last and abs(target - last) < gap:
            continue
        for n in by_tick[tick]:
            if n.end > target:
                # onset seconds are not read after this point: only ticks are written
                out.append(n._replace(start=target))
        last = target
    return out


@dataclass
class _Held:
    note: Note
    hand: str
    alive: bool = True


def _hold_limits(right: list[Note], left: list[Note], level: Level, min_length: int) -> tuple[list[Note], list[Note]]:
    """Both hands playable at every attack: the hands never cross or share a key, and neither holds more notes than
    the level allows or spans more than its reach. What gives way first is a chord note (a held one ends, one not
    yet struck is left out), the one farthest from the hand's tune or bass; then the bass's octave; the melody and
    bass themselves only against each other, where the bass goes an octave down. Chord notes cut shorter than
    min_length are left out: a blip is no chord."""
    records = sorted([_Held(n, "right") for n in right] + [_Held(n, "left") for n in left],
                     key=lambda r: (r.note.start, r.hand, r.note.pitch))
    limits = {"right": level.right_hand_notes, "left": level.left_hand_notes}
    active: list[_Held] = []

    def remove(record: _Held, tick: int) -> None:
        if record.note.start < tick:
            record.note = record.note._replace(end=tick)
        else:
            record.alive = False
        # by equality, as list.remove: the first record equal to this one
        active.remove(record)

    i = 0
    for tick in sorted({r.note.start for r in records}):
        while i < len(records) and records[i].note.start == tick:
            active.append(records[i])
            i += 1
        active[:] = [r for r in active if r.alive and r.note.end > tick]

        while True:
            rights = [r for r in active if r.hand == "right"]
            lefts = [r for r in active if r.hand == "left"]
            if not rights or not lefts:
                break
            low_right = min(rights, key=lambda r: (r.note.pitch, _priority(r.note)))
            high_left = max(lefts, key=lambda r: (r.note.pitch, -_priority(r.note)))
            if high_left.note.pitch < low_right.note.pitch:
                break
            if min(_priority(low_right.note), _priority(high_left.note)) >= PROTECTED:
                if high_left.note.pitch - 12 >= LOWEST:
                    high_left.note = high_left.note._replace(pitch=high_left.note.pitch - 12)
                else:
                    remove(high_left, tick)
                continue
            remove(min((low_right, high_left), key=lambda r: (_priority(r.note), r.note.start, r.hand)), tick)

        for hand in ("right", "left"):
            while True:
                group = [r for r in active if r.hand == hand]
                if not group:
                    break
                pitches = [r.note.pitch for r in group]
                over_count = len(group) > limits[hand]
                if not over_count and max(pitches) - min(pitches) <= level.hand_span:
                    break
                anchor = max(pitches) if hand == "right" else min(pitches)
                choices = group if over_count else [min(group, key=lambda r: r.note.pitch),
                                                    max(group, key=lambda r: r.note.pitch)]
                remove(min(choices, key=lambda r: (_priority(r.note), r.note.start == tick,
                                                   -abs(r.note.pitch - anchor), r.note.pitch)), tick)

    kept = [r for r in records if r.alive and r.note.end > r.note.start
            and (_priority(r.note) >= PROTECTED or r.note.end - r.note.start >= min_length)]
    return ([r.note for r in kept if r.hand == "right"], [r.note for r in kept if r.hand == "left"])


def _clean(notes: list[Note]) -> list[Note]:
    """On the keyboard, in onset order, with one note per key and start (the tune's or bass's before a chord's)."""
    seen: set[tuple[int, int]] = set()
    out: list[Note] = []
    folded = []
    for n in notes:
        if n.end > n.start:
            pitch = _fold(n.pitch, LOWEST, HIGHEST)
            folded.append(n if pitch == n.pitch else n._replace(pitch=pitch))
    for n in sorted(folded, key=lambda n: (n.start, n.pitch, -_priority(n), -n.velocity, n.key)):
        if (n.start, n.pitch) in seen:
            continue
        seen.add((n.start, n.pitch))
        out.append(n)
    return out


# --- the full level ---------------------------------------------------------------------------------------------


def _full(pitched: list[Part], bass_parts: list[Part], melody: list[Note]) -> Hands:
    """Every pitched note, for the piano's sound: on the keyboard, balanced by part, low mud cleared, unisons merged,
    a key struck again ending its earlier note, extreme attacks thinned, and split between the hands."""
    melody_keys = {n.key for n in melody}
    bass_ids = {id(p) for p in bass_parts}
    notes = []
    for part in pitched:
        gain = full_gain(part.program, id(part) in bass_ids)
        for n in part.notes:
            if n.key in melody_keys:
                role, velocity = "melody", n.velocity * 1.15 + 4
            else:
                role, velocity = ("bass" if id(part) in bass_ids else "chord"), n.velocity * gain
            notes.append(n._replace(pitch=_fold(n.pitch, LOWEST, HIGHEST), role=role,
                                    velocity=int(_clamp(round(velocity), 12, 120))))
    notes = _restrike(_merge_unisons(_lift_mud(notes)))
    notes = _limit_keys(notes)
    return _split_full(notes)


def _merge_unisons(notes: list[Note]) -> list[Note]:
    """Notes of one key struck together (by two parts, or doubled by octave folding) as one: the earliest start, the
    latest end, the loudest velocity, the weightiest role."""
    out: list[Note] = []
    first_onset = 0.0
    for n in sorted(notes, key=lambda n: (n.pitch, n.onset, n.key)):
        if out and out[-1].pitch == n.pitch and n.onset - first_onset <= CLUSTER_SECONDS:
            last = out[-1]
            role = last.role if _priority(last) >= _priority(n) else n.role
            out[-1] = last._replace(end=max(last.end, n.end), offset=max(last.offset, n.offset),
                                    velocity=max(last.velocity, n.velocity), role=role)
            continue
        out.append(n)
        first_onset = n.onset
    return sorted(out, key=lambda n: (n.start, n.pitch))


def _restrike(notes: list[Note]) -> list[Note]:
    """A key struck while it sounds ends its earlier note there, as on a piano."""
    out: list[Note] = []
    last_by_pitch: dict[int, int] = {}
    for n in sorted(notes, key=lambda n: (n.start, n.pitch)):
        i = last_by_pitch.get(n.pitch)
        if i is not None and out[i].end > n.start:
            out[i] = out[i]._replace(end=n.start, offset=n.onset)
        last_by_pitch[n.pitch] = len(out)
        out.append(n)
    return [n for n in out if n.end > n.start]


def _lift_mud(notes: list[Note]) -> list[Note]:
    """Low close intervals (under MUDDY_BELOW, closer than a fifth) are mud on a piano: the upper note of such a pair
    goes up by octaves until it is clear. The pitch class stays, so the harmony does too."""
    out: list[Note] = []
    # Only notes below MUDDY_BELOW can make mud, and a note sounds until its end: the low notes that still sound,
    # pruned when one is checked (starts only grow, so a note pruned once would stay out).
    low: list[Note] = []
    for n in sorted(notes, key=lambda n: (n.start, n.pitch, n.key)):
        pitch = n.pitch
        if pitch < MUDDY_BELOW and n.role != "bass":
            low = [a for a in low if a.end > n.start]
            while pitch < MUDDY_BELOW and pitch + 12 <= HIGHEST \
                    and any(a.pitch < pitch and pitch - a.pitch < MUDDY_INTERVAL for a in low):
                pitch += 12
            if pitch != n.pitch:
                n = n._replace(pitch=pitch)
        out.append(n)
        if pitch < MUDDY_BELOW:
            low.append(n)
    return out


def _limit_keys(notes: list[Note]) -> list[Note]:
    """At most FULL_MAX_KEYS keys at one attack: the melody, the lowest and the highest stay; then the loudest of
    the pitch classes not yet there, then the loudest doublings."""
    out: list[Note] = []
    for group in _clusters(notes):
        if len(group) <= FULL_MAX_KEYS:
            out.extend(group)
            continue
        by_pitch = sorted(group, key=lambda n: n.pitch)
        keep = {id(n) for n in group if n.role == "melody"} | {id(by_pitch[0]), id(by_pitch[-1])}
        classes = {n.pitch % 12 for n in group if id(n) in keep}
        rest = sorted((n for n in group if id(n) not in keep), key=lambda n: (-n.velocity, n.pitch))
        for n in [n for n in rest if n.pitch % 12 not in classes] + [n for n in rest if n.pitch % 12 in classes]:
            if len(keep) >= FULL_MAX_KEYS:
                break
            keep.add(id(n))
            classes.add(n.pitch % 12)
        out.extend(n for n in group if id(n) in keep)
    return sorted(out, key=lambda n: (n.start, n.pitch))


def _split_full(notes: list[Note]) -> Hands:
    """The hands of the full level, per attack: the melody right, the bass left, and the rest split at the widest gap
    between their pitches around middle C (at middle C when there is none)."""
    right, left = [], []
    for group in _clusters(notes):
        pitches = sorted({n.pitch for n in group if n.role == "chord"})
        split, widest = DEFAULT_SPLIT, 0
        for a, b in zip(pitches, pitches[1:]):
            if b - a > widest and FULL_SPLIT_RANGE[0] <= (a + b) / 2 <= FULL_SPLIT_RANGE[1]:
                split, widest = b, b - a
        for n in group:
            if n.role == "melody" or (n.role != "bass" and n.pitch >= split):
                right.append(n)
            else:
                left.append(n)
    return Hands(sorted(right, key=lambda n: (n.start, n.pitch)), sorted(left, key=lambda n: (n.start, n.pitch)))


def _pedal(notes: list[Note], ticks_per_beat: int) -> list[tuple[int, int]]:
    """Sustain pedal per harmony: down just after a harmony begins, up just before the next. A harmony changes where
    the lowest sounding pitch class or the set of strong pitch classes (by duration and loudness, per beat) does.
    Harmonies shorter than PEDAL_MIN_BEATS, and passages where the melody runs faster than PEDAL_MAX_MELODY_RATE
    notes a beat, are left dry: there the pedal blurs more than it binds."""
    if not notes:
        return []
    tpb = ticks_per_beat
    last = max(n.end for n in notes)
    beats = last // tpb + 1
    weights: list[dict[int, float]] = [defaultdict(float) for _ in range(beats)]
    lowest: list[int | None] = [None] * beats
    for n in notes:
        for b in range(n.start // tpb, min(beats - 1, (n.end - 1) // tpb) + 1):
            overlap = min(n.end, (b + 1) * tpb) - max(n.start, b * tpb)
            if overlap <= 0:
                continue
            weights[b][n.pitch % 12] += overlap * n.velocity
            if 4 * overlap >= tpb and (lowest[b] is None or n.pitch < lowest[b]):
                lowest[b] = n.pitch

    def harmony(b: int) -> tuple[int | None, frozenset[int]] | None:
        if not weights[b]:
            return None
        strongest = max(weights[b].values())
        return (None if lowest[b] is None else lowest[b] % 12,
                frozenset(pc for pc, w in weights[b].items() if w >= 0.3 * strongest))

    starts = sorted(n.start for n in notes)
    melody_starts = sorted(n.start for n in notes if n.role == "melody")

    def change_tick(b: int) -> int:
        """A harmony's first attack: the earliest note starting about its beat, or the beat."""
        i = bisect.bisect_left(starts, b * tpb - tpb // 8)
        return starts[i] if i < len(starts) and starts[i] <= b * tpb + tpb // 4 else b * tpb

    segments: list[tuple[int, int]] = []
    opened: int | None = None
    previous = None
    for b in range(beats + 1):
        current = harmony(b) if b < beats else None
        if current is None:
            changed = True
        elif previous is None:
            changed = True
        else:
            union = current[1] | previous[1]
            changed = current[0] != previous[0] or (len(current[1] & previous[1]) / len(union) if union else 1) < 0.5
        if changed:
            tick = change_tick(b) if current is not None else min(b * tpb, last)
            if opened is not None and tick > opened:
                segments.append((opened, tick))
            opened = tick if current is not None else None
        previous = current

    pedal = []
    delay, lead = max(1, tpb // 16), max(1, tpb // 32)
    for start, end in segments:
        if end - start < PEDAL_MIN_BEATS * tpb:
            continue
        runs = bisect.bisect_left(melody_starts, end) - bisect.bisect_left(melody_starts, start)
        if runs > PEDAL_MAX_MELODY_RATE * (end - start) / tpb:
            continue
        down, up = start + delay, min(end, last) - lead
        if down < up:
            pedal.append((down, up))
    return pedal


# --- MIDI files ---

DRUM_CHANNEL = 9
SUSTAIN = 64
RIGHT_CHANNEL, LEFT_CHANNEL = 0, 1
RIGHT_TRACK, LEFT_TRACK = "Piano right hand", "Piano left hand"
CONDUCTOR_EVENTS = {"set_tempo", "time_signature", "key_signature", "marker", "cue_marker", "smpte_offset"}
DEFAULT_TEMPO = 500_000


class Clock:
    """Seconds of ticks, by the file's tempo changes (in any track, as players read them)."""

    def __init__(self, ticks_per_beat: int, tempos: list[tuple[int, int]]):
        self.ticks_per_beat = ticks_per_beat
        self.ticks = [0]
        self.seconds = [0.0]
        self.tempos = [DEFAULT_TEMPO]
        for tick, tempo in sorted(tempos):
            if tick == self.ticks[-1]:
                self.tempos[-1] = tempo
                continue
            self.seconds.append(self.at(tick))
            self.ticks.append(tick)
            self.tempos.append(tempo)

    def at(self, tick: int) -> float:
        i = bisect.bisect_right(self.ticks, tick) - 1
        return self.seconds[i] + (tick - self.ticks[i]) * self.tempos[i] / 1e6 / self.ticks_per_beat


# A track's events as read, for building its parts: (tick, kind, channel, first data byte, second data byte).
NAME, PROGRAM, CONTROL, NOTE_ON, NOTE_OFF, OTHER = range(6)


def _absolute(track: mido.MidiTrack):
    tick = 0
    for message in track:
        tick += message.time
        yield tick, message


def _mido_events(track: mido.MidiTrack) -> Iterator[tuple[int, int, int, int, object]]:
    for tick, message in _absolute(track):
        kind = message.type
        if kind == "track_name":
            yield tick, NAME, 0, 0, message.name
        elif kind == "program_change":
            yield tick, PROGRAM, message.channel, message.program, 0
        elif kind == "control_change":
            yield tick, CONTROL, message.channel, message.control, message.value
        elif kind == "note_on":
            yield tick, NOTE_ON if message.velocity > 0 else NOTE_OFF, message.channel, message.note, message.velocity
        elif kind == "note_off":
            yield tick, NOTE_OFF, message.channel, message.note, message.velocity
        else:
            yield tick, OTHER, 0, 0, 0


def song_end(midi: mido.MidiFile) -> int:
    """The tick of the file's last event, end of track included: where the piano version ends too."""
    return max((sum(m.time for m in track) for track in midi.tracks), default=0)


def read(midi: mido.MidiFile) -> tuple[list[Part], list[tuple[int, mido.MetaMessage]]]:
    """The file's parts, one per track and channel, and its conductor events with their ticks. Notes held by the
    sustain pedal last until it lifts (or the key is struck again); a note of no length gets a sixty-fourth's."""
    conductor: list[tuple[int, mido.MetaMessage]] = []
    for track in midi.tracks:
        for tick, message in _absolute(track):
            if message.is_meta and message.type in CONDUCTOR_EVENTS:
                conductor.append((tick, message))
    return _parts(midi.ticks_per_beat, [_mido_events(track) for track in midi.tracks], conductor), conductor


def _parts(ticks_per_beat: int, tracks, conductor: list[tuple[int, mido.MetaMessage]], as_written: bool = False
           ) -> list[Part]:
    """The parts of tracks of events (_mido_events, or _parse's), by track and channel. as_written: the notes as the
    file has them, neither held by the sustain pedal nor lengthened."""
    clock = Clock(ticks_per_beat, [(tick, m.tempo) for tick, m in conductor if m.type == "set_tempo"])
    shortest = 0 if as_written else max(1, ticks_per_beat // 16)

    parts: dict[tuple[int, int], Part] = {}
    for index, events in enumerate(tracks):
        name = ""
        programs: dict[int, int] = {}
        # (channel, pitch) -> [(start tick, velocity)], first in, first out
        sounding: dict[tuple[int, int], list[tuple[int, int]]] = {}
        pedal: dict[int, bool] = {}
        # notes released while the pedal holds them: (channel, pitch) -> [(start, velocity)]
        pedalled: dict[tuple[int, int], list[tuple[int, int]]] = {}
        notes: dict[int, list[tuple[int, int, int, int]]] = {}
        last = 0

        def finish(channel: int, pitch: int, start: int, end: int, velocity: int) -> None:
            notes.setdefault(channel, []).append((start, max(end, start + shortest), pitch, velocity))

        def release(channel: int, tick: int) -> None:
            for (c, pitch), held in list(pedalled.items()):
                if c == channel:
                    for start, velocity in held:
                        finish(c, pitch, start, tick, velocity)
                    del pedalled[(c, pitch)]

        for tick, kind, channel, first, second in events:
            last = tick
            if kind == NAME:
                if not name:
                    name = second
            elif kind == PROGRAM:
                programs.setdefault(channel, first)
            elif kind == CONTROL and first == SUSTAIN and not as_written:
                down = second >= 64
                if pedal.get(channel) and not down:
                    release(channel, tick)
                pedal[channel] = down
            elif kind == NOTE_ON:
                key = (channel, first)
                for start, velocity in pedalled.pop(key, []):
                    finish(channel, first, start, tick, velocity)    # struck again under the pedal
                sounding.setdefault(key, []).append((tick, second))
            elif kind == NOTE_OFF:
                key = (channel, first)
                if sounding.get(key):
                    start, velocity = sounding[key].pop(0)
                    if pedal.get(channel):
                        pedalled.setdefault(key, []).append((start, velocity))
                    else:
                        finish(channel, first, start, tick, velocity)
        for (channel, pitch), held in list(sounding.items()) + list(pedalled.items()):
            for start, velocity in held:
                finish(channel, pitch, start, last, velocity)

        for channel, raw in sorted(notes.items()):
            part = parts.setdefault((index, channel), Part(name or f"Track {index}", programs.get(channel, 0),
                                                           channel == DRUM_CHANNEL))
            at = clock.at
            for start, end, pitch, velocity in sorted(raw):
                part.notes.append(Note(start, end, pitch, velocity, at(start), at(end)))
    return list(parts.values())


@dataclass
class Source:
    """A MIDI file as the levels need it, read once."""
    type: int
    ticks_per_beat: int
    parts: list[Part]
    conductor: list[tuple[int, mido.MetaMessage]]
    end: int

    @property
    def note_count(self) -> int:
        return sum(len(p.notes) for p in self.parts)


def _variable(data: bytes, pos: int) -> tuple[int, int]:
    value = 0
    while True:
        if pos >= len(data):
            raise EOFError
        byte = data[pos]
        pos += 1
        value = (value << 7) | (byte & 0x7F)
        if byte < 0x80:
            return value, pos


def _parse(data: bytes, as_written: bool = False) -> Source:
    """A MIDI file's bytes, read as mido reads them with clip=True (running status, data bytes clipped to 127), but
    into plain tuples: only meta events become mido messages. EOFError or OSError for a file that is not MIDI."""
    if len(data) < 8:
        raise EOFError
    name, size = struct.unpack_from(">4sL", data, 0)
    if name != b"MThd":
        raise OSError("MThd not found. Probably not a MIDI file")
    if len(data) < 8 + size or size < 6:
        raise EOFError
    midi_type, count, ticks_per_beat = struct.unpack_from(">hhh", data, 8)
    pos = 8 + size
    conductor: list[tuple[int, mido.MetaMessage]] = []
    tracks: list[list[tuple[int, int, int, int, object]]] = []
    end = 0
    lengths = {status: spec["length"] for status, spec in SPEC_BY_STATUS.items()}
    with meta_charset("latin1"):
        for _ in range(count):
            if pos + 8 > len(data):
                raise EOFError
            name, size = struct.unpack_from(">4sL", data, pos)
            if name != b"MTrk":
                raise OSError("no MTrk header at start of track")
            pos += 8
            stop = pos + size
            if stop > len(data):
                raise EOFError
            events: list[tuple[int, int, int, int, object]] = []
            append = events.append
            tick = 0
            last_status = None
            while pos < stop:
                delta, pos = _variable(data, pos)
                tick += delta
                if pos >= stop:
                    raise EOFError
                status = data[pos]
                pos += 1
                if status < 0x80:
                    if last_status is None:
                        raise OSError("running status without last_status")
                    status = last_status
                    if status not in (0xF0, 0xF7):
                        pos -= 1                # the byte was the first data byte (a sysex drops it, as mido)
                elif status != 0xFF:
                    last_status = status
                kind = status & 0xF0
                if kind in (0x80, 0x90, 0xA0, 0xB0, 0xE0):
                    if pos + 2 > stop:
                        raise EOFError
                    first, second = min(data[pos], 127), min(data[pos + 1], 127)
                    pos += 2
                    channel = status & 0x0F
                    if kind == 0x90:
                        append((tick, NOTE_ON if second > 0 else NOTE_OFF, channel, first, second))
                    elif kind == 0x80:
                        append((tick, NOTE_OFF, channel, first, second))
                    elif kind == 0xB0:
                        append((tick, CONTROL, channel, first, second))
                    else:
                        append((tick, OTHER, 0, 0, 0))
                elif kind in (0xC0, 0xD0):
                    if pos + 1 > stop:
                        raise EOFError
                    first = min(data[pos], 127)
                    pos += 1
                    append((tick, PROGRAM, status & 0x0F, first, 0) if kind == 0xC0 else (tick, OTHER, 0, 0, 0))
                elif status == 0xFF:
                    if pos >= stop:
                        raise EOFError
                    meta_type = data[pos]
                    length, pos = _variable(data, pos + 1)
                    if length > 1_000_000:
                        raise OSError(f"Message length {length} exceeds maximum length 1000000")
                    if pos + length > stop:
                        raise EOFError
                    message = build_meta_message(meta_type, list(data[pos:pos + length]), delta)
                    pos += length
                    if message.type in CONDUCTOR_EVENTS:
                        conductor.append((tick, message))
                    append((tick, NAME, 0, 0, message.name) if message.type == "track_name"
                           else (tick, OTHER, 0, 0, 0))
                elif status in (0xF0, 0xF7):
                    length, pos = _variable(data, pos)
                    if length > 1_000_000:
                        raise OSError(f"Message length {length} exceeds maximum length 1000000")
                    pos += length
                    if pos > stop:
                        raise EOFError
                    append((tick, OTHER, 0, 0, 0))
                else:
                    if status not in lengths:
                        raise OSError(f"undefined status byte 0x{status:02x}")
                    pos += lengths[status] - 1
                    if pos > stop:
                        raise EOFError
                    append((tick, OTHER, 0, 0, 0))
            tracks.append(events)
            end = max(end, tick)
    parts = _parts(ticks_per_beat, tracks, conductor, as_written)
    return Source(midi_type, ticks_per_beat, parts, conductor, end)


def played_notes(data: bytes) -> tuple[tuple[int, int, int, int], ...]:
    """A piano version's notes as its file plays them: (start tick, end tick, pitch, hand), hand 0 right and 1 left,
    in order; pedal and velocities aside. Two levels with the same notes are the same piece to play."""
    source = _parse(data, as_written=True)
    notes = []
    for part in source.parts:
        # a part per track: the hand is its track's, named by write
        hand = 0 if part.name == RIGHT_TRACK else 1
        notes.extend((n.start, n.end, n.pitch, hand) for n in part.notes)
    return tuple(sorted(notes))


def _source(midi: mido.MidiFile) -> Source:
    parts, conductor = read(midi)
    return Source(midi.type, midi.ticks_per_beat, parts, conductor, song_end(midi))


def _varint(value: int) -> bytes:
    if value < 0:
        raise ValueError("variable int must be a non-negative integer")
    out = [value & 0x7F]
    value >>= 7
    while value:
        out.append(0x80 | (value & 0x7F))
        value >>= 7
    return bytes(reversed(out))


END_OF_TRACK = b"\xff\x2f\x00"


def _chunk(data: bytes | bytearray) -> bytes:
    return b"MTrk" + struct.pack(">L", len(data)) + bytes(data)


def _conductor_track(conductor: list[tuple[int, mido.MetaMessage]], end: int) -> bytes:
    """The conductor's events once each, then the end of the track at end: as mido writes them."""
    data = bytearray()
    previous = 0
    seen = set()
    with meta_charset("latin1"):
        for tick, message in sorted(conductor, key=lambda e: e[0]):
            # a type 1 file may repeat the conductor's events in other tracks
            identity = (tick, message.type, str(message.dict()))
            if identity in seen:
                continue
            seen.add(identity)
            data += _varint(tick - previous)
            data += bytes(message.copy(time=tick - previous).bytes())
            previous = tick
    data += _varint(max(0, end - previous)) + END_OF_TRACK
    return _chunk(data)


def _note_track(name: str, channel: int, notes: list[Note], pedal: list[tuple[int, int]], end: int) -> bytes:
    """A track of notes and pedal ending at end; a note on a key still sounding ends the earlier one there, as on a
    piano. Written as mido writes a track: running status for the channel's messages, none after a meta event."""
    if not 0 <= channel <= 15:
        raise ValueError("channel must be in range 0..15")
    # (tick, order at that tick, status, data): pedal up, note-offs, note-ons, pedal down, so a repeated key is
    # struck again
    note_on, note_off, control = 0x90 | channel, 0x80 | channel, 0xB0 | channel
    events: list[tuple[int, int, int, int, int]] = []
    ends: dict[int, int] = {}            # pitch -> index of its pending note_off in events
    for note in sorted(notes, key=lambda n: (n.start, n.pitch)):
        note_end = min(note.end, end)
        if note.start >= note_end:
            continue
        if not 0 <= note.pitch <= 127:
            raise ValueError("data byte must be in range 0..127")
        previous = ends.get(note.pitch)
        if previous is not None and events[previous][0] > note.start:
            events[previous] = (note.start, 1) + events[previous][2:]
        events.append((note.start, 2, note_on, note.pitch, max(1, min(127, note.velocity))))
        ends[note.pitch] = len(events)
        events.append((note_end, 1, note_off, note.pitch, 0))
    for down, up in pedal:
        up = min(up, end)
        if down < up:
            events.append((down, 3, control, SUSTAIN, 127))
            events.append((up, 0, control, SUSTAIN, 0))
    with meta_charset("latin1"):
        title = bytes(mido.MetaMessage("track_name", name=name).bytes())
    data = bytearray(b"\x00" + title + b"\x00" + bytes((0xC0 | channel, 0)))
    running = 0xC0 | channel
    previous_tick = 0
    # stable, as sorting by (tick, order) alone: events of one tick and order stay in the order made
    for tick, _, status, first, second in sorted(events, key=lambda e: (e[0], e[1])):
        if tick < previous_tick:
            raise ValueError("message time must be non-negative in MIDI file")
        data += _varint(tick - previous_tick)
        if status != running:
            data.append(status)
            running = status
        data.append(first)
        data.append(second)
        previous_tick = tick
    data += _varint(max(0, end - previous_tick)) + END_OF_TRACK
    return _chunk(data)


def _write_bytes(hands: Hands, ticks_per_beat: int, conductor_track: bytes, end: int) -> bytes:
    header = b"MThd" + struct.pack(">L", 6) + struct.pack(">hhh", 1, 3, ticks_per_beat)
    return b"".join((header, conductor_track,
                     _note_track(RIGHT_TRACK, RIGHT_CHANNEL, hands.right, hands.pedal, end),
                     _note_track(LEFT_TRACK, LEFT_CHANNEL, hands.left, hands.pedal, end)))


def write(hands: Hands, ticks_per_beat: int, conductor: list[tuple[int, mido.MetaMessage]], end: int) -> mido.MidiFile:
    return mido.MidiFile(file=io.BytesIO(_write_bytes(hands, ticks_per_beat, _conductor_track(conductor, end), end)))


def _meter(source: Source) -> Meter:
    return Meter(source.ticks_per_beat, [(tick, m.numerator, m.denominator) for tick, m in source.conductor
                                         if m.type == "time_signature"])


def _check_type(midi_type: int) -> None:
    if midi_type == 2:
        raise ValueError("MIDI files of type 2 (independent songs) have no single piano version")


def piano_version(midi: mido.MidiFile, options: Options = Options()) -> mido.MidiFile:
    _check_type(midi.type)
    source = _source(midi)
    hands = arrange(source.parts, source.ticks_per_beat, options, _meter(source))
    return write(hands, source.ticks_per_beat, source.conductor, source.end)


class TooManyNotes(ValueError):
    """A source with more notes than the caller allows (levels' max_notes)."""


def read_source(data: bytes) -> Source:
    """A MIDI file's bytes, read once for any number of levels. ValueError, EOFError, or OSError for a file that is
    not MIDI, or one of type 2."""
    source = _parse(data)
    _check_type(source.type)
    return source


def levels(data: bytes, names=None, max_notes: int | None = None, **overrides) -> Iterator[tuple[str, bytes]]:
    """(level, the piano version's bytes) for each of names (every level by default), in that order, from one read
    and one analysis. overrides are Options' own (chord_notes, right_hand_notes, left_hand_notes, pedal). With
    max_notes, TooManyNotes for a source of more notes, before any level is made. Memory: the source's notes once,
    their normalized copies, and one level's work at a time; the source's own notes are let go once copied."""
    resolved = [Options(level=name, **overrides).resolved() for name in (names or LEVELS)]
    source = read_source(data)
    if max_notes is not None and source.note_count > max_notes:
        raise TooManyNotes(f"{source.note_count} notes, more than {max_notes}")
    ticks_per_beat, end = source.ticks_per_beat, source.end
    meter = _meter(source)
    conductor_track = _conductor_track(source.conductor, end)
    song = _Song(source.parts, ticks_per_beat, meter)
    del source
    for level in resolved:
        yield level.name, _write_bytes(song.arrange(level), ticks_per_beat, conductor_track, end)


def all_levels(data: bytes, **overrides) -> dict[str, bytes]:
    """Every level's piano version of a MIDI file's bytes, by level."""
    return dict(levels(data, **overrides))


def convert(data: bytes, options: Options = Options()) -> bytes:
    """The piano version of a MIDI file's bytes, at options.level. ValueError, EOFError, or OSError for a file that is
    not MIDI (or a level that does not exist)."""
    overrides = {name: getattr(options, name) for name in ("chord_notes", "right_hand_notes", "left_hand_notes", "pedal")}
    return next(levels(data, [options.level], **overrides))[1]


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="The piano version of a multi-track MIDI file.")
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path, nargs="?", help="the piano version (or give --all-levels)")
    parser.add_argument("--level", choices=list(LEVELS), default=Options.level,
                        help="how much of the music, and how hard to play (default %(default)s)")
    parser.add_argument("--all-levels", type=Path, metavar="OUTPUT_DIR",
                        help="write every level into OUTPUT_DIR, as '<source name> - piano <level>.mid'")
    parser.add_argument("--chord-notes", type=int, help="the most accompaniment notes at one attack (the level's)")
    parser.add_argument("--right-hand-notes", type=int, help="the most notes the right hand holds at once")
    parser.add_argument("--left-hand-notes", type=int, help="the most notes the left hand holds at once")
    parser.add_argument("--pedal", action=argparse.BooleanOptionalAction, default=None,
                        help="sustain pedal at harmony changes (the full level's default)")
    args = parser.parse_args(argv)
    if (args.output is None) == (args.all_levels is None):
        parser.error("give either an output file or --all-levels OUTPUT_DIR")
    overrides = dict(chord_notes=args.chord_notes, right_hand_notes=args.right_hand_notes,
                     left_hand_notes=args.left_hand_notes, pedal=args.pedal)
    data = args.source.read_bytes()
    try:
        if args.output is not None:
            args.output.write_bytes(convert(data, Options(level=args.level, **overrides)))
            return
        args.all_levels.mkdir(parents=True, exist_ok=True)
        for level, midi in levels(data, **overrides):
            target = args.all_levels / f"{args.source.stem} - piano {level}.mid"
            target.write_bytes(midi)
            print(target)
    except ValueError as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
