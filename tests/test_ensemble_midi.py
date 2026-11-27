"""The MIDI file of an ensemble result (app/transcription/ensemble/midi.py):
channels, programs, names, notes on one key, and the tempo map over it."""

from collections import defaultdict

import mido
import pytest

from app.transcription.ensemble import midi as ensemble_midi
from app.transcription.ensemble.midi import DRUM_CHANNEL, PITCHED_CHANNELS, TICKS_PER_BEAT, Track, build
from app.transcription.tempo import apply_tempo_map, build_tempo_map


def messages(track, *types):
    return [message for message in track if message.type in types]


def absolute(track):
    """(tick, message) of a mido track."""
    tick = 0
    for message in track:
        tick += message.time
        yield tick, message


def note_events(track):
    """(tick, type, key) of a track's note-ons and note-offs."""
    return [(tick, message.type, message.note) for tick, message in absolute(track)
            if message.type in ("note_on", "note_off")]


def seconds(ticks):
    return ticks * ensemble_midi.TEMPO / 1_000_000 / TICKS_PER_BEAT


def pitched(name, program, notes):
    return Track(name, program, False, notes)


# --- The file's layout -------------------------------------------------------------------------------

def test_the_conductor_track_comes_first_with_the_tempo_and_the_meter():
    result = build([pitched("Piano", 0, [(0.0, 1.0, 60, 80)])])

    assert result.type == 1 and result.ticks_per_beat == TICKS_PER_BEAT
    conductor = result.tracks[0]
    tempo = messages(conductor, "set_tempo")
    assert [message.tempo for message in tempo] == [500_000] == [ensemble_midi.TEMPO]
    [meter] = messages(conductor, "time_signature")
    assert (meter.numerator, meter.denominator) == (4, 4)
    assert messages(conductor, "note_on") == []
    assert len(result.tracks) == 2


def test_no_tracks_give_only_the_conductor():
    assert len(build([]).tracks) == 1


def test_every_track_has_its_name_program_and_channel():
    tracks = [
        pitched("Violin", 40, [(0.0, 1.0, 70, 90)]),
        pitched("Bass", 33, [(0.5, 1.5, 40, 100)]),
        Track("drums", 0, True, [(0.0, 0.1, 36, 100)]),
    ]

    result = build(tracks)

    assert len(result.tracks) == 4
    names = [messages(track, "track_name")[0].name for track in result.tracks[1:]]
    assert names == ["Violin", "Bass", "drums"]
    programs = [[(m.channel, m.program) for m in messages(track, "program_change")] for track in result.tracks[1:]]
    # Pitched tracks take channels in turn; the drums have no program change.
    assert programs == [[(0, 40)], [(1, 33)], []]
    channels = [{m.channel for m in messages(track, "note_on", "note_off")} for track in result.tracks[1:]]
    assert channels == [{0}, {1}, {DRUM_CHANNEL}]


def test_pitched_channels_skip_the_drum_channel_and_wrap_after_fifteen():
    tracks = [pitched(f"T{index}", index, [(0.0, 1.0, 60, 80)]) for index in range(17)]

    result = build(tracks)

    channels = [messages(track, "note_on")[0].channel for track in result.tracks[1:]]
    assert DRUM_CHANNEL not in channels
    assert channels[:15] == list(PITCHED_CHANNELS) == [0, 1, 2, 3, 4, 5, 6, 7, 8, 10, 11, 12, 13, 14, 15]
    # Only beyond fifteen pitched tracks do two share a channel.
    assert channels[15:] == [0, 1]
    assert len(set(channels[:15])) == 15


def test_drums_use_channel_ten_whatever_comes_before_them():
    tracks = [Track("drums", 0, True, [(0.0, 0.1, 38, 100)]), pitched("Piano", 0, [(0.0, 1.0, 60, 80)])]

    result = build(tracks)

    assert {m.channel for m in messages(result.tracks[1], "note_on")} == {9}
    # A drum track does not use up a pitched channel.
    assert {m.channel for m in messages(result.tracks[2], "note_on")} == {0}


