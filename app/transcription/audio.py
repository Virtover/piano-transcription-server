"""Fetching and converting audio. No torch import, so the remote dispatcher
can use it without GPU libraries."""

import json
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from app.transcription.cancellation import TranscriptionCancelled
from app.transcription.device import cpu_thread_count
from app.media import probe_audio
from app.url_safety import ensure_safe_source_url


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


def ffmpeg_thread_count() -> int:
    return cpu_thread_count()


def download_audio(
    source_url: str,
    output_dir: Path,
    cancellation_callback: CancellationCallback | None = None,
) -> DownloadedAudio:
    output_template = str(
        output_dir / "download.%(ext)s"
    )

    safe_url = ensure_safe_source_url(str(source_url))

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
            safe_url,
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

    wav_path = convert_to_wav(
        downloaded_path,
        output_dir,
        cancellation_callback,
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


def convert_to_wav(
    input_path: Path,
    output_dir: Path,
    cancellation_callback: CancellationCallback | None = None,
) -> Path:
    wav_path = output_dir / "audio.wav"

    ffmpeg_threads = ffmpeg_thread_count()
    ffmpeg_command = [
        "ffmpeg",
        "-y",
        "-nostdin",
        # Uploaded files are untrusted: never let ffmpeg open URLs.
        "-protocol_whitelist",
        "file",
        "-i",
        str(input_path),
        "-vn",
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
    return wav_path


def prepare_upload(
    upload_path: Path,
    title: str | None,
    output_dir: Path,
    cancellation_callback: CancellationCallback | None = None,
) -> DownloadedAudio:
    # Checked again here because the worker runs ffmpeg on the file.
    duration = probe_audio(upload_path)
    wav_path = convert_to_wav(
        upload_path,
        output_dir,
        cancellation_callback,
    )
    # The upload is kept until the job ends, so a job moved between local
    # and remote can be converted again.
    return DownloadedAudio(
        path=wav_path,
        metadata={
            "title": title or "Uploaded file",
            "duration": duration,
        },
    )


def format_upload_date(value: str | None) -> str | None:
    if not value or len(value) != 8 or not value.isdigit():
        return None

    return f"{value[:4]}-{value[4:6]}-{value[6:]}"


def remove_audio_files(output_dir: Path, keep_upload: bool = False) -> None:
    """Delete downloaded and converted audio. With keep_upload, the user's
    uploaded file stays for another attempt."""
    if not output_dir.is_dir():
        return
    for path in output_dir.iterdir():
        if not path.is_file():
            continue
        if path.name.startswith("upload."):
            if not keep_upload:
                path.unlink(missing_ok=True)
        elif path.name.startswith("download.") or path.suffix.lower() in AUDIO_FILE_SUFFIXES:
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


def convert_to_flac(
    wav_path: Path,
    output_dir: Path,
    cancellation_callback: CancellationCallback | None = None,
) -> Path:
    """Losslessly compress the converted WAV for upload to remote storage."""
    flac_path = output_dir / "audio.flac"
    command = [
        "ffmpeg",
        "-y",
        "-nostdin",
        "-protocol_whitelist",
        "file",
        "-i",
        str(wav_path),
        "-c:a",
        "flac",
        "-compression_level",
        "5",
        str(flac_path),
    ]
    run_process(command, cancellation_callback=cancellation_callback)
    return flac_path


def fetch_audio(
    output_dir: Path,
    source_url: str | None = None,
    upload_path: Path | None = None,
    upload_title: str | None = None,
    progress_callback: ProgressCallback | None = None,
    metadata_callback: MetadataCallback | None = None,
    cancellation_callback: CancellationCallback | None = None,
) -> DownloadedAudio:
    """Download source_url, or convert upload_path, to 44.1 kHz mono WAV.

    Progress range: 0.03 - 0.05 before the download starts.
    """
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

    if upload_path is not None:
        downloaded = prepare_upload(
            upload_path,
            upload_title,
            output_dir,
            cancellation_callback=cancellation_callback,
        )
    else:
        downloaded = download_audio(
            source_url,
            output_dir,
            cancellation_callback=cancellation_callback,
        )

    if metadata_callback:
        metadata_callback(downloaded.metadata)
    return downloaded


def check_midi(midi_path: Path) -> Path:
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
    return midi_path
