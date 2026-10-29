"""Where a job's lasting files are kept: the uploaded input, the video info
the API saved, and the MIDI result.

JOB_STORAGE=local keeps them in the job's folder on the data volume, which
the services of one machine share. JOB_STORAGE=s3 keeps them in the
bucket of STORAGE_ENDPOINT and STORAGE_BUCKET under jobs/<job_id>/, so API replicas and workers on other
machines reach them. Each attempt at a job then works in a scratch folder of
its own, under work/ on the data volume, removed when it is done: a job moved
to another backend on the same machine cannot lose the files of its next
attempt.

A completed job's `result` field says where its MIDI file is: a path on the
data volume, or `s3:<key>`. It is read by its form, not by JOB_STORAGE, so
results stored before a change of JOB_STORAGE stay downloadable.
"""

import logging
import os
import shutil
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Protocol

from app.config import settings


logger = logging.getLogger(__name__)

PREFIX = "jobs/"
S3_REFERENCE = "s3:"
RESULT_FILE_NAME = "transcription.mid"
# A signed result link is used at once, by the app that asked for it.
RESULT_LINK_SECONDS = 300
# Saved with each object, so the video info keeps the time it was checked,
# which decides whether its signed audio links are still fresh.
MTIME_METADATA = "mtime"


class StorageUnavailable(Exception):
    """The job's files cannot be read or written now."""


WORK_FOLDER = "work"


def job_dir(job_id: str) -> Path:
    """The job's folder with local storage."""
    return Path(settings.service.data_dir) / "jobs" / job_id


def workspace_job_id(folder: Path) -> str:
    """The job a scratch folder, named <job_id>.<attempt>, belongs to."""
    return folder.name.split(".", 1)[0]


class JobStorage(Protocol):
    def workspace(self, job_id: str) -> Path:
        """The folder this attempt at the job works in. Not created."""

    def save(self, job_id: str, path: Path) -> None:
        """Keep a file of the workspace, under its name, for any machine."""

    def fetch_inputs(self, job_id: str, folder: Path) -> None:
        """Bring the job's kept files into the workspace."""

    def save_result(self, job_id: str, path: Path) -> str:
        """Keep the MIDI result and return its reference for the job."""

    def discard_inputs(self, job_id: str, folder: Path) -> None:
        """Delete everything of the job but the result, after its last attempt."""

    def release(self, folder: Path) -> None:
        """This attempt is done with the workspace: free its scratch space."""

    def delete(self, job_id: str) -> None:
        """Delete all of the job's files. Never raises."""

    def names(self, job_id: str) -> list[str] | None:
        """The job's kept file names, or None without any."""

    def job_ids(self) -> Iterator[str]:
        """IDs of every job with kept files, for cleanup."""


class LocalJobStorage:
    """Everything stays in the job folder, as with a single machine."""

    def workspace(self, job_id: str) -> Path:
        return job_dir(job_id)

    def save(self, job_id: str, path: Path) -> None:
        pass

    def fetch_inputs(self, job_id: str, folder: Path) -> None:
        pass

    def save_result(self, job_id: str, path: Path) -> str:
        return str(path)

    def discard_inputs(self, job_id: str, folder: Path) -> None:
        from app.transcription.audio import remove_audio_files

        remove_audio_files(folder)

    def release(self, folder: Path) -> None:
        pass

    def delete(self, job_id: str) -> None:
        shutil.rmtree(job_dir(job_id), ignore_errors=True)

    def names(self, job_id: str) -> list[str] | None:
        folder = job_dir(job_id)
        return sorted(path.name for path in folder.iterdir()) if folder.is_dir() else None

    def job_ids(self) -> Iterator[str]:
        folder = Path(settings.service.data_dir) / "jobs"
        if folder.is_dir():
            yield from (path.name for path in folder.iterdir() if path.is_dir())


