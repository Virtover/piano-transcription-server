"""Local transcription: fetch audio, then run the model on this machine."""

from pathlib import Path

from app.transcription.audio import (  # noqa: F401  (re-exported)
    AUDIO_FILE_SUFFIXES,
    CancellationCallback,
    DownloadedAudio,
    MetadataCallback,
    ProgressCallback,
    check_midi,
    convert_to_wav,
    download_audio,
    fetch_audio,
    format_upload_date,
    prepare_upload,
    remove_audio_files,
    run_process,
)


def transcribe_source(
    source_url: str | None,
    output_dir: Path,
    progress_callback: ProgressCallback | None = None,
    metadata_callback: MetadataCallback | None = None,
    cancellation_callback: CancellationCallback | None = None,
    upload_path: Path | None = None,
    upload_title: str | None = None,
    max_seconds: float | None = None,
) -> Path:
    """Transcribe source_url, or upload_path when it is given, at most about
    max_seconds of it: the duration the job was charged for."""
    from app.transcription.piano_transcription import transcribe_piano

    try:
        downloaded = fetch_audio(
            output_dir,
            source_url=source_url,
            upload_path=upload_path,
            upload_title=upload_title,
            progress_callback=progress_callback,
            metadata_callback=metadata_callback,
            cancellation_callback=cancellation_callback,
            max_seconds=max_seconds,
        )

        midi_path = output_dir / "transcription.mid"

        transcribe_piano(
            audio_path=downloaded.path,
            output_path=midi_path,
            progress_callback=progress_callback,
            cancellation_callback=cancellation_callback,
        )

        check_midi(midi_path)

        if progress_callback:
            progress_callback(1.0)

        return midi_path
    finally:
        # The job lifecycle removes the upload once the job ends.
        remove_audio_files(output_dir, keep_upload=True)
