import math
import random
import re
import time
from pathlib import Path

import mido
import pytest

from app.transcription import tempo
from app.transcription.tempo import TICKS_PER_BEAT, apply_tempo_map, build_tempo_map

ROOT = Path(__file__).resolve().parent.parent


def rubato_beats(count=64, first=0.7):
    """Beats drifting between 90 and 120 BPM, like a played performance."""
    beats = [first]
    for index in range(count - 1):
        beats.append(beats[-1] + 60 / (105 + 15 * math.sin(2 * math.pi * index / 32)))
    return beats


def transkun_midi(notes):
    """A MIDI file like Transkun's: 120 BPM, notes (start, end, pitch) in seconds."""
    midi = mido.MidiFile(type=1, ticks_per_beat=220)
    meta = mido.MidiTrack([
        mido.MetaMessage("set_tempo", tempo=500_000),
        mido.MetaMessage("time_signature", numerator=4, denominator=4),
    ])
    events = sorted(
        [(start, 1, mido.Message("note_on", note=pitch, velocity=80)) for start, _, pitch in notes]
        + [(end, 0, mido.Message("note_off", note=pitch, velocity=0)) for _, end, pitch in notes],
        key=lambda event: (event[0], event[1]),
    )
    track = mido.MidiTrack([mido.MetaMessage("track_name", name="Piano"), mido.Message("program_change", program=0)])
    previous = 0
    for seconds, _, message in events:
        tick = round(mido.second2tick(seconds, 220, 500_000))
        track.append(message.copy(time=tick - previous))
        previous = tick
    midi.tracks.extend([meta, track])
    return midi


def note_times(midi):
    """(seconds, type, pitch) of every note event, as a player hears them."""
    now, times = 0.0, []
    for message in midi:
        now += message.time
        if message.type in ("note_on", "note_off"):
            times.append((now, message.type, message.note))
    return times


def note_ticks(midi, pitch):
    tick, ticks = 0, []
    for message in midi.tracks[1]:
        tick += message.time
        if message.type == "note_on" and message.note == pitch:
            ticks.append(tick)
    return ticks