def test_programs_and_velocities_are_kept_in_range_and_names_are_cut():
    result = build([
        pitched("x" * 100, 300, [(0.0, 1.0, 60, 500)]),
        pitched("low", -5, [(0.0, 1.0, 60, 0)]),
    ])

    assert len(messages(result.tracks[1], "track_name")[0].name) == 64
    assert messages(result.tracks[1], "program_change")[0].program == 127
    assert messages(result.tracks[1], "note_on")[0].velocity == 127
    assert messages(result.tracks[2], "program_change")[0].program == 0
    assert messages(result.tracks[2], "note_on")[0].velocity == 1


# --- The notes ---------------------------------------------------------------------------------------

def test_note_times_are_written_at_120_bpm():
    result = build([pitched("Piano", 0, [(0.5, 1.25, 60, 80)])])

    events = note_events(result.tracks[1])

    # 960 ticks per 0.5 s beat.
    assert events == [(960, "note_on", 60), (2400, "note_off", 60)]
    assert seconds(960) == pytest.approx(0.5) and seconds(2400) == pytest.approx(1.25)


def test_a_note_on_a_sounding_key_ends_the_earlier_one():
    # The same key twice, overlapping: the first one ends where the second starts.
    result = build([pitched("Piano", 0, [(0.0, 2.0, 60, 80), (1.0, 3.0, 60, 90), (0.5, 0.75, 64, 70)])])

    events = note_events(result.tracks[1])

    assert [(tick, kind) for tick, kind, key in events if key == 60] == [
        (0, "note_on"), (1920, "note_off"), (1920, "note_on"), (5760, "note_off")]
    # The note-off comes before the note-on at the same tick, so the key restarts.
    sixty = [(kind) for tick, kind, key in events if key == 60 and tick == 1920]
    assert sixty == ["note_off", "note_on"]
    assert [(tick, kind) for tick, kind, key in events if key == 64] == [(960, "note_on"), (1440, "note_off")]


def test_a_second_start_on_the_same_tick_is_dropped():
    result = build([pitched("Piano", 0, [(1.0, 2.0, 60, 80), (1.0, 1.5, 60, 100), (1.0, 2.0, 62, 80)])])

    events = note_events(result.tracks[1])

    assert len([e for e in events if e[1] == "note_on" and e[2] == 60]) == 1
    assert len([e for e in events if e[1] == "note_off" and e[2] == 60]) == 1
    assert len([e for e in events if e[2] == 62]) == 2


def test_a_note_never_ends_before_it_starts():
    result = build([pitched("Piano", 0, [(1.0, 1.0, 60, 80), (2.0, 1.5, 61, 80)])])

    for key in (60, 61):
        [(on, _, _), (off, _, _)] = [event for event in note_events(result.tracks[1]) if event[2] == key]
        assert off >= on + ensemble_midi.MIN_TICKS


def test_a_pitch_out_of_range_is_skipped():
    result = build([pitched("Piano", 0, [(0.0, 1.0, 128, 80), (0.0, 1.0, -1, 80), (0.0, 1.0, 60, 80)])])

    assert {key for _, _, key in note_events(result.tracks[1])} == {60}


def test_the_note_ons_and_offs_balance_in_every_track():
    import random

    random.seed(7)
    tracks = []
    for index in range(4):
        notes = [(round(random.uniform(0, 30), 3), 0.0, random.randint(30, 90), random.randint(1, 127))
                 for _ in range(300)]
        # Some long notes, many overlapping on a few keys, some at the same instant.
        notes = [(onset, onset + random.choice([0.0, 0.05, 0.5, 4.0]), pitch, velocity) for onset, _, pitch, velocity in notes]
        notes += [(5.0, 6.0, 60, 80)] * 3
        tracks.append(Track(f"t{index}", index, index == 3, notes))

    result = build(tracks)

    for track in result.tracks[1:]:
        ons, offs = defaultdict(int), defaultdict(int)
        sounding = set()
        for tick, message in absolute(track):
            if message.type == "note_on":
                assert message.note not in sounding, "a key started while sounding"
                sounding.add(message.note)
                ons[message.note] += 1
            elif message.type == "note_off":
                assert message.note in sounding
                sounding.discard(message.note)
                offs[message.note] += 1
        assert ons == offs and sum(ons.values()) > 0
        assert not sounding


