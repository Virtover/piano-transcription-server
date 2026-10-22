import asyncio
import base64
import importlib.util
from pathlib import Path

import pytest

# Skipped like the model imports below when the worker requirements are missing.
np = pytest.importorskip("numpy")

# Needs the RunPod handler image (requirements.runpod.txt).
pytest.importorskip("runpod")
soundfile = pytest.importorskip("soundfile")
pytest.importorskip("transkun")

from app.config import settings  # noqa: E402
from app.transcription import piano_transcription  # noqa: E402

STORAGE = "http://storage.test:9000"
HANDLER_PATH = Path(__file__).resolve().parent.parent / "runpod" / "handler.py"


@pytest.fixture(scope="module")
def handler():
    # Loaded from its file: the directory name `runpod` is also the SDK's package name.
    spec = importlib.util.spec_from_file_location("runpod_handler", HANDLER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def requests_in(handler, monkeypatch):
    """Serve generated FLAC audio instead of downloading it, and record batch sizes."""
    monkeypatch.setattr(settings.outsource, "storage_endpoint", STORAGE)
    sizes = []
    original = piano_transcription._transcribe_audio_batch

    def recording(model, requests):
        sizes.append(len(requests))
        return original(model, requests)

    monkeypatch.setattr(piano_transcription, "_transcribe_audio_batch", recording)

    def fake_download(url, destination, max_bytes):
        if url.endswith("broken.flac"):
            destination.write_bytes(b"not audio")
            return 9
        seconds = 3
        t = np.arange(seconds * 44100) / 44100
        tone = (0.3 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
        soundfile.write(destination, tone, 44100, format="FLAC")
        return destination.stat().st_size

    monkeypatch.setattr(handler, "download", fake_download)
    return sizes


def job(name="audio.flac", job_id="job-1"):
    return {"id": f"test-{job_id}", "input": {"audio_url": f"{STORAGE}/bucket/{name}", "job_id": job_id}}


async def run_all(handler, jobs):
    return await asyncio.gather(*(handler.handler(item) for item in jobs))


def test_concurrent_requests_share_one_batch(handler, requests_in):
    results = asyncio.run(run_all(handler, [job(job_id=f"job-{index}") for index in range(3)]))

    for result in results:
        assert "error" not in result, result
        assert base64.b64decode(result["midi_base64"]).startswith(b"MThd")
        assert result["timing"]["audio_seconds"] == pytest.approx(3, abs=0.01)
    assert requests_in == [3]


def test_concurrency_follows_batch_size(handler):
    assert handler.concurrency(1) == handler.BATCHER.batch_size
    assert handler.EXECUTOR._max_workers >= handler.BATCHER.batch_size


def test_invalid_requests_fail_without_affecting_others(handler, requests_in):
    results = asyncio.run(
        run_all(
            handler,
            [
                job(job_id="good"),
                {"id": "test-bad", "input": {"audio_url": "https://evil.example/x", "job_id": "bad"}},
                job(name="broken.flac", job_id="broken"),
            ],
        )
    )

    assert base64.b64decode(results[0]["midi_base64"]).startswith(b"MThd")
    assert results[1] == {"error": "The audio URL is not on the configured storage host"}
    assert results[2] == {"error": "The audio file cannot be decoded"}
    assert requests_in == [1]
