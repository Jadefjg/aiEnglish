"""Upload storage backends: local disk or S3-compatible (MinIO/OSS/AWS)."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from urllib.parse import quote


ROOT = Path(__file__).resolve().parents[1]
UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", ROOT / "uploads"))


def _backend() -> str:
    return (os.getenv("STORAGE_BACKEND", "local") or "local").strip().lower()


def storage_status() -> dict[str, Any]:
    backend = _backend()
    info: dict[str, Any] = {
        "backend": backend,
        "upload_dir": str(UPLOAD_DIR),
        "shared_ready": backend in {"s3", "minio", "oss"},
    }
    if info["shared_ready"]:
        info["bucket"] = os.getenv("S3_BUCKET", "")
        info["endpoint"] = os.getenv("S3_ENDPOINT", "")
        info["public_base"] = os.getenv("S3_PUBLIC_BASE_URL", "")
    return info


def _s3_client():
    import boto3
    from botocore.client import Config

    endpoint = os.getenv("S3_ENDPOINT", "").strip() or None
    region = os.getenv("S3_REGION", "us-east-1").strip() or "us-east-1"
    key = os.getenv("S3_ACCESS_KEY", "").strip()
    secret = os.getenv("S3_SECRET_KEY", "").strip()
    if not key or not secret:
        raise RuntimeError("S3_ACCESS_KEY/S3_SECRET_KEY required for S3 storage")
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name=region,
        aws_access_key_id=key,
        aws_secret_access_key=secret,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


def _bucket() -> str:
    bucket = os.getenv("S3_BUCKET", "").strip()
    if not bucket:
        raise RuntimeError("S3_BUCKET required for S3 storage")
    return bucket


def ensure_local_dirs():
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    (UPLOAD_DIR / "voice").mkdir(parents=True, exist_ok=True)


def save_bytes(relative_key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
    """Save object and return public URL path (always under /uploads/...)."""
    key = relative_key.lstrip("/").replace("\\", "/")
    backend = _backend()
    if backend in {"s3", "minio", "oss"}:
        client = _s3_client()
        client.put_object(Bucket=_bucket(), Key=key, Body=data, ContentType=content_type)
    else:
        ensure_local_dirs()
        path = UPLOAD_DIR / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return "/uploads/" + quote(key, safe="/")


def read_bytes(relative_key: str) -> bytes:
    key = relative_key.lstrip("/").replace("\\", "/")
    if key.startswith("uploads/"):
        key = key[len("uploads/") :]
    backend = _backend()
    if backend in {"s3", "minio", "oss"}:
        client = _s3_client()
        obj = client.get_object(Bucket=_bucket(), Key=key)
        return obj["Body"].read()
    path = UPLOAD_DIR / key
    if not path.exists():
        raise FileNotFoundError(key)
    return path.read_bytes()


def exists(relative_key: str) -> bool:
    key = relative_key.lstrip("/").replace("\\", "/")
    if key.startswith("uploads/"):
        key = key[len("uploads/") :]
    backend = _backend()
    if backend in {"s3", "minio", "oss"}:
        client = _s3_client()
        try:
            client.head_object(Bucket=_bucket(), Key=key)
            return True
        except Exception:
            return False
    return (UPLOAD_DIR / key).exists()


def public_or_presigned_url(relative_key: str, expires: int = 3600) -> str | None:
    """Optional absolute URL for S3; None means use /uploads proxy."""
    key = relative_key.lstrip("/").replace("\\", "/")
    if key.startswith("uploads/"):
        key = key[len("uploads/") :]
    backend = _backend()
    if backend not in {"s3", "minio", "oss"}:
        return None
    base = os.getenv("S3_PUBLIC_BASE_URL", "").rstrip("/")
    if base:
        return f"{base}/{quote(key, safe='/')}"
    if os.getenv("S3_PRESIGN", "1").strip() in {"1", "true", "yes"}:
        client = _s3_client()
        return client.generate_presigned_url(
            "get_object",
            Params={"Bucket": _bucket(), "Key": key},
            ExpiresIn=expires,
        )
    return None
