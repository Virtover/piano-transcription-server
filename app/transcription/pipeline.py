import json
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from app.transcription.cancellation import TranscriptionCancelled
from app.transcription.device import automatic_cpu_threads
from app.transcription.piano_transcription import (
    transcribe_piano,
)


ProgressCallback = Callable[[float], None]
MetadataCallback = Callable[[dict[str, Any]], None]
CancellationCallback = Callable[[], bool]


AUDIO_FILE_SUFFIXES = {
    ".aac",
    ".flac",
    ".m4a",
    ".mp3",
    ".ogg",
    ".opus",
    ".wav",
    ".webm",
}


@dataclass
class DownloadedAudio:
    path: Path
    metadata: dict[str, Any]


def ffmpeg_thread_count() -> int | None:
    value = os.environ.get("WORKER_THREADS", "auto").strip().lower()
    if value == "auto":
        return automatic_cpu_threads()
    try:
        thread_count = int(value)
    except ValueError as error:
        raise ValueError("WORKER_THREADS must be an integer or 'auto'") from error
    if thread_count < 1:
        raise ValueError("WORKER_THREADS must be at least 1")
    return thread_count


def download_audio(
    source_url: str,
    output_dir: Path,
    cancellation_callback: CancellationCallback | None = None,
) -> DownloadedAudio:
    output_template = str(
        output_dir / "download.%(ext)s"
    )

    result = run_process(
        [
            "yt-dlp",
            "--no-playlist",
            "--js-runtimes",
            "deno",
            "--extractor-args",
            "youtube:player_client=web,android",
            "--format",
            "bestaudio/best",
            "--socket-timeout",
            "30",
            "--retries",
            "3",
            "--fragment-retries",
            "3",
            "--extractor-retries",
            "3",
            "--print-json",
            "-x",
            "--audio-format",
            "wav",
            "-o",
            output_template,
            source_url,
        ],
        cancellation_callback=cancellation_callback,
    )

    json_lines = [
        line
        for line in result.stdout.splitlines()
        if line.strip()
    ]

    if not json_lines:
        raise RuntimeError(
            "yt-dlp did not return video metadata"
        )

    try:
        info = json.loads(json_lines[-1])
    except json.JSONDecodeError as error:
        raise RuntimeError(
            "yt-dlp returned invalid video metadata"
        ) from error

    title = info.get("title")

    if not title:
        raise RuntimeError(
            "yt-dlp did not return a video title"
        )

    downloaded_path = None

    for path in output_dir.glob("download.*"):
        if path.suffix.lower() == ".wav":
            downloaded_path = path
            break

    if downloaded_path is None:
        raise RuntimeError(
            "yt-dlp did not produce a WAV file"
        )

    wav_path = output_dir / "audio.wav"

    ffmpeg_threads = ffmpeg_thread_count()
    ffmpeg_command = [
        "ffmpeg",
        "-y",
        "-i",
        str(downloaded_path),
        "-ar",
        "44100",
        "-ac",
        "1",
    ]
    if ffmpeg_threads is not None:
        ffmpeg_command.extend(["-threads", str(ffmpeg_threads)])
    ffmpeg_command.append(str(wav_path))

    run_process(
        ffmpeg_command,
        cancellation_callback=cancellation_callback,
    )

    downloaded_path.unlink()

    return DownloadedAudio(
        path=wav_path,
        metadata={
            "title": title,
            "author": info.get("uploader") or info.get("channel"),
            "channel": info.get("channel"),
            "channel_id": info.get("channel_id"),
            "channel_url": info.get("channel_url"),
            "upload_date": format_upload_date(info.get("upload_date")),
            "duration": info.get("duration"),
            "thumbnail": info.get("thumbnail"),
            "webpage_url": info.get("webpage_url") or source_url,
            "view_count": info.get("view_count"),
            "like_count": info.get("like_count"),
        },
    )


def format_upload_date(value: str | None) -> str | None:
    if not value or len(value) != 8 or not value.isdigit():
        return None

    return f"{value[:4]}-{value[4:6]}-{value[6:]}"


def remove_audio_files(output_dir: Path) -> None:
    for path in output_dir.iterdir():
        if (
            path.is_file()
            and (
                path.name.startswith("download.")
                or path.suffix.lower() in AUDIO_FILE_SUFFIXES
            )
        ):
            path.unlink(missing_ok=True)


def run_process(
    command: list[str],
    cancellation_callback: CancellationCallback | None = None,
) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=os.name != "nt",
    )

    while True:
        if cancellation_callback and cancellation_callback():
            if os.name == "nt":
                process.terminate()
            else:
                os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                if os.name == "nt":
                    process.kill()
                else:
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            raise TranscriptionCancelled()

        try:
            stdout, stderr = process.communicate(timeout=0.5)
            break
        except subprocess.TimeoutExpired:
            continue

    if process.returncode:
        raise subprocess.CalledProcessError(
            process.returncode,
            command,
            output=stdout,
            stderr=stderr,
        )

    return subprocess.CompletedProcess(
        command,
        process.returncode,
        stdout,
        stderr,
    )


def transcribe_source(
    source_url: str,
    output_dir: Path,
    progress_callback: ProgressCallback | None = None,
    metadata_callback: MetadataCallback | None = None,
    cancellation_callback: CancellationCallback | None = None,
) -> Path:
    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    if cancellation_callback and cancellation_callback():
        raise TranscriptionCancelled()

    if progress_callback:
        progress_callback(0.03)
        time.sleep(2)
        progress_callback(0.05)

    try:
        downloaded = download_audio(
            source_url,
            output_dir,
            cancellation_callback=cancellation_callback,
        )

        if metadata_callback:
            metadata_callback(downloaded.metadata)

        midi_path = (
            output_dir
            / "transcription.mid"
        )

        transcribe_piano(
            audio_path=downloaded.path,
            output_path=midi_path,
            progress_callback=progress_callback,
            cancellation_callback=cancellation_callback,
        )

        if not midi_path.exists():
            raise RuntimeError(
                "Piano transcription did not "
                "produce a MIDI file"
            )

        if midi_path.stat().st_size == 0:
            raise RuntimeError(
                "Piano transcription produced "
                "an empty MIDI file"
            )

        if progress_callback:
            progress_callback(1.0)

        return midi_path
    finally:
        remove_audio_files(output_dir)