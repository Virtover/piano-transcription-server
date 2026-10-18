import shutil
import subprocess
import wave

import pytest

# Needs the worker dependencies (requirements.worker.txt) and ffmpeg.
pipeline = pytest.importorskip("app.transcription.pipeline")
pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg is not installed")

from app.media import UnsupportedMedia  # noqa: E402


def ffmpeg(*arguments):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *arguments], check=True)


@pytest.mark.parametrize("source", ["stereo.mp3", "clip.mp4"])
def test_upload_is_converted_to_mono_44100_wav(tmp_path, source):
    upload = tmp_path / "upload.input"
    if source == "clip.mp4":
        ffmpeg(
            "-f", "lavfi", "-i", "color=size=64x64:duration=3",
            "-f", "lavfi", "-i", "sine=sample_rate=48000:duration=3",
            "-shortest", "-f", "mp4", str(upload),
        )
    else:
        ffmpeg("-f", "lavfi", "-i", "sine=sample_rate=22050:duration=3", "-ac", "2", "-f", "mp3", str(upload))

    audio = pipeline.prepare_upload(upload, "My Song", tmp_path)

    assert audio.path == tmp_path / "audio.wav"
    assert audio.metadata["title"] == "My Song"
    assert audio.metadata["duration"] == pytest.approx(3, abs=0.1)
    with wave.open(str(audio.path)) as wav:
        assert (wav.getframerate(), wav.getnchannels()) == (44100, 1)
        assert wav.getnframes() / 44100 == pytest.approx(3, abs=0.1)
    # Kept until the job ends, for a retry on another backend.
    assert upload.exists()


def test_upload_without_title_gets_default(tmp_path):
    upload = tmp_path / "upload.input"
    ffmpeg("-f", "lavfi", "-i", "sine=duration=1", "-f", "wav", str(upload))

    assert pipeline.prepare_upload(upload, None, tmp_path).metadata["title"] == "Uploaded file"


def test_unsupported_upload_is_not_converted(tmp_path):
    upload = tmp_path / "upload.input"
    upload.write_text("#EXTM3U\n#EXTINF:2,\nhttp://169.254.169.254/latest\n#EXT-X-ENDLIST\n")

    with pytest.raises(UnsupportedMedia):
        pipeline.prepare_upload(upload, "x", tmp_path)
    assert not (tmp_path / "audio.wav").exists()


def test_upload_is_removed_with_other_audio_files(tmp_path):
    for name in ["upload.input", "download.webm", "audio.wav", "transcription.mid"]:
        (tmp_path / name).write_bytes(b"x")

    pipeline.remove_audio_files(tmp_path, keep_upload=True)
    assert sorted(path.name for path in tmp_path.iterdir()) == ["transcription.mid", "upload.input"]

    pipeline.remove_audio_files(tmp_path)
    assert [path.name for path in tmp_path.iterdir()] == ["transcription.mid"]
