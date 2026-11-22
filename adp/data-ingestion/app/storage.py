from __future__ import annotations

import mimetypes
import re
import time
from pathlib import Path
from typing import Protocol

from .config import (
    S3_ACCESS_KEY,
    S3_BUCKET,
    S3_ENDPOINT_URL,
    S3_FORCE_PATH_STYLE,
    S3_PREFIX,
    S3_REGION,
    S3_SECRET_KEY,
    STORAGE_BACKEND,
    STORAGE_DIR,
)


class StorageBackend(Protocol):
    name: str

    def ensure_ready(self) -> None: ...

    def save_bytes(self, kind: str, run_id: str, filename: str, content: bytes) -> str: ...

    def save_text(self, kind: str, run_id: str, filename: str, content: str) -> str: ...

    def read_bytes(self, uri: str) -> bytes: ...

    def exists(self, uri: str) -> bool: ...

    def info(self) -> dict[str, str]: ...


class LocalStorage:
    name = "local"

    def ensure_ready(self) -> None:
        STORAGE_DIR.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _scope(space_id: str) -> Path:
        safe_space_id = re.sub(r"[^a-zA-Z0-9_-]", "-", space_id).strip("-")
        if not safe_space_id or safe_space_id == "default":
            return Path()
        return Path("spaces") / safe_space_id

    def save_bytes(
        self,
        kind: str,
        run_id: str,
        filename: str,
        content: bytes,
        space_id: str = "default",
    ) -> str:
        target = STORAGE_DIR / self._scope(space_id) / kind / run_id / Path(filename).name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        return str(target.resolve())

    def save_text(
        self,
        kind: str,
        run_id: str,
        filename: str,
        content: str,
        space_id: str = "default",
    ) -> str:
        return self.save_bytes(
            kind,
            run_id,
            filename,
            content.encode("utf-8"),
            space_id=space_id,
        )

    def read_bytes(self, uri: str) -> bytes:
        path = Path(uri.removeprefix("file://"))
        if not path.is_absolute():
            path = STORAGE_DIR / path
        return path.resolve().read_bytes()

    def exists(self, uri: str) -> bool:
        path = Path(uri.removeprefix("file://"))
        if not path.is_absolute():
            path = STORAGE_DIR / path
        return path.resolve().is_file()

    def info(self) -> dict[str, str]:
        return {
            "backend": self.name,
            "root": str(STORAGE_DIR),
        }


class S3Storage:
    name = "s3"

    def __init__(self) -> None:
        self.client = None

    def _client(self):
        if self.client is not None:
            return self.client
        try:
            import boto3
            from botocore.config import Config
        except ImportError as error:
            raise RuntimeError("S3 存储需要安装 boto3") from error
        self.client = boto3.client(
            "s3",
            endpoint_url=S3_ENDPOINT_URL or None,
            aws_access_key_id=S3_ACCESS_KEY or None,
            aws_secret_access_key=S3_SECRET_KEY or None,
            region_name=S3_REGION,
            config=Config(
                s3={"addressing_style": "path" if S3_FORCE_PATH_STYLE else "auto"},
            ),
        )
        return self.client

    def ensure_ready(self) -> None:
        client = self._client()
        last_error: Exception | None = None
        for _ in range(20):
            try:
                client.head_bucket(Bucket=S3_BUCKET)
                return
            except Exception as error:
                last_error = error
                response = getattr(error, "response", {})
                code = str(response.get("Error", {}).get("Code", ""))
                if code in {"404", "NoSuchBucket", "NotFound"}:
                    try:
                        client.create_bucket(Bucket=S3_BUCKET)
                        return
                    except Exception as create_error:
                        last_error = create_error
                time.sleep(1)
        raise RuntimeError("无法连接或创建 S3 存储桶") from last_error

    def _key(
        self,
        kind: str,
        run_id: str,
        filename: str,
        space_id: str,
    ) -> str:
        safe_space_id = re.sub(r"[^a-zA-Z0-9_-]", "-", space_id).strip("-")
        scope = (
            []
            if not safe_space_id or safe_space_id == "default"
            else ["spaces", safe_space_id]
        )
        parts = [S3_PREFIX, *scope, kind, run_id, Path(filename).name]
        return "/".join(part for part in parts if part)

    def save_bytes(
        self,
        kind: str,
        run_id: str,
        filename: str,
        content: bytes,
        space_id: str = "default",
    ) -> str:
        client = self._client()
        key = self._key(kind, run_id, filename, space_id)
        media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        client.put_object(
            Bucket=S3_BUCKET,
            Key=key,
            Body=content,
            ContentType=media_type,
        )
        return f"s3://{S3_BUCKET}/{key}"

    def save_text(
        self,
        kind: str,
        run_id: str,
        filename: str,
        content: str,
        space_id: str = "default",
    ) -> str:
        return self.save_bytes(
            kind,
            run_id,
            filename,
            content.encode("utf-8"),
            space_id=space_id,
        )

    def read_bytes(self, uri: str) -> bytes:
        if not uri.startswith("s3://"):
            # Existing runs created before object storage was enabled can remain readable.
            return LocalStorage().read_bytes(uri)
        bucket, _, key = uri.removeprefix("s3://").partition("/")
        response = self._client().get_object(Bucket=bucket, Key=key)
        return response["Body"].read()

    def exists(self, uri: str) -> bool:
        if not uri.startswith("s3://"):
            return LocalStorage().exists(uri)
        bucket, _, key = uri.removeprefix("s3://").partition("/")
        try:
            self._client().head_object(Bucket=bucket, Key=key)
            return True
        except Exception:
            return False

    def info(self) -> dict[str, str]:
        return {
            "backend": self.name,
            "endpoint": S3_ENDPOINT_URL,
            "bucket": S3_BUCKET,
            "prefix": S3_PREFIX,
        }


def _build_storage() -> StorageBackend:
    if STORAGE_BACKEND == "s3":
        return S3Storage()
    if STORAGE_BACKEND != "local":
        raise RuntimeError(f"不支持的存储后端：{STORAGE_BACKEND}")
    return LocalStorage()


storage = _build_storage()
