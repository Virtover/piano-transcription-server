"""Piano versions of ensemble results (app/transcription/ensemble/piano_levels.py): small MIDI files built in memory
with mido. The tests of the standalone script multitrack_to_piano.py the module came from, unchanged; then the golden
check that the module's output stays byte-identical to the script's, its shared reading of all levels, and how its
time grows with a recording's length.

The golden digests are of the script's own output on the files built here. With PIANO_LEVELS_REFERENCE set to the
script's path, the script itself is run on them too and compared byte for byte.
"""
from __future__ import annotations

import hashlib
import importlib.util
import io
import os
import random
import sys
import time
from pathlib import Path

import mido
import pytest

from app.transcription.ensemble import piano_levels as m2p

TPB = 480
REDUCTIONS = [name for name, level in m2p.LEVELS.items() if not level.all_notes]


# --- building and reading files ----------------------------------------------------------------------------------


def build(parts, tempos=((0, 500_000),), signatures=((0, 4, 4),), midi_type=1, tpb=TPB, extra=()) -> bytes:
    """A file of parts: (program, channel, [(start tick, length in ticks, pitch, velocity)]). extra: per part index,
    further (tick, message) events such as pedal."""
    midi = mido.MidiFile(type=midi_type, ticks_per_beat=tpb)
    conductor = [(t, mido.MetaMessage("set_tempo", tempo=v)) for t, v in tempos]
    conductor += [(t, mido.MetaMessage("time_signature", numerator=n, denominator=d)) for t, n, d in signatures]
    tracks = [conductor]
    for index, (program, channel, notes) in enumerate(parts):
        events = [(0, mido.Message("program_change", channel=channel, program=program))]
        for start, length, pitch, velocity in notes:
            events.append((start, mido.Message("note_on", channel=channel, note=pitch, velocity=velocity)))
            events.append((start + length, mido.Message("note_off", channel=channel, note=pitch, velocity=0)))
        events += [e for i, e in extra if i == index]
        tracks.append(events)
    if midi_type == 0:
        tracks = [[e for track in tracks for e in track]]
    for events in tracks:
        track = mido.MidiTrack()
        previous = 0
        # note-offs before note-ons at one tick
        for tick, message in sorted(events, key=lambda e: (e[0], e[1].type == "note_on" and e[1].velocity > 0)):
            track.append(message.copy(time=tick - previous))
            previous = tick
        midi.tracks.append(track)
    out = io.BytesIO()
    midi.save(file=out)
    return out.getvalue()


def load(data: bytes) -> mido.MidiFile:
    return mido.MidiFile(file=io.BytesIO(data))


def hand_notes(data: bytes) -> dict[int, list[tuple[int, int, int, int]]]:
    """Per channel: (start, end, pitch, velocity), first in first out per key."""
    out: dict[int, list] = {0: [], 1: []}
    for track in load(data).tracks:
        tick, sounding = 0, {}
        for m in track:
            tick += m.time
            if m.type == "note_on" and m.velocity > 0:
                sounding.setdefault((m.channel, m.note), []).append((tick, m.velocity))
            elif m.type in ("note_on", "note_off") and sounding.get((m.channel, m.note)):
                start, velocity = sounding[(m.channel, m.note)].pop(0)
                out.setdefault(m.channel, []).append((start, tick, m.note, velocity))
    return {c: sorted(ns) for c, ns in out.items()}


def conductor_of(data: bytes) -> list[tuple[int, str, tuple]]:
    out = []
    for track in load(data).tracks:
        tick = 0
        for m in track:
            tick += m.time
            if m.type == "set_tempo":
                out.append((tick, m.type, (m.tempo,)))
            elif m.type == "time_signature":
                out.append((tick, m.type, (m.numerator, m.denominator)))
    return sorted(out)


def max_at_once(notes) -> int:
    best = 0
    for start, *_ in notes:
        best = max(best, sum(1 for s, e, *_ in notes if s <= start < e))
    return best


def sounding_at(notes, tick):
    return [n for n in notes if n[0] <= tick < n[1]]


def convert(data: bytes, level: str, **overrides) -> bytes:
    return m2p.convert(data, m2p.Options(level=level, **overrides))


# --- a busy band arrangement -------------------------------------------------------------------------------------


