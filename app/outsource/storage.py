"""S3-compatible storage (Cloudflare R2, MinIO, AWS S3) for handing audio to
the remote provider."""

import logging
import uuid
from pathlib import Path
from typing import Any, Protocol

from app.config import settings
from app.outsource.provider import RemoteFailed


logger = logging.getLogger(__name__)


class AudioStorage(Protocol):
    def upload(self, path: Path) -> str:
        """Upload a file under a new random key and return the key."""

    def presigned_url(self, key: str, expires_seconds: int) -> str:
        """A temporary GET URL for the object."""

    def delete(self, key: str) -> None:
        """Delete the object. Must not raise."""


class S3Storage:
    def __init__(self, client: Any, bucket: str):
        self.client = client
        self.bucket = bucket

    @classmethod
    def from_settings(cls, config=None) -> "S3Storage":
        import boto3
        from botocore.config import Config

        config = config or settings
        outsource = config.outsource
        access_key = config.secrets.outsource_storage_access_key
        secret_key = config.secrets.outsource_storage_secret_key
        if not (outsource.storage_endpoint and outsource.storage_bucket and access_key and secret_key):
            raise RemoteFailed("Outsourcing storage is not configured")
        client = boto3.client(
            "s3",
            endpoint_url=outsource.storage_endpoint,
            region_name=outsource.storage_region,
            aws_access_key_id=access_key.get_secret_value(),
            aws_secret_access_key=secret_key.get_secret_value(),
            # Path-style URLs keep presigned URLs on the endpoint host, which
            # is the only host the remote handler downloads from.
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"}, retries={"max_attempts": 3}),
        )
        return cls(client, outsource.storage_bucket)

    def upload(self, path: Path) -> str:
        key = f"audio/{uuid.uuid4().hex}{path.suffix}"
        try:
            self.client.upload_file(str(path), self.bucket, key)
        except Exception as error:
            raise RemoteFailed(f"Could not upload audio to storage ({type(error).__name__})") from error
        return key

    def presigned_url(self, key: str, expires_seconds: int) -> str:
        try:
            return self.client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self.bucket, "Key": key},
                ExpiresIn=expires_seconds,
            )
        except Exception as error:
            raise RemoteFailed(f"Could not create a download URL ({type(error).__name__})") from error

    def delete(self, key: str) -> None:
        try:
            self.client.delete_object(Bucket=self.bucket, Key=key)
        except Exception as error:
            logger.warning("Could not delete %s from storage: %s", key, type(error).__name__)
