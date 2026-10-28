"""Fetching and converting audio. No torch import, so the remote dispatcher
can use it without GPU libraries."""

import json
import logging
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from app import video_sites
from app.errors import SourceError, UserFacingError
from app.transcription.cancellation import TranscriptionCancelled
from app.transcription.device import cpu_thread_count
from app.media import UPLOAD_INPUT_OPTIONS, probe_audio
from app.url_safety import YT_DLP_SAFETY_OPTIONS, ensure_safe_source_url
from app.video_info import (  # noqa: F401  (format_upload_date is re-exported)
    VIDEO_INFO_FILE_NAME,
    format_upload_date,
    saved_video_info,
    video_metadata,
)


logger = logging.getLogger(__name__)


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
# Audio is converted up to the paid duration plus this, so a file or video
# whose stated length is too short cannot be transcribed for longer.
DURATION_TOLERANCE = 5.0
# yt-dlp stops a download above this size. A long video in the "best"
# fallback format is large, so this only stops runaway downloads.
MAX_DOWNLOAD_SIZE = "4G"
# Partial files yt-dlp leaves while it downloads.
PARTIAL_SUFFIXES = {".part", ".ytdl", ".temp"}
# How long a worker waits for its turn to download from a video site. It
# stops waiting sooner if the job is cancelled.
SITE_WAIT = 60 * 60


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
    max_seconds: float | None = None,
) -> DownloadedAudio:
    """Download with the info the API saved when it checked the video, so
    the site is asked once per job. Without recent info, or if its links no
    longer work, yt-dlp asks the site once more: never after the site
    refused this server, or said the video cannot be read."""
    safe_url = ensure_safe_source_url(str(source_url))
    site = video_sites.site_of(safe_url)
    info_path = saved_video_info(output_dir)

    result = None
    if info_path is not None:
        try:
            result = download_from_site(
                site, ["--load-info-json", str(info_path)], output_dir, cancellation_callback, paced=False
            )
        except SourceError as error:
            # Only an unexplained failure, such as expired audio links.
            if error.code != SourceError.code:
                raise
            logger.warning("Download from the saved video info failed, asking the site again: %s", error)
            info_path.unlink(missing_ok=True)
            remove_downloads(output_dir)
    if result is None:
        result = download_from_site(site, ["--", safe_url], output_dir, cancellation_callback)

    json_lines = [
        line
        for line in result.stdout.splitlines()
        if line.strip()
    ]

    if not json_lines:
        raise UserFacingError(
            "yt-dlp did not return video metadata"
        )

    try:
        info = json.loads(json_lines[-1])
    except json.JSONDecodeError as error:
        raise UserFacingError(
            "yt-dlp returned invalid video metadata"
        ) from error

    title = info.get("title")

    if not title:
        raise UserFacingError(
            "yt-dlp did not return a video title"
        )

    downloaded_path = None

    for path in sorted(output_dir.glob("download.*")):
        if path.suffix.lower() not in PARTIAL_SUFFIXES:
            downloaded_path = path
            break

    if downloaded_path is None:
        raise UserFacingError(
            "yt-dlp did not produce an audio file"
        )

    wav_path = convert_to_wav(
        downloaded_path,
        output_dir,
        cancellation_callback,
        max_seconds,
    )

    downloaded_path.unlink()

    return DownloadedAudio(
        path=wav_path,
        metadata=video_metadata(info, source_url),
    )


def download_from_site(
    site: str,
    source: list[str],
    output_dir: Path,
    cancellation_callback: CancellationCallback | None = None,
    paced: bool = True,
) -> subprocess.CompletedProcess[str]:
    """run_yt_dlp once site may be contacted, in one of its download places;
    see app.video_sites. Its failures become SourceErrors that say what went
    wrong."""
    with video_sites.access(site, SITE_WAIT, paced=paced, download=True, cancelled=cancellation_callback):
        try:
            return run_yt_dlp(source, output_dir, cancellation_callback)
        except subprocess.CalledProcessError as error:
            raise video_sites.failure(site, error) from error