class S3JobStorage:
    """Kept files in the bucket under jobs/<job_id>/; the folder is scratch."""

    def __init__(self, client: Any, bucket: str):
        self.client = client
        self.bucket = bucket

    @classmethod
    def from_settings(cls, config=None) -> "S3JobStorage":
        from app.outsource.storage import s3_client, storage_configured

        config = config or settings
        if not storage_configured(config):
            raise StorageUnavailable("JOB_STORAGE=s3 needs the outsourcing storage settings and secrets")
        return cls(s3_client(config), config.storage.bucket)

    @staticmethod
    def key(job_id: str, name: str) -> str:
        return f"{PREFIX}{job_id}/{name}"

    def _keys(self, prefix: str) -> Iterator[str]:
        token = None
        while True:
            arguments = {"Bucket": self.bucket, "Prefix": prefix}
            if token:
                arguments["ContinuationToken"] = token
            response = self.client.list_objects_v2(**arguments)
            yield from (item["Key"] for item in response.get("Contents", []))
            if not response.get("IsTruncated"):
                return
            token = response.get("NextContinuationToken")

    def workspace(self, job_id: str) -> Path:
        return Path(settings.service.data_dir) / WORK_FOLDER / f"{job_id}.{uuid.uuid4().hex[:12]}"

    def save(self, job_id: str, path: Path) -> None:
        try:
            self.client.upload_file(
                str(path),
                self.bucket,
                self.key(job_id, path.name),
                ExtraArgs={"Metadata": {MTIME_METADATA: str(path.stat().st_mtime)}},
            )
        except Exception as error:
            raise StorageUnavailable(f"Could not store {path.name} ({type(error).__name__})") from error

    def fetch_inputs(self, job_id: str, folder: Path) -> None:
        folder.mkdir(parents=True, exist_ok=True)
        try:
            for key in self._keys(self.key(job_id, "")):
                name = key.rsplit("/", 1)[-1]
                destination = folder / name
                if name == RESULT_FILE_NAME or destination.exists():
                    continue
                self.client.download_file(self.bucket, key, str(destination))
                metadata = self.client.head_object(Bucket=self.bucket, Key=key).get("Metadata", {})
                if MTIME_METADATA in metadata:
                    mtime = float(metadata[MTIME_METADATA])
                    os.utime(destination, (mtime, mtime))
        except Exception as error:
            raise StorageUnavailable(f"Could not read the job's files ({type(error).__name__})") from error

    def save_result(self, job_id: str, path: Path) -> str:
        key = self.key(job_id, RESULT_FILE_NAME)
        try:
            self.client.upload_file(
                str(path), self.bucket, key, ExtraArgs={"ContentType": "audio/midi"}
            )
        except Exception as error:
            raise StorageUnavailable(f"Could not store the MIDI result ({type(error).__name__})") from error
        return f"{S3_REFERENCE}{key}"

    def discard_inputs(self, job_id: str, folder: Path) -> None:
        try:
            self._delete(key for key in self._keys(self.key(job_id, "")) if not key.endswith(f"/{RESULT_FILE_NAME}"))
        except Exception as error:
            # The bucket's automatic deletion removes them later.
            logger.warning("Could not delete the stored inputs of job %s: %s", job_id, type(error).__name__)

    def release(self, folder: Path) -> None:
        shutil.rmtree(folder, ignore_errors=True)

    def delete(self, job_id: str) -> None:
        # Files kept on this machine before JOB_STORAGE changed.
        shutil.rmtree(job_dir(job_id), ignore_errors=True)
        try:
            self._delete(self._keys(self.key(job_id, "")))
        except Exception as error:
            logger.warning("Could not delete the stored files of job %s: %s", job_id, type(error).__name__)

    def _delete(self, keys) -> None:
        batch = []
        for key in keys:
            batch.append({"Key": key})
            # The most one request deletes.
            if len(batch) == 1000:
                self.client.delete_objects(Bucket=self.bucket, Delete={"Objects": batch, "Quiet": True})
                batch = []
        if batch:
            self.client.delete_objects(Bucket=self.bucket, Delete={"Objects": batch, "Quiet": True})

    def names(self, job_id: str) -> list[str] | None:
        names = sorted(key.rsplit("/", 1)[-1] for key in self._keys(self.key(job_id, "")))
        return names or None

    def job_ids(self) -> Iterator[str]:
        seen = None
        for key in self._keys(PREFIX):
            job_id = key[len(PREFIX):].split("/", 1)[0]
            if job_id and job_id != seen:
                seen = job_id
                yield job_id

    def result_exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
        except Exception:
            return False
        return True

    def result_link(self, key: str) -> str:
        try:
            return self.client.generate_presigned_url(
                "get_object",
                Params={
                    "Bucket": self.bucket,
                    "Key": key,
                    "ResponseContentType": "audio/midi",
                    "ResponseContentDisposition": f'attachment; filename="{RESULT_FILE_NAME}"',
                },
                ExpiresIn=RESULT_LINK_SECONDS,
            )
        except Exception as error:
            raise StorageUnavailable(f"Could not sign the result link ({type(error).__name__})") from error


_storage: JobStorage | None = None
_s3: S3JobStorage | None = None


def s3() -> S3JobStorage:
    """The bucket, for results stored there, whatever JOB_STORAGE is now."""
    global _s3
    if _s3 is None:
        _s3 = S3JobStorage.from_settings()
    return _s3


def storage() -> JobStorage:
    global _storage
    if _storage is None:
        _storage = s3() if settings.jobs.storage == "s3" else LocalJobStorage()
    return _storage


def reset() -> None:
    """Forget the chosen storage, as when settings change in tests."""
    global _storage, _s3
    _storage = None
    _s3 = None


def s3_key(reference: str) -> str | None:
    return reference[len(S3_REFERENCE):] if reference.startswith(S3_REFERENCE) else None


def delete_job_files(job_id: str) -> None:
    """Delete a job's kept files. Never raises. Scratch folders are removed
    by the attempts that made them, or by cleanup after a crash."""
    try:
        storage().delete(job_id)
    except Exception:
        logger.exception("Could not delete the files of job %s", job_id)
