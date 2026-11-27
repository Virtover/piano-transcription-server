"""The sample rate of a job's source (app/transcription/audio.py), which the
ensemble drums need: read with ffprobe, None whenever that fails."""

import subprocess
import types

import pytest

from app import media
from app.transcription import audio


def probe_says(monkeypatch, stdout="", returncode=0, error=None, calls=None):
    def run(command, **options):
        if calls is not None:
            calls.append((command, options))
        if error:
            raise error
        return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")

    monkeypatch.setattr(audio.subprocess, "run", run)


@pytest.mark.parametrize(
    ("output", "rate"),
    [
        ("48000\n", 48000),
        ("44100", 44100),
        ("  22050  \n", 22050),
        ("96000,\n", 96000),
        # A file with two audio streams in one line: the first one.
        ("48000,44100\n", 48000),
        ("1000000\n", 1_000_000),
        ("1\n", 1),
    ],
)
def test_the_rate_is_what_ffprobe_prints(monkeypatch, tmp_path, output, rate):
    probe_says(monkeypatch, output)

    assert audio.source_sample_rate(tmp_path / "audio.webm") == rate


@pytest.mark.parametrize("output", ["", "\n", "N/A\n", "abc", "-48000\n", "0\n", "1000001\n", "99999999999\n", "4.8e4\n"])
def test_a_rate_that_is_not_one_is_none(monkeypatch, tmp_path, output):
    probe_says(monkeypatch, output)

    assert audio.source_sample_rate(tmp_path / "audio.webm") is None


@pytest.mark.parametrize(
    "error",
    [
        FileNotFoundError("ffprobe"),
        PermissionError("ffprobe"),
        subprocess.TimeoutExpired("ffprobe", 60),
        subprocess.SubprocessError("boom"),
    ],
)
def test_a_failed_probe_is_none(monkeypatch, tmp_path, error):
    probe_says(monkeypatch, error=error)

    assert audio.source_sample_rate(tmp_path / "audio.webm") is None


def test_ffprobe_reads_only_the_first_audio_stream_of_a_local_file(monkeypatch, tmp_path):
    calls = []
    probe_says(monkeypatch, "48000\n", calls=calls)
    path = tmp_path / "-weird name.webm"

    audio.source_sample_rate(path)

    [(command, options)] = calls
    assert command[0] == "ffprobe"
    assert command[command.index("-select_streams") + 1] == "a:0"
    assert command[command.index("-protocol_whitelist") + 1] == "file"
    # The path follows "--", so a name that starts with "-" is no option.
    assert command[-2:] == ["--", str(path)]
    assert options["timeout"] > 0 and options["stdin"] == subprocess.DEVNULL


def test_an_upload_is_probed_with_the_demuxers_uploads_may_have(monkeypatch, tmp_path):
    calls = []
    probe_says(monkeypatch, "44100\n", calls=calls)

    assert audio.source_sample_rate(tmp_path / "upload.input", upload=True) == 44100

    [(command, _)] = calls
    assert all(option in command for option in media.UPLOAD_INPUT_OPTIONS)


def test_a_downloaded_audio_has_no_source_rate_unless_it_was_read(tmp_path):
    assert audio.DownloadedAudio(path=tmp_path / "a.wav", metadata={}, source_rate=48000).source_rate == 48000
    assert audio.DownloadedAudio(path=tmp_path / "a.wav", metadata={}).source_rate is None


# --- The pipeline hands the rate to the ensemble models -------------------------------------------------

@pytest.mark.parametrize(("ensemble", "rate"), [(True, 48000), (True, None), (False, 48000)])
def test_the_pipeline_gives_an_ensemble_job_the_source_rate(monkeypatch, tmp_path, ensemble, rate):
    import sys

    from app.transcription import pipeline

    calls = []
    midi = b"MThd"

    def fake(kind):
        def run(**options):
            calls.append((kind, options))
            options["output_path"].write_bytes(midi)
        return run

    modules = {
        "app.transcription.piano_transcription": types.SimpleNamespace(transcribe_piano=fake("piano")),
        "app.transcription.ensemble_transcription": types.SimpleNamespace(transcribe_ensemble_file=fake("ensemble")),
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(pipeline, "fetch_audio", lambda output_dir, **options: audio.DownloadedAudio(
        path=tmp_path / "audio.wav", metadata={}, source_rate=rate))
    monkeypatch.setattr(pipeline, "check_midi", lambda path: None)

    pipeline.transcribe_source(source_url=None, output_dir=tmp_path, upload_path=tmp_path / "u", ensemble=ensemble)

    [(kind, options)] = calls
    assert kind == ("ensemble" if ensemble else "piano")
    assert options["audio_path"] == tmp_path / "audio.wav"
    # Only ensemble jobs need the source's rate, for the drums.
    assert options.get("source_rate") == (rate if ensemble else None)