def meters(midi):
    tick, found = 0, []
    for message in midi.tracks[0]:
        tick += message.time
        if message.type == "time_signature":
            found.append((tick // TICKS_PER_BEAT, message.numerator, message.denominator))
    return found


def test_notes_keep_their_time_and_land_on_the_beat_grid():
    beats = rubato_beats()
    on_beat = [(beat, beat + 0.2, 60) for beat in beats[:-1]]
    # Syncopation: halfway between beats, and a grace note just before one.
    offbeat = [((a + b) / 2, (a + b) / 2 + 0.1, 72) for a, b in zip(beats[1:-1:2], beats[2::2])]
    grace = [(beats[10] - 0.03, beats[10] - 0.01, 76)]
    original = transkun_midi(on_beat + offbeat + grace)

    result = apply_tempo_map(original, build_tempo_map(beats, beats[1::4]))

    for (before, kind, pitch), (after, kind_after, pitch_after) in zip(note_times(original), note_times(result)):
        assert (kind, pitch) == (kind_after, pitch_after)
        assert after == pytest.approx(before, abs=0.001)
    assert len(note_times(result)) == len(note_times(original))

    # Within the 220 ticks per beat of Transkun's file: a few of these ticks.
    # One lead beat before the first detected beat at 0.7 s.
    beat_ticks = note_ticks(result, 60)
    assert all(abs(tick - (index + 1) * TICKS_PER_BEAT) <= 3 for index, tick in enumerate(beat_ticks))
    offbeat_ticks = note_ticks(result, 72)
    assert all(abs(tick % TICKS_PER_BEAT - TICKS_PER_BEAT // 2) <= 3 for tick in offbeat_ticks)


def test_tempo_changes_follow_the_performance():
    beats = rubato_beats()
    tempo_map = build_tempo_map(beats, beats[::4])

    # After the lead beat before 0.7 s.
    tempos = [60_000_000 / value for value in tempo_map.tempos[1:]]
    assert min(tempos) == pytest.approx(90, abs=1)
    assert max(tempos) == pytest.approx(120, abs=1)


def test_time_before_the_first_beat_becomes_whole_beats():
    beats = [1.0 + 0.5 * index for index in range(16)]

    tempo_map = build_tempo_map(beats, beats[::4])

    assert tempo_map.beats_us[:3] == [0, 500_000, 1_000_000]
    assert tempo_map.tick(1.0) == 2 * TICKS_PER_BEAT
    assert tempo_map.tick(0.25) == TICKS_PER_BEAT // 2


def test_pickup_bar_before_the_first_downbeat():
    beats = [0.5 * index for index in range(20)]

    # Downbeats on the second beat: a pickup of one beat, then 4/4.
    tempo_map = build_tempo_map(beats, beats[1::4])

    assert tempo_map.meters == [(0, 1), (1, 4)]


def test_three_four_time_from_the_downbeats():
    beats = [0.4 * index for index in range(30)]

    assert build_tempo_map(beats, beats[::3]).meters == [(0, 3)]


def test_a_bar_of_another_length_is_followed():
    beats = [0.5 * index for index in range(40)]
    downbeats = [beats[index] for index in (0, 4, 8, 12, 14, 18, 22, 26, 30, 34, 38)]

    # A 2/4 bar among 4/4 bars keeps the bar lines on the downbeats.
    assert build_tempo_map(beats, downbeats).meters == [(0, 4), (12, 2), (14, 4)]


def test_noisy_downbeats_give_no_map():
    beats = [0.5 * index for index in range(40)]
    # Half the bars in 4/4: no meter can be trusted.
    downbeats = [beats[index] for index in (2, 6, 10, 13, 18, 22, 25, 30, 34)]

    assert build_tempo_map(beats, downbeats) is None


def test_too_few_downbeats_give_no_map():
    beats = [0.5 * index for index in range(40)]

    assert build_tempo_map(beats, []) is None
    assert build_tempo_map(beats, [beats[4]]) is None


def test_missed_downbeats_are_bars_of_the_main_meter():
    beats = [0.5 * index for index in range(40)]
    downbeats = [beats[index] for index in (0, 4, 8, 16, 20, 24, 28, 32, 36)]

    assert build_tempo_map(beats, downbeats).meters == [(0, 4)]


def test_a_missed_beat_is_filled_in():
    beats = [0.5 * index for index in range(20)]
    del beats[7]

    tempo_map = build_tempo_map(beats, beats[::4])

    assert len(tempo_map.beats_us) == 20
    assert tempo_map.beats_us[7] == 3_500_000


def test_a_spurious_beat_is_dropped():
    beats = [0.5 * index for index in range(20)]
    downbeats = beats[::4]
    beats.insert(8, 3.7)

    tempo_map = build_tempo_map(beats, downbeats)

    assert len(tempo_map.beats_us) == 20
    assert 3_700_000 not in tempo_map.beats_us


def test_too_few_or_irregular_beats_give_no_map():
    random.seed(1)
    assert build_tempo_map([0.5 * index for index in range(5)], [0.0]) is None
    irregular = sorted(random.uniform(0, 30) for _ in range(60))
    assert build_tempo_map(irregular, irregular[::4]) is None
    # 600 BPM is not a tempo.
    assert build_tempo_map([0.1 * index for index in range(40)], []) is None


def test_the_tempo_map_is_written(tmp_path, monkeypatch):
    beats = rubato_beats()
    path = tmp_path / "transcription.mid"
    transkun_midi([(beat, beat + 0.2, 60) for beat in beats]).save(path)
    monkeypatch.setattr(tempo, "detect_beats", lambda audio, rate, device: (beats, beats[::4]))

    assert tempo.add_tempo_map(path, None, 44100)

    midi = mido.MidiFile(path)
    assert midi.ticks_per_beat == TICKS_PER_BEAT
    # A pickup: the lead beat before the first beat, which is a downbeat.
    assert meters(midi) == [(0, 1, 4), (1, 4, 4)]


def test_failures_keep_the_midi_as_written(tmp_path, monkeypatch):
    path = tmp_path / "transcription.mid"
    transkun_midi([(1.0, 1.5, 60)]).save(path)
    written = path.read_bytes()

    def fail(audio, rate, device):
        raise RuntimeError("no model")

    monkeypatch.setattr(tempo, "detect_beats", fail)
    assert not tempo.add_tempo_map(path, None, 44100)
    monkeypatch.setattr(tempo, "detect_beats", lambda audio, rate, device: ([1.0, 2.0], []))
    assert not tempo.add_tempo_map(path, None, 44100)
    assert path.read_bytes() == written


def test_the_setting_turns_it_off(tmp_path, monkeypatch):
    path = tmp_path / "transcription.mid"
    transkun_midi([(1.0, 1.5, 60)]).save(path)
    monkeypatch.setattr(tempo.settings.worker, "tempo_map", False)
    monkeypatch.setattr(tempo, "detect_beats", lambda *arguments: pytest.fail("beats detected"))

    assert not tempo.add_tempo_map(path, None, 44100)


@pytest.mark.parametrize("dockerfile", ["Dockerfile.worker", "Dockerfile.runpod"])
def test_images_include_the_beat_tracker_weights(dockerfile):
    text = (ROOT / dockerfile).read_text(encoding="utf-8")

    assert re.search(r"ARG BEAT_THIS_CHECKPOINT_SHA256=[0-9a-f]{64}", text)
    assert text.count(str(tempo.CHECKPOINT.as_posix())) >= 3
    assert "sha256sum -c -" in text


def test_beat_tracking_moves_to_cpu_when_the_gpu_is_full(tmp_path, monkeypatch):
    beats = rubato_beats()
    path = tmp_path / "transcription.mid"
    transkun_midi([(beat, beat + 0.2, 60) for beat in beats]).save(path)
    devices = []

    def detect(audio, rate, device):
        devices.append(device)
        if device != "cpu":
            raise RuntimeError("CUDA out of memory. Tried to allocate 64.00 MiB")
        return beats, beats[::4]

    monkeypatch.setattr(tempo, "detect_beats", detect)

    assert tempo.add_tempo_map(path, None, 44100, "cuda:0")
    assert devices == ["cuda:0", "cpu"]


def test_gpu_job_capacity_reserves_the_beat_tracker(monkeypatch):
    from app.transcription import device

    free = 5.86 * 1024**3
    monkeypatch.setattr(tempo.settings.worker, "memory_per_job_gib", 0.5)
    with_map = device.job_capacity(free)
    monkeypatch.setattr(tempo.settings.worker, "tempo_map", False)

    # 5.86 GiB - 1.0 for the model, with and without 0.25 for the tracker.
    assert (with_map, device.job_capacity(free)) == (9, 9)
    assert device.job_capacity(1.0 * 1024**3 + 0.5 * 1024**3) == 1
    monkeypatch.setattr(tempo.settings.worker, "tempo_map", True)
    assert device.job_capacity(1.0 * 1024**3 + 0.5 * 1024**3) == 0


# 441000 samples are exactly 1000 hops: the last block is one frame.
@pytest.mark.parametrize("samples", [22050 * 20, 22050 * 20 + 300])
def test_spectrogram_in_blocks_is_beat_this_spectrogram(monkeypatch, samples):
    np = pytest.importorskip("numpy")
    torch = pytest.importorskip("torch")
    preprocessing = pytest.importorskip("beat_this.preprocessing")
    signal = np.random.default_rng(1).standard_normal(samples).astype(np.float32) * 0.1
    # Blocks of 2 s, so that 20 s of audio crosses several block borders.
    monkeypatch.setattr(tempo, "SPECTROGRAM_BLOCK_FRAMES", 100)

    expected = preprocessing.LogMelSpect()(torch.from_numpy(signal))
    blocked = tempo._Spectrogram()(signal)

    assert blocked.shape == expected.shape
    assert torch.allclose(blocked, expected, rtol=1e-4, atol=1e-3)


def test_a_failed_write_keeps_the_midi_as_written(tmp_path, monkeypatch):
    beats = rubato_beats()
    path = tmp_path / "transcription.mid"
    transkun_midi([(beat, beat + 0.2, 60) for beat in beats]).save(path)
    written = path.read_bytes()
    monkeypatch.setattr(tempo, "detect_beats", lambda audio, rate, device: (beats, beats[::4]))

    def full_disk(self, filename):
        Path(filename).write_bytes(b"MThd")
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(mido.MidiFile, "save", full_disk)

    assert not tempo.add_tempo_map(path, None, 44100)
    assert path.read_bytes() == written
    assert [file.name for file in tmp_path.iterdir()] == ["transcription.mid"]


def test_a_map_that_would_move_notes_is_not_written(tmp_path, monkeypatch):
    beats = rubato_beats()
    path = tmp_path / "transcription.mid"
    transkun_midi([(beat, beat + 0.2, 60) for beat in beats]).save(path)
    written = path.read_bytes()
    monkeypatch.setattr(tempo, "detect_beats", lambda audio, rate, device: (beats, beats[::4]))
    shifted = tempo.TempoMap.tick
    monkeypatch.setattr(tempo.TempoMap, "tick", lambda self, seconds: shifted(self, seconds) + 100)

    assert not tempo.add_tempo_map(path, None, 44100)
    assert path.read_bytes() == written


def test_the_weights_are_never_downloaded(tmp_path, monkeypatch):
    pytest.importorskip("beat_this")
    monkeypatch.setattr(tempo, "CHECKPOINT", tmp_path / "missing.ckpt")

    with pytest.raises(FileNotFoundError, match="missing.ckpt"):
        tempo._Tracker("cpu")


def test_two_hours_of_beats_take_moments():
    beats = rubato_beats(count=12_000)
    # A spurious beat in every bar, and downbeats to match up.
    noisy = sorted(beats + [beat + 0.1 for beat in beats[2::4]])

    started = time.monotonic()
    tempo_map = build_tempo_map(noisy, beats[::4])

    assert time.monotonic() - started < 3
    assert len(tempo_map.beats_us) == 12_001


def test_preloading_loads_the_tracker_once(monkeypatch):
    loaded = []
    monkeypatch.setattr(tempo, "_trackers", {})
    monkeypatch.setattr(tempo, "_Tracker", lambda device: loaded.append(device) or (lambda audio, rate: ([], [])))

    tempo.preload_tracker("cuda:0")
    tempo.detect_beats([0.0], 44100, "cuda:0")

    assert loaded == ["cuda:0"]


def test_preloading_skips_the_tracker_when_the_tempo_map_is_off(monkeypatch):
    monkeypatch.setattr(tempo.settings.worker, "tempo_map", False)
    monkeypatch.setattr(tempo, "_Tracker", lambda device: pytest.fail("tracker loaded"))

    tempo.preload_tracker("cpu")
