"""Immutable byte storage, not provenance or audit approval.

Caller must authorize project access before invoking this internal adapter. SHA256 is
computed over actual bytes; S3 ETag is never treated as the content digest.
"""

import hashlib
import re
from dataclasses import dataclass
from tempfile import SpooledTemporaryFile
from typing import BinaryIO
from uuid import UUID

from botocore.exceptions import ClientError


class IntegrityError(ValueError):
    """Stored/uploaded bytes do not match their declared address."""


class ObjectTooLarge(ValueError):
    """Byte limit exceeded before publishing content."""


@dataclass(frozen=True)
class ObjectReceipt:
    digest: str
    size_bytes: int


class S3Objects:
    """Bounded single-object transport; no delete, public URL or overwrite interface."""

    def __init__(self, client, bucket: str, *, max_bytes: int):
        if not bucket or not 0 < max_bytes <= 5 * 1024**3:
            raise ValueError("bucket and positive single-PUT limit required")
        self.client = client
        self.bucket = bucket
        self.max_bytes = max_bytes

    def probe(self) -> None:
        """只读验证目标 bucket 当前可访问；失败交给调用方降级状态。"""
        self.client.head_bucket(Bucket=self.bucket)

    @staticmethod
    def key(project_id: UUID, digest: str) -> str:
        if not isinstance(project_id, UUID) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise ValueError("UUID and SHA256 digest required")
        return f"projects/{project_id}/sha256/{digest[7:]}"

    def put(self, project_id: UUID, digest: str, source: BinaryIO) -> ObjectReceipt:
        key = self.key(project_id, digest)
        with SpooledTemporaryFile(max_size=1024 * 1024) as staged:
            size = 0
            hasher = hashlib.sha256()
            while chunk := source.read(min(65536, self.max_bytes - size + 1)):
                size += len(chunk)
                if size > self.max_bytes:
                    raise ObjectTooLarge("artifact exceeds configured byte limit")
                hasher.update(chunk)
                staged.write(chunk)
            if "sha256:" + hasher.hexdigest() != digest:
                raise IntegrityError("upload digest mismatch")
            staged.seek(0)
            try:
                self.client.put_object(
                    Bucket=self.bucket,
                    Key=key,
                    Body=staged,
                    ContentLength=size,
                    ContentType="application/octet-stream",
                    IfNoneMatch="*",
                )
            except ClientError as exc:
                if exc.response["ResponseMetadata"]["HTTPStatusCode"] != 412:
                    raise
                # Existing content is verified, not blindly accepted as an idempotent success.
            stored = self.read(project_id, digest)
            if len(stored) != size:
                raise IntegrityError("stored size mismatch")
        return ObjectReceipt(digest, size)

    def read(self, project_id: UUID, digest: str) -> bytes:
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=self.key(project_id, digest))
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "NoSuchKey":
                raise FileNotFoundError("artifact bytes not found") from exc
            raise
        body = response["Body"]
        try:
            if response["ContentLength"] > self.max_bytes:
                raise ObjectTooLarge("stored artifact exceeds configured byte limit")
            data = body.read(self.max_bytes + 1)
            if len(data) > self.max_bytes:
                raise ObjectTooLarge("stored artifact exceeds configured byte limit")
            if (
                len(data) != response["ContentLength"]
                or "sha256:" + hashlib.sha256(data).hexdigest() != digest
            ):
                raise IntegrityError("stored digest mismatch")
            return data
        finally:
            body.close()
