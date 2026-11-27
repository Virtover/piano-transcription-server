"""The end-to-end checks (app.e2e): reading MIDI, what passes, and a run
against fake workers."""

import struct
from types import SimpleNamespace

import pytest

from app import e2e
from app.admin.common import AdminError
from app.worker import tasks


def varlen(value: int) -> bytes:
    out = [value & 0x7F]
    value >>= 7
    while value:
        out.append(0x80 | (value & 0x7F))
        value >>= 7
    return bytes(reversed(out))


def midi(notes: list[tuple[float, int]], tempo: int = 500000, division: int = 480) -> bytes:
    """A one-track MIDI file of notes (onset seconds, pitch), at a tempo."""
    events, last = bytearray(), 0
    events += varlen(0) + b"\xff\x51\x03" + tempo.to_bytes(3, "big")
    for onset, pitch in sorted(notes):
        tick = round(onset * 1e6 / tempo * division)
        events += varlen(tick - last) + bytes([0x90, pitch, 80])
        # A running-status note-off, as velocity 0
        events += varlen(10) + bytes([pitch, 0])
        last = tick + 10
    events += varlen(0) + b"\xff\x2f\x00"
    return b"MThd" + struct.pack(">IHHH", 6, 0, 1, division) + b"MTrk" + struct.pack(">I", len(events)) + bytes(events)