def run_yt_dlp(
    source: list[str],
    output_dir: Path,
    cancellation_callback: CancellationCallback | None = None,
) -> subprocess.CompletedProcess[str]:
    """Download source, a URL or saved info, to output_dir/download.*."""
    return run_process(
        [
            "yt-dlp",
            *YT_DLP_SAFETY_OPTIONS,
            *video_sites.YT_DLP_SITE_OPTIONS,
            "--format",
            "bestaudio/best",
            "--retries",
            "2",
            "--fragment-retries",
            "3",
            "--retry-sleep",
            "http:exp=2:30",
            "--retry-sleep",
            "fragment:exp=1:10",
            "--max-filesize",
            MAX_DOWNLOAD_SIZE,
            # Conversion is left to convert_to_wav, which never lets ffmpeg
            # open anything but the downloaded file.
            "--print-json",
            "-o",
            str(output_dir / "download.%(ext)s"),
            *source,
        ],
        cancellation_callback=cancellation_callback,
    )


def remove_downloads(output_dir: Path) -> None:
    for path in output_dir.glob("download.*"):
        path.unlink(missing_ok=True)


def convert_to_wav(
    input_path: Path,
    output_dir: Path,
    cancellation_callback: CancellationCallback | None = None,
    max_seconds: float | None = None,
    upload: bool = False,
) -> Path:
    """Convert to 44.1 kHz mono WAV, stopping DURATION_TOLERANCE past
    max_seconds, the duration the job was charged for. An upload may only
    be read by the demuxers of the formats uploads may have; a download
    can be in any format a site serves."""
    wav_path = output_dir / "audio.wav"

    ffmpeg_threads = ffmpeg_thread_count()
    ffmpeg_command = [
        "ffmpeg",
        "-y",
        "-nostdin",
        # Uploaded files are untrusted: never let ffmpeg open URLs.
        *(UPLOAD_INPUT_OPTIONS if upload else ["-protocol_whitelist", "file"]),
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
    if max_seconds is not None:
        ffmpeg_command.extend(["-t", f"{max_seconds + DURATION_TOLERANCE:.3f}"])
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
    max_seconds: float | None = None,
) -> DownloadedAudio:
    # The format is checked again here because the worker runs ffmpeg on the
    # file. The API already measured its length; conversion stops there.
    duration = probe_audio(upload_path, measure=False)
    if max_seconds is not None:
        duration = min(duration, max_seconds)
    wav_path = convert_to_wav(
        upload_path,
        output_dir,
        cancellation_callback,
        max_seconds,
        upload=True,
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


def remove_audio_files(output_dir: Path, keep_upload: bool = False) -> None:
    """Delete downloaded and converted audio. With keep_upload, the user's
    uploaded file, or the saved video info, stays for another attempt."""
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
        elif path.name == VIDEO_INFO_FILE_NAME and not keep_upload:
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
    max_seconds: float | None = None,
) -> DownloadedAudio:
    """Download source_url, or convert upload_path, to 44.1 kHz mono WAV,
    at most about max_seconds long: the duration the job was charged for.

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
            max_seconds=max_seconds,
        )
    else:
        downloaded = download_audio(
            source_url,
            output_dir,
            cancellation_callback=cancellation_callback,
            max_seconds=max_seconds,
        )

    if metadata_callback:
        metadata_callback(downloaded.metadata)
    return downloaded


def check_midi(midi_path: Path) -> Path:
    if not midi_path.exists():
        raise UserFacingError(
            "Piano transcription did not "
            "produce a MIDI file"
        )

    if midi_path.stat().st_size == 0:
        raise UserFacingError(
            "Piano transcription produced "
            "an empty MIDI file"
        )
    with midi_path.open("rb") as file:
        if file.read(4) != b"MThd":
            raise UserFacingError("Piano transcription produced an invalid MIDI file")
    return midi_path
