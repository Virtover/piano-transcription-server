"""Writing an ensemble result as a multi-track MIDI file.

One MIDI track per dettato track, in its order (the largest first, the drums last), named after its instrument, with its
General MIDI program. Pitched tracks get channels 1-16 except 10 in turn (two tracks share a channel only beyond 15
pitched tracks); the drums play on channel 10. Times are written at 120 BPM, as a transcription's are, and the tempo
map (app/transcription/tempo.py) then puts them on the recording's beats without moving any note. No torch import.

A note on a key that is still sounding in the same track ends the earlier note: MIDI cannot hold two notes of one key on
one channel, and a player would otherwise end the wrong one.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import mido

TICKS_PER_BEAT = 960
TEMPO = 500_000                  # 120 BPM
DRUM_CHANNEL = 9
PITCHED_CHANNELS = tuple(c for c in range(16) if c != DRUM_CHANNEL)
MIN_TICKS = 1


@dataclass
class Track:
    name: str
    program: int
    is_drum: bool
    # (onset s, offset s, MIDI pitch, velocity 1-127)
    notes: list[tuple[float, float, int, int]] = field(default_factory=list)


def _ticks(seconds: float) -> int:
    return max(0, int(round(seconds * TICKS_PER_BEAT * 1_000_000 / TEMPO)))


def _events(notes, channel) -> list[tuple[int, int, mido.Message]]:
    """(tick, order, message): at one tick note-offs come before note-ons, so a repeated key restarts."""
    out = []
    ordered = sorted(notes, key=lambda n: (n[0], n[2]))
    sounding_until: dict[int, int] = {}
    pending_off: dict[int, int] = {}
    started: dict[int, int] = {}
    for onset, offset, pitch, velocity in ordered:
        pitch = int(pitch)
        if not 0 <= pitch <= 127:
            continue
        on, off = _ticks(onset), _ticks(offset)
        off = max(off, on + MIN_TICKS)
        if pitch in started and started[pitch] >= on:
            continue                      # a second start on the same tick: the same note
        started[pitch] = on
        if pitch in sounding_until and sounding_until[pitch] > on:
            # the earlier note on this key ends where this one starts
            out[pending_off[pitch]] = (on, 0, out[pending_off[pitch]][2])
        out.append((on, 1, mido.Message("note_on", channel=channel, note=pitch,
                                         velocity=max(1, min(127, int(velocity))))))
        pending_off[pitch] = len(out)
        out.append((off, 0, mido.Message("note_off", channel=channel, note=pitch, velocity=0)))
        sounding_until[pitch] = off
    return out


def build(tracks: list[Track]) -> mido.MidiFile:
    midi = mido.MidiFile(type=1, ticks_per_beat=TICKS_PER_BEAT)
    conductor = mido.MidiTrack()
    conductor.append(mido.MetaMessage("set_tempo", tempo=TEMPO, time=0))
    conductor.append(mido.MetaMessage("time_signature", numerator=4, denominator=4, time=0))
    midi.tracks.append(conductor)
    pitched = 0
    for track in tracks:
        if track.is_drum:
            channel = DRUM_CHANNEL
        else:
            channel = PITCHED_CHANNELS[pitched % len(PITCHED_CHANNELS)]
            pitched += 1
        mt = mido.MidiTrack()
        mt.append(mido.MetaMessage("track_name", name=track.name[:64], time=0))
        if not track.is_drum:
            mt.append(mido.Message("program_change", channel=channel, program=max(0, min(127, int(track.program))), time=0))
        previous = 0
        for tick, _, message in sorted(_events(track.notes, channel), key=lambda e: (e[0], e[1])):
            mt.append(message.copy(time=tick - previous))
            previous = tick
        midi.tracks.append(mt)
    return midi
