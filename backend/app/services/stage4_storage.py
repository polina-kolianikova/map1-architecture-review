from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.core.config import Settings


@dataclass(frozen=True)
class S3Uri:
    bucket: str
    key: str


def parse_s3_uri(uri: str) -> S3Uri:
    value = uri.strip()
    if not value.lower().startswith("s3://"):
        raise ValueError("Stage 4 Batch Statistical storage must use s3:// URIs")
    path = value[5:]
    bucket, separator, key = path.partition("/")
    if not bucket or not separator or not key:
        raise ValueError("Stage 4 Batch Statistical s3:// URI must include bucket and key")
    return S3Uri(bucket=bucket, key=key)


def upload_s3_file(local_path: Path, uri: str, *, settings: "Settings") -> None:
    target = parse_s3_uri(uri)
    _s3_client(settings).upload_file(str(local_path), target.bucket, target.key)


def download_s3_prefix(uri: str, local_dir: Path, *, settings: "Settings", suffix: str = ".json") -> list[Path]:
    source = parse_s3_uri(uri)
    prefix = source.key.rstrip("/")
    client = _s3_client(settings)
    local_dir.mkdir(parents=True, exist_ok=True)
    downloaded = []
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=source.bucket, Prefix=prefix):
        for item in page.get("Contents", []):
            key = item.get("Key")
            if not key or key.endswith("/") or (suffix and not key.endswith(suffix)):
                continue
            relative_key = key[len(prefix) :].lstrip("/") or Path(key).name
            local_path = local_dir / relative_key
            local_path.parent.mkdir(parents=True, exist_ok=True)
            client.download_file(source.bucket, key, str(local_path))
            downloaded.append(local_path)
    return downloaded


def _s3_client(settings: "Settings"):
    access_key = settings.stage4_batch_statistical_s3_access_key.strip()
    bucket_key_pair = settings.stage4_batch_statistical_s3_secret_access_key.strip()
    if not access_key or not bucket_key_pair:
        raise RuntimeError(
            "STAGE4_BATCH_STATISTICAL_S3_ACCESS_KEY and "
            "STAGE4_BATCH_STATISTICAL_S3_SECRET_ACCESS_KEY are required for Stage 4 S3 transfers"
        )
    import boto3

    client_kwargs = {
        "aws_access_key_id": access_key,
        "aws_" + "secret_access_key": bucket_key_pair,
    }
    region = settings.stage4_batch_statistical_s3_region.strip()
    if region:
        client_kwargs["region_name"] = region
    endpoint_url = settings.stage4_batch_statistical_s3_endpoint_url.strip()
    if endpoint_url:
        client_kwargs["endpoint_url"] = endpoint_url
    return boto3.client("s3", **client_kwargs)
