"""Ensemble jobs on the CPU (app/transcription/ensemble/cpu.py): reading the
tool's result, picking its build, the memory limit, and a run of the tool,
with a script in its place that behaves like dettato_server."""

import json
import subprocess
import sys
import textwrap
import time
import types

import numpy as np
import pytest

try:
    import resource  # noqa: F401
except ImportError:
    # Windows has no resource module, which cpu.py imports; the limit is
    # then not set (limit_process says so), as when prlimit is missing.
    sys.modules["resource"] = types.SimpleNamespace(RLIMIT_AS=9, RLIMIT_CORE=4)
    try:
        from app.transcription.ensemble import cpu
    finally:
        del sys.modules["resource"]
else:
    from app.transcription.ensemble import cpu

from app.transcription.cancellation import TranscriptionCancelled
from app.transcription.ensemble.cpu import CpuEnsembleFailed, parse_result


def valid(**changes):
    data = {
        "tracks": [
            {"name": "Piano", "program": 0, "is_drum": False},
            {"name": "drums", "program": 0, "is_drum": True},
            {"name": "Empty", "program": 40, "is_drum": False},
        ],
        "notes": [[0.5, 1.0, 60, 80, 0], [0.0, 0.1, 36, 100, 1], [1.0, 2.0, 64, 70, 0]],
        "tuning_cents": 3.5,
        "transcribed": 12,
        "dropped": 1,
        "inserted": 2,
        "drum_model": "kit",
    }
    data.update(changes)
    return data


# --- parse_result -------------------------------------------------------------------------------------

def test_a_valid_result_gives_tracks_with_their_notes_and_info():
    tracks, info = parse_result(valid())

    # The track without notes is dropped; the others keep their order.
    assert [(track.name, track.program, track.is_drum) for track in tracks] == [("Piano", 0, False), ("drums", 0, True)]
    assert tracks[0].notes == [(0.5, 1.0, 60, 80), (1.0, 2.0, 64, 70)]
    assert tracks[1].notes == [(0.0, 0.1, 36, 100)]
    assert info == {"drums": {"model": "kit"}, "tuning_cents": 3.5, "transcribed": 12, "dropped": 1, "inserted": 2}


def test_optional_info_may_be_missing():
    data = valid()
    for name in ("tuning_cents", "transcribed", "dropped", "inserted", "drum_model"):
        del data[name]

    _, info = parse_result(data)

    assert info == {"drums": {"model": None}, "tuning_cents": None, "transcribed": None, "dropped": None,
                    "inserted": None}


def test_names_are_cut_programs_clamped_and_offsets_never_before_onsets():
    data = valid(tracks=[{"name": "n" * 200, "program": 500, "is_drum": False},
                         {"name": "low", "program": -3, "is_drum": False}],
                 notes=[[1.0, 0.5, 60, 80, 0], [0.0, 1.0, 61, 80, 1]])

    tracks, _ = parse_result(data)

    assert len(tracks[0].name) == 64 and tracks[0].program == 127
    assert tracks[1].program == 0
    assert tracks[0].notes == [(1.0, 1.0, 60, 80)]


def test_no_notes_give_no_tracks():
    tracks, _ = parse_result(valid(notes=[]))

    assert tracks == []


@pytest.mark.parametrize(
    ("changes", "error"),
    [
        ({"tracks": "x"}, ValueError),
        ({"tracks": None}, ValueError),
        ({"tracks": [{"name": 5, "program": 0, "is_drum": False}]}, ValueError),
        ({"tracks": [{"name": "a", "program": "0", "is_drum": False}]}, ValueError),
        ({"tracks": [{"name": "a", "program": 0, "is_drum": 1}]}, ValueError),
        ({"tracks": [{"name": "a", "program": 0}]}, KeyError),
        ({"notes": [[0.0, 1.0, 60, 80, 3]]}, ValueError),
        ({"notes": [[0.0, 1.0, 60, 80, -1]]}, ValueError),
        ({"notes": [[0.0, 1.0, 60, 80, "0"]]}, ValueError),
        ({"notes": [[0.0, 1.0, 60, 80, 0.0]]}, ValueError),
        ({"notes": [[float("nan"), 1.0, 60, 80, 0]]}, ValueError),
        ({"notes": [[0.0, float("inf"), 60, 80, 0]]}, ValueError),
        ({"notes": [[-0.5, 1.0, 60, 80, 0]]}, ValueError),
        ({"notes": [[0.0, 1.0, 60, 80]]}, ValueError),
        ({"notes": [["x", 1.0, 60, 80, 0]]}, ValueError),
        ({"notes": [None]}, TypeError),
        ({"notes": None}, TypeError),
        ({"notes": [[0.0, None, 60, 80, 0]]}, TypeError),
    ],
)
def test_a_malformed_result_raises(changes, error):
    with pytest.raises(error):
        parse_result(valid(**changes))


def test_a_result_without_tracks_or_notes_raises():
    for name in ("tracks", "notes"):
        data = valid()
        del data[name]
        with pytest.raises(KeyError):
            parse_result(data)