def band() -> bytes:
    """Eight bars of 4/4 with a tempo change: a voice in sixteenths with wide leaps, an electric bass in eighths, a
    guitar strumming four-note chords in sixteenths, a pad holding chords, and drums."""
    bar = 4 * TPB
    progression = [(48, [60, 64, 67]), (45, [57, 60, 64]), (41, [53, 57, 60]), (43, [55, 59, 62])] * 2
    voice, bass, guitar, pad, drums = [], [], [], [], []
    tune = [72, 74, 76, 79, 88, 74, 67, 71, 84, 60, 76, 72, 71, 69, 67, 65]
    for b, (root, chord) in enumerate(progression):
        t0 = b * bar
        for i in range(16):
            voice.append((t0 + i * TPB // 4, TPB // 4, tune[(i + b) % 16], 90))
            guitar.append((t0 + i * TPB // 4 + 3, TPB // 4 - 10, chord[0] - 12 + 12 * (i % 2), 70))
            for p in chord:
                guitar.append((t0 + i * TPB // 4 + 3, TPB // 4 - 10, p, 70))
        for i in range(8):
            bass.append((t0 + i * TPB // 2, TPB // 2, root - 12 + 12 * (i % 2 == 1 and i % 4 == 3), 85))
        for p in chord:
            pad.append((t0, bar, p + 12, 50))
        for i in range(8):
            drums.append((t0 + i * TPB // 2 + 7, 30, 36 + i % 3, 100))
    return build([(52, 0, voice), (33, 1, bass), (25, 2, guitar), (89, 3, pad), (0, 9, drums)],
                 tempos=((0, 500_000), (4 * bar, 400_000), (6 * bar + TPB, 600_000)))


@pytest.fixture(scope="module")
def band_data() -> bytes:
    return band()


@pytest.fixture(scope="module")
def band_outputs(band_data) -> dict[str, bytes]:
    return {name: convert(band_data, name) for name in m2p.LEVELS}


@pytest.mark.parametrize("name", REDUCTIONS)
def test_hand_limits(band_outputs, name):
    level = m2p.LEVELS[name]
    hands = hand_notes(band_outputs[name])
    right, left = hands[0], hands[1]
    assert right and left
    assert max_at_once(right) <= level.right_hand_notes
    assert max_at_once(left) <= level.left_hand_notes
    for notes in (right, left):
        for start, *_ in notes:
            pitches = [p for _, _, p, _ in sounding_at(notes, start)]
            assert max(pitches) - min(pitches) <= level.hand_span


@pytest.mark.parametrize("name", REDUCTIONS)
def test_attack_density(band_outputs, name):
    gap = round(m2p.LEVELS[name].chord_gap * TPB)
    for notes in hand_notes(band_outputs[name]).values():
        ticks = sorted({s for s, *_ in notes})
        assert all(b - a >= gap for a, b in zip(ticks, ticks[1:])), name


@pytest.mark.parametrize("name", REDUCTIONS)
def test_hands_do_not_cross(band_outputs, name):
    hands = hand_notes(band_outputs[name])
    for start in sorted({s for notes in hands.values() for s, *_ in notes}):
        right = [p for _, _, p, _ in sounding_at(hands[0], start)]
        left = [p for _, _, p, _ in sounding_at(hands[1], start)]
        if right and left:
            assert max(left) < min(right)


def test_beginner_lines(band_outputs):
    level = m2p.LEVELS["beginner"]
    hands = hand_notes(band_outputs["beginner"])
    right, left = hands[0], hands[1]
    assert max_at_once(right) == 1 and max_at_once(left) == 1
    assert all(level.melody_range[0] <= p <= level.melody_range[1] for _, _, p, _ in right)
    assert all(level.bass_range[0] <= p <= level.bass_range[1] for _, _, p, _ in left)
    # no leap wider than the level's where the range has a nearer octave; no key struck again within a beat
    pitches = [p for _, _, p, _ in right]
    low, high = level.melody_range
    for a, b in zip(pitches, pitches[1:]):
        nearest = min((q for q in range(low, high + 1) if q % 12 == b % 12), key=lambda q: abs(q - a))
        assert abs(b - a) <= level.max_leap or abs(b - a) == abs(nearest - a)
    for notes in (right, left):
        assert all(b[0] - a[0] >= TPB for a, b in zip(notes, notes[1:]))
    # the bass only every half bar at most, and long
    assert all(b[0] - a[0] >= 2 * TPB for a, b in zip(left, left[1:]))
    assert min(e - s for s, e, _, _ in left[:-1]) >= TPB


def test_easy_chords(band_outputs):
    hands = hand_notes(band_outputs["easy"])
    assert max_at_once(hands[0]) == 2          # the melody and a chord note somewhere
    level = m2p.LEVELS["easy"]
    assert all(level.melody_range[0] - 12 <= p <= level.melody_range[1] for _, _, p, _ in hands[0])


def test_levels_grow(band_outputs):
    counts = [sum(len(n) for n in hand_notes(band_outputs[name]).values()) for name in REDUCTIONS]
    assert counts == sorted(counts) and counts[0] < counts[-1]


def test_melody_is_the_voice(band_data, band_outputs):
    """The right hand's top line follows the voice's rhythm at intermediate (sixteenths)."""
    right = hand_notes(band_outputs["intermediate"])[0]
    voice_starts = {s for s in range(0, 8 * 4 * TPB, TPB // 4)}
    tops = {}
    for s, e, p, v in right:
        tops[s] = max(tops.get(s, 0), p)
    on_voice = [s for s in tops if s in voice_starts]
    assert len(on_voice) >= 0.9 * len(voice_starts)


@pytest.mark.parametrize("name", list(m2p.LEVELS))
def test_timing_and_tempo_map(band_data, band_outputs, name):
    source = load(band_data)
    source_starts = {s for c, notes in source_notes(band_data).items() if c != 9 for s, *_ in notes}
    out = band_outputs[name]
    assert conductor_of(out) == conductor_of(band_data)
    assert load(out).length == pytest.approx(source.length, abs=1e-9)
    for notes in hand_notes(out).values():
        assert {s for s, *_ in notes} <= source_starts


def source_notes(data):
    out: dict[int, list] = {}
    for track in load(data).tracks:
        tick, sounding = 0, {}
        for m in track:
            tick += m.time
            if m.type == "note_on" and m.velocity > 0:
                sounding.setdefault((m.channel, m.note), []).append(tick)
            elif m.type in ("note_on", "note_off") and sounding.get((m.channel, m.note)):
                out.setdefault(m.channel, []).append((sounding[(m.channel, m.note)].pop(0), tick, m.note))
    return out


@pytest.mark.parametrize("name", list(m2p.LEVELS))
def test_drums_left_out(band_outputs, name):
    # the drums start 7 ticks after the eighths; nothing else does
    for notes in hand_notes(band_outputs[name]).values():
        assert all(s % (TPB // 2) != 7 for s, *_ in notes)


def test_only_drums():
    data = build([(0, 9, [(i * TPB, 50, 36, 100) for i in range(8)])])
    for name in m2p.LEVELS:
        out = convert(data, name)
        assert hand_notes(out) == {0: [], 1: []}
        assert load(out).length == pytest.approx(load(data).length)


@pytest.mark.parametrize("name", list(m2p.LEVELS))
def test_deterministic(band_data, band_outputs, name):
    assert convert(band_data, name) == band_outputs[name]


# --- full -------------------------------------------------------------------------------------------------------


def test_full_keeps_every_note():
    flute = [(i * TPB, TPB, 72 + (i % 5), 80) for i in range(16)]
    strings = [(i * 2 * TPB, 2 * TPB, p, 60) for i in range(8) for p in (60, 64, 67)]
    cello = [(i * TPB, TPB, 36 + (i % 4) * 2, 70) for i in range(16)]
    high = [(i * 4 * TPB, TPB, 115, 60) for i in range(4)]        # above the keyboard: folded to 103
    data = build([(73, 0, flute), (48, 1, strings), (42, 2, cello), (11, 3, high)])
    out = hand_notes(convert(data, "full"))
    played = {(s, p) for notes in out.values() for s, _, p, _ in notes}
    for program, channel, notes in [(73, 0, flute), (48, 1, strings), (42, 2, cello), (11, 3, high)]:
        for start, _, pitch, _ in notes:
            assert (start, m2p._fold(pitch, m2p.LOWEST, m2p.HIGHEST)) in played
    assert all(m2p.LOWEST <= p <= m2p.HIGHEST for notes in out.values() for _, _, p, _ in notes)


def test_full_merges_unisons_and_restrikes():
    # a guitar and a piano strike the same C at once; a pad holds E while the voice strikes E again
    data = build([(24, 0, [(0, TPB, 60, 50)]), (0, 1, [(5, TPB, 60, 100)]),
                  (89, 2, [(0, 4 * TPB, 64, 60)]), (52, 3, [(2 * TPB, TPB, 64, 90)])])
    notes = [n for ns in hand_notes(convert(data, "full")).values() for n in ns]
    cs = [n for n in notes if n[2] == 60]
    assert len(cs) == 1 and cs[0][0] == 0
    es = sorted(n for n in notes if n[2] == 64)
    assert [s for s, *_ in es] == [0, 2 * TPB] and es[0][1] == 2 * TPB      # the pad's E ends where the voice's starts
    # no key sounds twice at once, across both hands
    for a in notes:
        for b in notes:
            if a is not b and a[2] == b[2]:
                assert a[1] <= b[0] or b[1] <= a[0]


def test_full_balance_and_mud():
    # the voice over a loud pad; a cello's low E1 against a bass's low D1
    voice = [(i * TPB, TPB, 76, 70) for i in range(8)]
    pad = [(0, 8 * TPB, p, 110) for p in (60, 64, 67)]
    bass = [(0, 8 * TPB, 26, 90)]
    cello = [(0, 8 * TPB, 28, 80)]
    out = hand_notes(convert(build([(52, 0, voice), (89, 1, pad), (33, 2, bass), (42, 3, cello)]), "full"))
    notes = out[0] + out[1]
    melody = [v for s, e, p, v in notes if p == 76]
    pads = [v for s, e, p, v in notes if p in (60, 64, 67)]
    assert min(melody) > max(pads)
    assert any(p == 26 for _, _, p, _ in notes)                # the bass stays
    assert not any(p == 28 for _, _, p, _ in notes)            # the cello's E goes up, out of the mud
    assert any(p % 12 == 4 and p > 28 for _, _, p, _ in notes)
    # the melody in the right hand, the bass in the left
    assert all(p != 26 for _, _, p, _ in out[0]) and all(p != 76 for _, _, p, _ in out[1])


def test_full_pedal():
    # a bar of C, a bar of F, a bar of G, in detached chords
    chords = [(60, 64, 67), (65, 69, 72), (67, 71, 74)]
    notes = [(b * 4 * TPB + i * TPB, TPB // 3, p, 70) for b, chord in enumerate(chords) for i in range(4) for p in chord]
    bass = [(b * 4 * TPB, TPB // 2, chord[0] - 24, 80) for b, chord in enumerate(chords)]
    out = convert(build([(0, 0, notes), (33, 1, bass)]), "full")
    for channel in (0, 1):
        events = []
        for track in load(out).tracks:
            tick = 0
            for m in track:
                tick += m.time
                if m.type == "control_change" and m.control == 64 and m.channel == channel:
                    events.append((tick, m.value))
        downs = [t for t, v in events if v == 127]
        ups = [t for t, v in events if v == 0]
        assert len(downs) == 3 and len(ups) == 3
        # each released before the next harmony, pressed just after it begins
        for bar in (1, 2):
            assert ups[bar - 1] < bar * 4 * TPB < downs[bar]
    assert not any(m.type == "control_change" for t in load(convert(build([(0, 0, notes)]), "beginner")).tracks
                   for m in t)


def test_full_clamps_huge_attacks():
    cluster = [(0, TPB, p, 60 + i) for i, p in enumerate(range(40, 90, 2))]      # 25 keys at once
    notes = [n for ns in hand_notes(convert(build([(48, 0, cluster)]), "full")).values() for n in ns]
    pitches = sorted(p for _, _, p, _ in notes)
    assert len(pitches) == m2p.FULL_MAX_KEYS
    assert pitches[0] == 40 and pitches[-1] == 88
    assert len({p % 12 for p in pitches}) == 6                 # every pitch class of the cluster still there


# --- a lone piano ------------------------------------------------------------------------------------------------


def piano_solo() -> bytes:
    """A right hand in eighths over an Alberti left hand, on one track."""
    notes = []
    for b in range(4):
        for i in range(8):
            notes.append((b * 4 * TPB + i * TPB // 2, TPB // 2, 72 + (i * 3 + b) % 8, 80))
            notes.append((b * 4 * TPB + i * TPB // 2, TPB // 2, [48, 55, 52, 55][i % 4], 60))
        notes.append((b * 4 * TPB, 4 * TPB, 36, 70))
    return build([(0, 0, notes)])


@pytest.mark.parametrize("name", REDUCTIONS)
def test_piano_solo_split(name):
    level = m2p.LEVELS[name]
    hands = hand_notes(convert(piano_solo(), name))
    right, left = hands[0], hands[1]
    assert right and left
    assert max_at_once(right) <= level.right_hand_notes and max_at_once(left) <= level.left_hand_notes
    assert min(p for _, _, p, _ in right) > max(p for _, _, p, _ in left) or name != "beginner"
    # the tune is the right hand's top line, the bass the left's bottom
    assert max(p for _, _, p, _ in right) >= level.melody_center[1] - 12
    assert min(p for _, _, p, _ in left) <= 48


def test_piano_solo_full_keeps_all():
    data = piano_solo()
    source = {(s, p) for s, _, p in source_notes(data)[0]}
    out = {(s, p) for ns in hand_notes(convert(data, "full")).values() for s, _, p, _ in ns}
    # the low C and the Alberti's C3 sound together: the mud rule lifts nothing a fifth or more apart
    assert source == out


# --- edge cases --------------------------------------------------------------------------------------------------


def test_empty_file():
    midi = mido.MidiFile(type=1, ticks_per_beat=TPB)
    midi.tracks.append(mido.MidiTrack())
    out = io.BytesIO()
    midi.save(file=out)
    for name in m2p.LEVELS:
        result = load(convert(out.getvalue(), name))
        assert hand_notes(convert(out.getvalue(), name)) == {0: [], 1: []}
        assert result.length == 0


def test_type_0_file(band_data):
    parts = [(52, 0, [(i * TPB, TPB, 72 + i % 3, 80) for i in range(8)]),
             (33, 1, [(i * 2 * TPB, 2 * TPB, 36, 80) for i in range(4)]),
             (0, 9, [(i * TPB, 30, 38, 100) for i in range(8)])]
    one, zero = build(parts, midi_type=1), build(parts, midi_type=0)
    for name in m2p.LEVELS:
        assert hand_notes(convert(zero, name)) == hand_notes(convert(one, name))


def test_type_2_refused():
    midi = mido.MidiFile(type=2, ticks_per_beat=TPB)
    midi.tracks.append(mido.MidiTrack())
    out = io.BytesIO()
    midi.save(file=out)
    with pytest.raises(ValueError):
        convert(out.getvalue(), "easy")


def test_unknown_level():
    with pytest.raises(ValueError):
        convert(band(), "virtuoso")


def test_overlapping_same_notes_zero_length_and_out_of_range():
    notes = [(0, 2 * TPB, 72, 80), (TPB, 2 * TPB, 72, 80),        # the same key struck again while it sounds
             (4 * TPB, 0, 74, 80),                                 # a note of no length
             (5 * TPB, TPB, 10, 80), (6 * TPB, TPB, 125, 80)]     # below and above the keyboard
    data = build([(0, 0, notes)])
    for name in m2p.LEVELS:
        out = hand_notes(convert(data, name))
        all_notes = out[0] + out[1]
        assert all(m2p.LOWEST <= p <= m2p.HIGHEST for _, _, p, _ in all_notes)
        assert all(e > s for s, e, _, _ in all_notes)
        assert load(convert(data, name)).length == pytest.approx(load(data).length)
    full = {(s, p) for ns in hand_notes(convert(data, "full")).values() for s, _, p, _ in ns}
    assert {(0, 72), (TPB, 72), (4 * TPB, 74), (5 * TPB, 22), (6 * TPB, 101)} <= full


def test_source_pedal_lengthens_notes():
    notes = [(0, 100, 60, 80), (TPB, 100, 64, 80)]
    pedal = [(0, (10, mido.Message("control_change", channel=0, control=64, value=127))),
             (0, (3 * TPB, mido.Message("control_change", channel=0, control=64, value=0)))]
    parts, _ = m2p.read(load(build([(0, 0, notes)], extra=pedal)))
    assert sorted((n.start, n.end) for n in parts[0].notes) == [(0, 3 * TPB), (TPB, 3 * TPB)]


def test_tempo_changes_and_meter():
    data = build([(52, 0, [(i * TPB, TPB, 72, 80) for i in range(12)])],
                 tempos=((0, 400_000), (3 * TPB, 800_000), (7 * TPB, 300_000)), signatures=((0, 3, 4), (6 * TPB, 6, 8)))
    for name in m2p.LEVELS:
        out = convert(data, name)
        assert conductor_of(out) == conductor_of(data)
        assert load(out).length == pytest.approx(load(data).length)
    meter = m2p.Meter(TPB, [(0, 3, 4), (6 * TPB, 6, 8)])
    assert [meter.strength(t) for t in (0, TPB, 3 * TPB, 3 * TPB + TPB // 2)] == [4, 2, 4, 1]
    assert [meter.strength(6 * TPB + t) for t in (0, 3 * TPB // 2, TPB // 2)] == [4, 3, 2]


def test_overrides():
    out = hand_notes(convert(band(), "intermediate", chord_notes=1, right_hand_notes=2, left_hand_notes=1))
    assert max_at_once(out[0]) <= 2 and max_at_once(out[1]) <= 1
    with pytest.raises(ValueError):
        m2p.Options(right_hand_notes=0).resolved()
    assert any(m.type == "control_change" for t in load(convert(band(), "easy", pedal=True)).tracks for m in t)


def test_command_line(tmp_path, band_data):
    source = tmp_path / "song - ensemble.mid"
    source.write_bytes(band_data)
    m2p.main([str(source), str(tmp_path / "one.mid"), "--level", "easy"])
    assert (tmp_path / "one.mid").read_bytes() == convert(band_data, "easy")
    m2p.main([str(source), "--all-levels", str(tmp_path / "all")])
    for name in m2p.LEVELS:
        assert (tmp_path / "all" / f"song - ensemble - piano {name}.mid").read_bytes() == convert(band_data, name)
    with pytest.raises(SystemExit):
        m2p.main([str(source)])


# --- the same bytes as the script --------------------------------------------------------------------------------


def song(seed: int, bars: int = 24, bass_part: bool = True, midi_type: int = 1) -> bytes:
    """A band of random but repeatable parts: a lead with leaps and specks, a bass part or none (then a low guitar is
    the bass), strummed chords, a pad, a string section, keys under the sustain pedal, drums, and tempo and meter
    changes. Only random() is drawn, whose values Python keeps across versions."""
    rng = random.Random(seed)
    bar = 4 * TPB
    roots = [48 + int(rng.random() * 12) for _ in range(bars)]
    lead, bass, guitar, pad, strings, keys, drums = [], [], [], [], [], [], []
    for b, root in enumerate(roots):
        t0 = b * bar
        third = 3 if rng.random() < 0.4 else 4
        chord = [root, root + third, root + 7]
        step = TPB // (4 if rng.random() < 0.5 else 2)
        for t in range(0, bar, step):
            if rng.random() < 0.8:
                pitch = 60 + int(rng.random() * 30)
                length = step if rng.random() < 0.9 else 7          # now and then a speck
                lead.append((t0 + t + int(rng.random() * 9), length, pitch, 50 + int(rng.random() * 70)))
        for t in range(0, bar, TPB // 2):
            bass.append((t0 + t, TPB // 2 - int(rng.random() * 40), root - 24 + 12 * (rng.random() < 0.2), 90))
        for t in range(0, bar, TPB // 4):
            for p in chord:
                guitar.append((t0 + t + 3, TPB // 4 - 10, p, 60 + int(rng.random() * 30)))
        for p in chord:
            pad.append((t0, bar + int(rng.random() * TPB), p + 12, 40 + int(rng.random() * 40)))
        strings.append((t0, 2 * TPB, chord[1] + 24, 70))
        strings.append((t0 + 2 * TPB, 2 * TPB, chord[2] + 24, 70))
        keys.append((t0, TPB // 3, root + 12, 64))
        for t in range(0, bar, TPB // 2):
            drums.append((t0 + t + 7, 30, 36 + int(rng.random() * 10), 100))
    pedal = [(4, (b * bar, mido.Message("control_change", channel=4, control=64, value=127))) for b in range(0, bars, 2)]
    pedal += [(4, (b * bar + bar - 20, mido.Message("control_change", channel=4, control=64, value=0)))
              for b in range(0, bars, 2)]
    low = (33, 1, bass) if bass_part else (25, 1, [(s, n, p - 12, v) for s, n, p, v in bass])
    parts = [(54, 0, lead), low, (27, 2, guitar), (89, 3, pad), (0, 4, keys), (48, 5, strings), (0, 9, drums)]
    return build(parts, tempos=((0, 500_000), (8 * bar, 420_000), (16 * bar + TPB, 650_000)),
                 signatures=((0, 4, 4), (12 * bar, 3, 4), (12 * bar + 6 * TPB, 6, 8)), midi_type=midi_type,
                 extra=pedal)


GOLDEN_SOURCES = {
    "band": band,
    "piano solo": piano_solo,
    "song 1": lambda: song(1),
    "song 2 without a bass part": lambda: song(2, bass_part=False),
    "song 3 of type 0": lambda: song(3, midi_type=0),
}
# sha256 of multitrack_to_piano.py's own output (the user's version of 2026-11-27), per source and level.
GOLDEN: dict[str, dict[str, str]] = {
    'band': {
        'beginner': '626ecb9f565abf9e38ee6a58593643cb621d30d5b954df8d3c892fe46a399061',
        'easy': 'fb7cf261d0c2848a8a3b4e854bc8fcdd0c4fef7c96027a796999d6bb034c454a',
        'intermediate': '2891868ef085c832ca711918d17a3e19e219abcc978939ec3dfdb215a6413375',
        'advanced': 'ddc523ffbc6383f3aeb3f1448bc26dac2bc4c8249f860046e33595d5d959720e',
        'full': '04e365498e94997c8d528a89419fce51a8d7e7f643ff88045b857def361cc7c6',
    },
    'piano solo': {
        'beginner': '4ca82ee3a896a4a7df2bfdbe4f488aab3dedfeaa8f735afe65a0b883036d2a35',
        'easy': '2e4f3f4091aa18e1bba92db4e9c50430f617611e1c8c0174fd3f31d1605dcdfd',
        'intermediate': 'f75a9cef16ed7f68679622ead14d93105486a76d373bb3ff520bdc71baec2ce3',
        'advanced': '9f76f7655983dd7fd5357d9a445f50a2385ae2f0da7e43e6813de40b49ad1357',
        'full': '246d85cef85f923366b4a143a2ac3a047d33bdd34cf36847708e0f3861c41ba2',
    },
    'song 1': {
        'beginner': 'cbda5a014d98936c7c1a0e1f5a037260e65e26dcb9de511bcf03f3218626e332',
        'easy': 'b6332683ede09fc15e85b0a0ad0a4950208c7ab3be3a21d5f5d99ffa15b57eba',
        'intermediate': 'f09ad238028c4824331b600e8251235bf34e3bd8c2d3d02508c6fda61d3e02a7',
        'advanced': 'cb260bfda63ef668bc03027dd6067e16272196e61b7b1d1128f648f497f659fe',
        'full': 'e9653abaf2a8a1b4c674350dd8c6587e629aa1b693f7fea0660d38a919c413f8',
    },
    'song 2 without a bass part': {
        'beginner': '62350631c2223bd1ace6e527ae15ed7af6d3d35aa48eeb4fd31e1ec94a57fd0c',
        'easy': '8699a5ad4c75e2e075779f349940bf4c0847b02163f611f2d65e353dc7d6f373',
        'intermediate': '1a374dc528bec61a3c1cc537ccd012a874a48842d8f1c3ef4b00eeb43af78e37',
        'advanced': '0786a4b3671e1dbc966cb6ff5e0f0523af330d6b0f7896e490419c9f5c480adb',
        'full': '5665d90d4ca897f5a3ccc1c5297b084230b2b9af5c36c31608583881ff9b567c',
    },
    'song 3 of type 0': {
        'beginner': '0864238102411f03bff540cae796de9c494008620abf0416bd138a65a1d496cb',
        'easy': '28953057cfdc3e9839c3eddad3da402a7361944cdbcf917790260289629501d8',
        'intermediate': '2c2613b8c940be4949f893a2ce46ef34e52885764f73ad4df9708eb048706a94',
        'advanced': 'c17fa14a8a9f48fce867bea5f55795bd11ece18b230487cb63199725c1277567',
        'full': '0092b7a262e47bfc32d80777438cc960a4753b9a666380d49cf84a1216c6f987',
    },
}


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.mark.parametrize("source", list(GOLDEN_SOURCES))
def test_every_level_is_byte_identical_to_the_script(source):
    data = GOLDEN_SOURCES[source]()
    made = m2p.all_levels(data)
    assert list(made) == list(m2p.LEVELS)
    assert {level: digest(midi) for level, midi in made.items()} == GOLDEN[source]
    # each level alone, as the script makes it: the same bytes as all of them together
    for level in m2p.LEVELS:
        assert convert(data, level) == made[level]


@pytest.mark.skipif(not os.environ.get("PIANO_LEVELS_REFERENCE"), reason="PIANO_LEVELS_REFERENCE is not set")
@pytest.mark.parametrize("source", list(GOLDEN_SOURCES))
def test_the_reference_script_gives_the_same_bytes(source):
    spec = importlib.util.spec_from_file_location("multitrack_to_piano_reference", os.environ["PIANO_LEVELS_REFERENCE"])
    reference = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = reference
    spec.loader.exec_module(reference)
    data = GOLDEN_SOURCES[source]()
    made = m2p.all_levels(data)
    for level in m2p.LEVELS:
        assert reference.convert(data, reference.Options(level=level)) == made[level], level


def test_the_fast_reader_reads_as_mido_does():
    for make in GOLDEN_SOURCES.values():
        data = make()
        fast = m2p.read_source(data)
        parts, conductor = m2p.read(mido.MidiFile(file=io.BytesIO(data), clip=True))
        assert fast.parts == parts
        assert [(tick, m.dict()) for tick, m in fast.conductor] == [(tick, m.dict()) for tick, m in conductor]
        assert fast.end == m2p.song_end(load(data))


def test_running_status_clipped_bytes_and_sysex_read_as_mido_does():
    # note-ons by running status, a velocity byte over 127 (clipped), a sysex, and a text event between
    track = (b"\x00\xc0\x34" + b"\x00\x90\x3c\x50" + b"\x10\x3e\x50" + b"\x10\x80\x3c\x00" + b"\x00\x3e\x00"
             + b"\x00\xf0\x03\x01\x02\xf7" + b"\x00\xff\x01\x02hi" + b"\x08\x90\x40\xff" + b"\x60\x40\x00"
             + b"\x00\xff\x2f\x00")
    data = b"MThd\x00\x00\x00\x06\x00\x00\x00\x01\x01\xe0" + b"MTrk" + len(track).to_bytes(4, "big") + track
    parts, _ = m2p.read(mido.MidiFile(file=io.BytesIO(data), clip=True))
    assert m2p.read_source(data).parts == parts
    assert [(n.start, n.end, n.pitch, n.velocity) for n in parts[0].notes] == [(0, 32, 60, 80), (16, 46, 62, 80),
                                                                              (40, 136, 64, 127)]


@pytest.mark.parametrize("data", [b"", b"MThd", b"RIFF\x00\x00\x00\x06\x00\x01\x00\x01\x01\xe0",
                                  b"MThd\x00\x00\x00\x06\x00\x01\x00\x01\x01\xe0MTrk\x00\x00\x00\x09\x00\x90\x3c"])
def test_what_is_not_midi_is_refused(data):
    with pytest.raises((ValueError, EOFError, OSError)):
        m2p.all_levels(data)


def test_a_note_cap_refuses_before_any_level_is_made():
    with pytest.raises(m2p.TooManyNotes):
        next(m2p.levels(band(), max_notes=10))


def test_time_grows_linearly_with_the_length():
    """Four times the music takes about four times as long, not sixteen: no step is quadratic in the notes."""
    def seconds(bars: int) -> float:
        data = song(7, bars=bars)
        best = float("inf")
        for _ in range(2):
            started = time.perf_counter()
            m2p.all_levels(data)
            best = min(best, time.perf_counter() - started)
        return best

    short, long = seconds(24), seconds(96)
    assert long < 7 * short, (short, long)