def plucks(start: float, end: float, offset: float = 0.0) -> list[tuple[float, int]]:
    """The test file's notes from start to end, as a part starting at offset."""
    notes = []
    time = start
    while time < end - 1e-9:
        notes.append((round(time - offset, 3), e2e.PITCHES[int(time // e2e.SECTION)]))
        time += 1 / e2e.NOTES_PER_SECOND
    return notes


def test_midi_notes_honour_the_tempo():
    notes = e2e.midi_notes(midi([(0.0, 60), (1.5, 64)], tempo=250000))
    assert [(round(onset, 3), pitch) for onset, pitch in notes] == [(0.0, 60), (1.5, 64)]
    with pytest.raises(ValueError):
        e2e.midi_notes(b"RIFF")


@pytest.mark.parametrize(
    ("part", "inside"),
    [(None, {60, 64, 67, 69}), ((10.0, 20.0), {64}), ((20.0, None), {67, 69}), ((5.0, 15.0), {60, 64})],
)
def test_expected_pitches(part, inside):
    assert e2e.expected_pitches(part) == (inside, set(e2e.PITCHES) - inside)


def result(mode="piano"):
    return e2e.Result("clip", mode, "job")


def test_a_correct_cut_passes():
    checked = result()
    e2e.check(checked, (10.0, 20.0), 10.0, plucks(10, 20, offset=10))
    assert checked.problems == []
    assert checked.notes == {64: 20}


def test_a_cut_in_the_wrong_place_fails():
    # The first 10 seconds instead of 10 to 20
    checked = result()
    e2e.check(checked, (10.0, 20.0), 10.0, plucks(0, 10))
    assert any("pitch 60" in problem for problem in checked.problems)
    assert any("pitch 64" in problem for problem in checked.problems)


def test_a_part_not_cut_at_its_end_fails():
    checked = result()
    e2e.check(checked, (10.0, 20.0), 10.0, plucks(10, 30, offset=10))
    assert any("past the part" in problem for problem in checked.problems)


@pytest.mark.parametrize("mode", e2e.MODES)
def test_both_modes_are_held_to_the_strict_check(mode):
    # Ensemble jobs once had a lenient check, as piano covers rewrote the
    # notes; every instrument is transcribed now, so the plucked part must
    # come back as the piano job gets it.
    checked = result(mode)
    e2e.check(checked, (10.0, 20.0), 10.0, plucks(10, 20, offset=10))
    assert checked.problems == []
    sparse = result(mode)
    e2e.check(sparse, (10.0, 20.0), 10.0, [(0.0, 64), (0.5, 64), (1.0, 52), (2.0, 64)])
    assert any("notes of pitch 64, expected at least" in problem for problem in sparse.problems)
    wrong = result(mode)
    e2e.check(wrong, (10.0, 20.0), 10.0, [(0.0, 60), (0.5, 60), (1.0, 64)])
    assert wrong.problems


def test_the_modes_are_piano_and_ensemble():
    assert e2e.MODES == ("piano", "ensemble")


def test_midi_notes_ignore_the_drum_channel():
    delta = varlen(0)
    track = b"".join([
        delta + bytes([0x90, 60, 80]),       # channel 1, pitched
        delta + bytes([0x99, 36, 90]),       # channel 10, a drum
        varlen(480) + bytes([0x80, 60, 0]),
        delta + bytes([0x89, 36, 0]),
        delta + bytes([0x91, 64, 80]),       # channel 2, pitched
        delta + b"\xff\x2f\x00",
    ])
    data = b"MThd" + struct.pack(">IHHH", 6, 1, 1, 480) + b"MTrk" + struct.pack(">I", len(track)) + track

    assert [(round(onset, 3), pitch) for onset, pitch in e2e.midi_notes(data)] == [(0.0, 60), (0.5, 64)]


@pytest.fixture
def fake_server(api_context, monkeypatch):
    """Workers that 'transcribe' the stored part correctly, or as told."""
    _, settings, _ = api_context
    sent = []
    behaviour = {"offset_error": 0.0}

    def send_job(job_id, backend, *arguments, **options):
        sent.append((job_id, backend))
        job = tasks.redis.hgetall(tasks.job_key(job_id))
        start = float(job.get("clip_start") or 0) + behaviour["offset_error"]
        end = float(job["clip_end"]) if job.get("clip_end") else e2e.SECONDS
        end = min(end + behaviour["offset_error"], e2e.SECONDS)
        folder = tasks.job_dir(job_id)
        folder.mkdir(parents=True, exist_ok=True)
        result = folder / "transcription.mid"
        result.write_bytes(midi(plucks(start, end, offset=start)))
        tasks.redis.hset(tasks.job_key(job_id), mapping={"status": "completed", "result": str(result), "node": "hub"})

    monkeypatch.setattr(e2e, "test_audio", lambda path: path.write_bytes(b"ID3audio"))
    monkeypatch.setattr(tasks, "send_job", send_job)
    return settings, sent, behaviour


def args(**values):
    return SimpleNamespace(**{"case": None, "mode": "piano", "backend": "local", "timeout": 30,
                              "keep": False, "json": False, **values})


def test_a_run_checks_every_case_and_cleans_up(fake_server, capsys):
    _, sent, _ = fake_server

    e2e.run(args())

    output = capsys.readouterr().out
    assert output.count("PASS") == len(e2e.CASES)
    assert "All 3 end-to-end checks passed" in output
    assert [backend for _, backend in sent] == ["local"] * 3
    # Nothing stays behind, and nobody was charged
    assert list(tasks.redis.scan_iter("transcription:*")) == []


def test_a_run_of_both_modes_stores_each_as_the_server_does(fake_server, capsys):
    _, sent, _ = fake_server

    e2e.run(args(case="clip", mode=None, keep=True))

    assert "All 2 end-to-end checks passed" in capsys.readouterr().out
    stored = [tasks.redis.hgetall(tasks.job_key(job_id)) for job_id, _ in sent]
    by_name = {fields["mode_name"]: fields for fields in stored}
    assert set(by_name) == {"piano", "ensemble"}
    # Ensemble jobs are stored as "arrangement", as the API stores them.
    assert by_name["ensemble"]["mode"] == "arrangement" and "mode" not in by_name["piano"]


def test_a_wrong_cut_makes_the_run_fail(fake_server, capsys):
    _, _, behaviour = fake_server
    behaviour["offset_error"] = 5.0

    with pytest.raises(AdminError, match="end-to-end checks failed"):
        e2e.run(args(case="clip"))
    assert "FAIL" in capsys.readouterr().out


def test_jobs_on_this_servers_workers_are_never_outsourced(fake_server):
    _, sent, _ = fake_server
    e2e.run(args(case="clip", keep=True))
    job_id = sent[0][0]
    assert tasks.redis.hget(tasks.job_key(job_id), "outsourced") == "1"
    assert tasks.redis.hget(tasks.job_key(job_id), "user_id") is None


def test_remote_needs_outsourcing(fake_server):
    with pytest.raises(AdminError, match="outsourcing is off"):
        e2e.run(args(backend="remote"))