# --- The build and the memory limit ------------------------------------------------------------------

@pytest.mark.parametrize(
    ("flags", "build"),
    [
        ({"avx2", "fma", "f16c", "sse4_2"}, "avx2"),
        ({"avx2", "fma"}, "generic"),
        ({"avx2", "f16c"}, "generic"),
        ({"fma", "f16c"}, "generic"),
        ({"sse2"}, "generic"),
        (set(), "generic"),
    ],
)
def test_the_binary_follows_the_cpu_flags(monkeypatch, flags, build):
    monkeypatch.setattr(cpu, "_cpu_flags", lambda: flags)

    assert cpu.binary() == cpu.BINARIES[build]
    assert cpu.BINARIES["avx2"] != cpu.BINARIES["generic"]


def test_cpu_flags_come_from_proc_cpuinfo_or_are_empty(monkeypatch, tmp_path):
    info = tmp_path / "cpuinfo"
    info.write_text("processor\t: 0\nflags\t\t: fpu avx2 fma f16c\nbogomips\t: 1\n")
    real_path = cpu.Path
    monkeypatch.setattr(cpu, "Path", lambda name: info if name == "/proc/cpuinfo" else real_path(name))

    assert cpu._cpu_flags() == {"fpu", "avx2", "fma", "f16c"}
    monkeypatch.setattr(cpu, "Path", lambda name: tmp_path / "missing")
    assert cpu._cpu_flags() == set()


def test_the_address_space_limit_grows_with_the_audio():
    base = cpu.address_space_limit(0)
    assert base == cpu.BASE_ADDRESS_SPACE
    assert cpu.address_space_limit(100) == base + 100 * cpu.ADDRESS_SPACE_PER_SECOND
    assert cpu.address_space_limit(3600) > cpu.address_space_limit(600) > base
    assert cpu.address_space_limit(-5) == base
    assert isinstance(cpu.address_space_limit(12.5), int)


def test_the_limit_is_set_on_the_started_process_where_the_system_has_prlimit(monkeypatch):
    calls = []
    fake = types.SimpleNamespace(RLIMIT_AS=9, RLIMIT_CORE=4, prlimit=lambda *args: calls.append(args))
    monkeypatch.setattr(cpu, "resource", fake)

    cpu.limit_process(123, 1000)

    assert calls == [(123, 9, (1000, 1000)), (123, 4, (0, 0))]


def test_a_limit_that_cannot_be_set_does_not_stop_the_job(monkeypatch):
    def refuse(*args):
        raise OSError("gone")

    monkeypatch.setattr(cpu, "resource", types.SimpleNamespace(RLIMIT_AS=9, RLIMIT_CORE=4, prlimit=refuse))
    cpu.limit_process(123, 1000)
    monkeypatch.setattr(cpu, "resource", types.SimpleNamespace(RLIMIT_AS=9, RLIMIT_CORE=4))
    cpu.limit_process(123, 1000)


# --- A run, with a script in the tool's place ---------------------------------------------------------

# Behaves like dettato_server: MODEL AUDIO RATE SOURCE_RATE THREADS OUT, progress lines on stdout, the result as JSON
# in OUT. What it does is named by the model file's name.
FAKE_TOOL = textwrap.dedent('''
    import json, os, sys, time

    model, audio, rate, source_rate, threads, out = sys.argv[1:7]
    behaviour = os.path.basename(model)
    samples = os.path.getsize(audio) // 4
    if behaviour == "hang":
        print("progress 0.1", flush=True)
        time.sleep(120)
        sys.exit(0)
    if behaviour == "fail":
        print("progress 0.2", flush=True)
        print("out of memory somewhere", file=sys.stderr, flush=True)
        sys.exit(3)
    print("loading", flush=True)
    print("progress 0.25", flush=True)
    print("progress nope", flush=True)
    print("progress 1.5", flush=True)
    print("progress -1", flush=True)
    if behaviour == "nofile":
        sys.exit(0)
    if behaviour == "badjson":
        open(out, "w").write("{not json")
        sys.exit(0)
    if behaviour == "badnote":
        open(out, "w").write(json.dumps({"tracks": [], "notes": [[0, 1, 60, 80, 7]]}))
        sys.exit(0)
    result = {
        "tracks": [{"name": "Guitar", "program": 24, "is_drum": False}, {"name": "drums", "program": 0, "is_drum": True}],
        "notes": [[0.5, 1.0, 60, 80, 0], [0.0, 0.1, 36, 100, 1]],
        "transcribed": {"rate": int(rate), "source_rate": int(source_rate), "threads": int(threads), "samples": samples,
                        "argv": len(sys.argv)},
    }
    open(out, "w").write(json.dumps(result))
''')