def test_the_file_round_trips_through_a_saved_midi_file(tmp_path):
    tracks = [
        pitched("Piano", 0, [(0.0, 1.0, 60, 80), (0.5, 1.5, 64, 70)]),
        pitched("Flute", 73, [(2.0, 2.5, 76, 90)]),
        Track("drums", 0, True, [(0.0, 0.05, 36, 100), (0.5, 0.55, 38, 90)]),
    ]
    built = build(tracks)
    path = tmp_path / "ensemble.mid"

    built.save(str(path))
    loaded = mido.MidiFile(str(path))

    assert loaded.type == 1 and loaded.ticks_per_beat == TICKS_PER_BEAT and len(loaded.tracks) == 4
    for before, after in zip(built.tracks, loaded.tracks):
        assert [m for m in before if m.type != "end_of_track"] == [m for m in after if m.type != "end_of_track"]
    assert [messages(track, "track_name")[0].name for track in loaded.tracks[1:]] == ["Piano", "Flute", "drums"]
    assert len(messages(loaded.tracks[3], "note_on")) == 2 and messages(loaded.tracks[3], "program_change") == []


# --- The tempo map over a multi-track file -----------------------------------------------------------

def rubato_beats(count=48, first=0.7):
    import math

    beats = [first]
    for index in range(count - 1):
        beats.append(beats[-1] + 60 / (105 + 15 * math.sin(2 * math.pi * index / 32)))
    return beats


def played(midi):
    """{(channel, key): [(seconds, type), ...]} as a player hears the file, tracks merged."""
    now, found = 0.0, defaultdict(list)
    for message in midi:
        now += message.time
        if message.type in ("note_on", "note_off"):
            found[(message.channel, message.note)].append((now, message.type))
    return found


def test_the_tempo_map_keeps_every_note_of_a_multi_track_file_in_time():
    beats = rubato_beats()
    on_beat = [(beat, beat + 0.3, 60 + index % 5, 80) for index, beat in enumerate(beats[:-1])]
    bass = [(beat + 0.01, beat + 1.2, 40, 90) for beat in beats[:-1:2]]
    # The same key sounding twice: the earlier note is cut by the later one.
    overlapping = [(beats[3], beats[3] + 2.0, 70, 80), (beats[3] + 0.5, beats[3] + 1.0, 70, 80)]
    drums = [(beat, beat + 0.05, 36 if index % 2 == 0 else 38, 100) for index, beat in enumerate(beats[:-1])]
    original = build([
        pitched("Piano", 0, on_beat),
        pitched("Bass", 33, bass),
        pitched("Lead", 80, overlapping),
        Track("drums", 0, True, drums),
    ])
    tempo_map = build_tempo_map(beats, beats[1::4])
    assert tempo_map is not None

    result = apply_tempo_map(original, tempo_map)

    before, after = played(original), played(result)
    assert set(before) == set(after) and len(before) > 5
    assert {channel for channel, _ in before} == {0, 1, 2, 9}
    for key in before:
        assert [kind for _, kind in before[key]] == [kind for _, kind in after[key]]
        for (a, _), (b, _) in zip(before[key], after[key]):
            assert b == pytest.approx(a, abs=0.002)
    # Names, programs and the track count are untouched.
    assert len(result.tracks) == len(original.tracks)
    assert [messages(t, "track_name")[0].name for t in result.tracks[1:]] == ["Piano", "Bass", "Lead", "drums"]
    assert [m.program for t in result.tracks[1:] for m in messages(t, "program_change")] == [0, 33, 80]