@pytest.fixture
def tool(tmp_path, monkeypatch):
    """cpu.transcribe starts the fake tool instead of the binary; behaviour(name) picks what it does."""
    script = tmp_path / "fake_dettato_server.py"
    script.write_text(FAKE_TOOL)
    started = []
    real_popen = subprocess.Popen

    def popen(command, **options):
        process = real_popen([sys.executable, str(script), *command[1:]], **options)
        started.append((command, process))
        return process

    monkeypatch.setattr(cpu.subprocess, "Popen", popen)
    monkeypatch.setattr(cpu, "POLL_SECONDS", 0.02)
    monkeypatch.setattr(cpu, "binary", lambda: tmp_path / "dettato_server-fake")

    def behaviour(name):
        monkeypatch.setattr(cpu, "MODEL", tmp_path / name)
        return started

    return behaviour


def audio(seconds=2.0, rate=16000):
    return (np.sin(np.arange(int(seconds * rate)) * 0.05) * 0.5).astype(np.float32)


def leftovers(folder):
    return [path for path in folder.iterdir() if path.name.startswith("ensemble-")]


def test_a_run_gives_the_tracks_and_passes_what_the_tool_needs(tool, tmp_path):
    started = tool("ok")
    progress, stats = [], {}
    x = audio(2.0, 16000)

    tracks, info = cpu.transcribe(x, 16000, 44100, progress.append, lambda: False, stats, tmp_path)

    assert [(track.name, track.program, track.is_drum) for track in tracks] == [("Guitar", 24, False), ("drums", 0, True)]
    assert tracks[0].notes == [(0.5, 1.0, 60, 80)]
    # The arguments: model, audio, rate, source rate, threads, result.
    command = started[0][0]
    assert command[1] == str(tmp_path / "ok") and len(command) == 7
    assert command[3:5] == ["16000", "44100"]
    echoed = info["transcribed"]
    assert (echoed["rate"], echoed["source_rate"], echoed["samples"], echoed["argv"]) == (16000, 44100, len(x), 7)
    # Progress lines are read as fractions, clamped; others are ignored.
    assert progress == [0.25, 1.0, 0.0]
    assert stats["model_s"] >= 0
    # Its files are in a folder of the job's own, removed afterwards.
    assert leftovers(tmp_path) == []


def test_an_unknown_source_rate_is_the_rate_of_the_audio(tool, tmp_path):
    tool("ok")

    _, info = cpu.transcribe(audio(1.0, 22050), 22050, None, lambda value: None, lambda: False, work_dir=tmp_path)

    assert (info["transcribed"]["rate"], info["transcribed"]["source_rate"]) == (22050, 22050)


def test_a_tool_that_exits_with_an_error_fails_the_job(tool, tmp_path, caplog):
    tool("fail")

    with pytest.raises(CpuEnsembleFailed) as error:
        cpu.transcribe(audio(), 16000, 16000, lambda value: None, lambda: False, work_dir=tmp_path)

    assert "failed on the CPU" in str(error.value)
    assert "exited with 3" in caplog.text and "out of memory somewhere" in caplog.text
    assert leftovers(tmp_path) == []


@pytest.mark.parametrize("behaviour", ["badjson", "badnote", "nofile"])
def test_a_result_that_cannot_be_read_fails_the_job(tool, tmp_path, behaviour):
    tool(behaviour)

    with pytest.raises(CpuEnsembleFailed):
        cpu.transcribe(audio(), 16000, 16000, lambda value: None, lambda: False, work_dir=tmp_path)

    assert leftovers(tmp_path) == []


def test_a_cancelled_job_ends_the_tool(tool, tmp_path):
    started = tool("hang")
    progress = []
    begun = time.monotonic()

    # Cancelled once the tool says it works.
    with pytest.raises(TranscriptionCancelled):
        cpu.transcribe(audio(), 16000, 16000, progress.append, lambda: bool(progress), work_dir=tmp_path)

    assert time.monotonic() - begun < 30
    [(_, process)] = started
    assert process.poll() is not None and process.returncode != 0
    assert leftovers(tmp_path) == []


def test_a_job_cancelled_at_once_never_waits_for_the_tool(tool, tmp_path):
    started = tool("hang")

    with pytest.raises(TranscriptionCancelled):
        cpu.transcribe(audio(), 16000, 16000, lambda value: None, lambda: True, work_dir=tmp_path)

    assert started[0][1].poll() is not None


def test_check_says_what_is_missing(monkeypatch, tmp_path, caplog):
    import logging

    monkeypatch.setattr(cpu, "binary", lambda: tmp_path / "dettato_server-avx2")
    monkeypatch.setattr(cpu, "MODEL", tmp_path / "model.gguf")
    with caplog.at_level(logging.INFO):
        cpu.check()
        assert "need" in caplog.text and "dettato_server-avx2" in caplog.text
        caplog.clear()
        (tmp_path / "dettato_server-avx2").write_text("x")
        (tmp_path / "model.gguf").write_text("x")
        cpu.check()
        assert "run dettato_server-avx2" in caplog.text


def test_the_fake_tool_is_valid_python():
    # Or every run test above would fail the same way.
    compile(FAKE_TOOL, "fake_dettato_server.py", "exec")
