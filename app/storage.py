"""The API's own S3 calls (upload-ingest-merge design.md §4). The API never imports workers/.

Two clients, because of hostnames. Inside Docker the API reaches LocalStack as http://localstack:4566, which a
browser cannot resolve, and a presigned URL's host is part of its signature, so it cannot be rewritten afterwards:
  internal — AWS_ENDPOINT_URL: create / complete / abort multipart, list parts, head, copy, delete
  public   — S3_PUBLIC_ENDPOINT_URL: presigning part uploads and downloads, which the browser uses
In AWS both endpoints are unset and resolve to real S3. SigV4 always; path-style addressing whenever an endpoint
is set (LocalStack), virtual-hosted on real AWS, as Anugrah's s3_storage.py does.
"""
from functools import lru_cache
from typing import Any, NoReturn
from urllib.parse import quote

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from app.config import get_settings

NOT_FOUND = {"NoSuchKey", "404", "NotFound"}
INVALID_PARTS = {"InvalidPart", "InvalidPartOrder", "EntityTooSmall", "MalformedXML"}
DELETE_BATCH = 1000                      # S3's limit per DeleteObjects call


class StorageRefused(Exception):
    """An S3 refusal the API answers itself; str() is S3's message and `code` its error code."""

    def __init__(self, message: str, code: str = "") -> None:
        """Keep S3's message and code (Anugrah's API shows them as "S3 error (<code>): <message>")."""
        super().__init__(message)
        self.code = code


class NoSuchUpload(StorageRefused):
    """The multipart upload no longer exists: aborted, completed, or expired (design.md §5.3)."""


class InvalidParts(StorageRefused):
    """S3 refused the part list at completion: a part is missing, its ETag is wrong, or it is too small (§5.3)."""


def _config(endpoint_url: str | None) -> Config:
    """SigV4, path-style for a custom endpoint, and the worker's short timeouts (ingest design.md §8.1)."""
    return Config(signature_version="s3v4", connect_timeout=2, read_timeout=3,
                  retries={"mode": "standard", "total_max_attempts": 2},
                  s3={"addressing_style": "path" if endpoint_url else "virtual"})


def _client(endpoint_url: str | None) -> Any:
    """One boto3 S3 client for this endpoint (None = real AWS)."""
    return boto3.client("s3", endpoint_url=endpoint_url, config=_config(endpoint_url))


@lru_cache
def internal_client() -> Any:
    """Client on AWS_ENDPOINT_URL (LocalStack inside Docker), otherwise real AWS."""
    return _client(get_settings().AWS_ENDPOINT_URL or None)


@lru_cache
def public_client() -> Any:
    """Client on S3_PUBLIC_ENDPOINT_URL (the host a browser reaches), otherwise real AWS. Used only to presign."""
    return _client(get_settings().S3_PUBLIC_ENDPOINT_URL or None)


def _bucket() -> str:
    """The POC bucket."""
    return get_settings().S3_BUCKET


def _code(exc: ClientError) -> str:
    """The S3 error code of a ClientError."""
    return str(exc.response.get("Error", {}).get("Code", ""))


def _raise_mapped(exc: ClientError) -> NoReturn:
    """Re-raise as NoSuchUpload or InvalidParts when S3's code says so; otherwise re-raise unchanged."""
    code, message = _code(exc), str(exc.response.get("Error", {}).get("Message") or exc)
    if code == "NoSuchUpload":
        raise NoSuchUpload(message, code) from exc
    if code in INVALID_PARTS:
        raise InvalidParts(message, code) from exc
    raise exc


# ── Objects ─────────────────────────────────────────────────────────────────

def object_size(key: str) -> int | None:
    """Size in bytes of the object at key, or None if it does not exist. Other S3 errors propagate."""
    try:
        head = internal_client().head_object(Bucket=_bucket(), Key=key)
    except ClientError as exc:
        if _code(exc) in NOT_FOUND:
            return None
        raise
    return int(head["ContentLength"])


def exists(key: str) -> bool:
    """True if an object is stored at key."""
    return object_size(key) is not None


def copy(source_key: str, dest_key: str) -> None:
    """Server-side copy within the bucket; a managed copy, so objects over 5 GB go part by part."""
    internal_client().copy({"Bucket": _bucket(), "Key": source_key}, _bucket(), dest_key)


def delete(key: str) -> None:
    """Delete one object; deleting a missing key is not an error (S3 semantics)."""
    internal_client().delete_object(Bucket=_bucket(), Key=key)


def delete_many(keys: Any) -> None:
    """Delete every key, 1000 per call; raise if S3 reports any key it could not delete."""
    batch = [k for k in keys if k]
    for start in range(0, len(batch), DELETE_BATCH):
        chunk = batch[start:start + DELETE_BATCH]
        response = internal_client().delete_objects(
            Bucket=_bucket(), Delete={"Objects": [{"Key": k} for k in chunk], "Quiet": True})
        if response.get("Errors"):
            raise RuntimeError(f"S3 could not delete {len(response['Errors'])} object(s): "
                               f"{response['Errors'][:3]}")


def presign_get(key: str, file_name: str, expires_in: int | None = None) -> str:
    """A download URL on the public host that saves as file_name (Content-Disposition: attachment)."""
    ascii_name = file_name.encode("ascii", "replace").decode().replace('"', "'").replace("?", "_")
    disposition = f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(file_name, safe='')}"
    return public_client().generate_presigned_url(
        "get_object", Params={"Bucket": _bucket(), "Key": key, "ResponseContentDisposition": disposition},
        ExpiresIn=expires_in or get_settings().DOWNLOAD_URL_EXPIRES_SECONDS)


# ── Multipart uploads ───────────────────────────────────────────────────────

def create_multipart(key: str, content_type: str | None) -> str:
    """Start a multipart upload at key; return S3's UploadId."""
    response = internal_client().create_multipart_upload(
        Bucket=_bucket(), Key=key, ContentType=content_type or "application/octet-stream")
    return str(response["UploadId"])


def presign_part(key: str, upload_id: str, part_number: int, expires_in: int | None = None) -> str:
    """A PUT URL for one part, signed for the public host. The browser sends it without a Content-Type."""
    return public_client().generate_presigned_url(
        "upload_part",
        Params={"Bucket": _bucket(), "Key": key, "UploadId": upload_id, "PartNumber": part_number},
        ExpiresIn=expires_in or get_settings().PRESIGN_EXPIRES_SECONDS)


def list_parts(key: str, upload_id: str) -> list[dict[str, Any]]:
    """Parts already stored, as Anugrah's API returns them: [{partNumber, etag, size}], in part order."""
    parts: list[dict[str, Any]] = []
    marker = 0
    try:
        while True:
            response = internal_client().list_parts(Bucket=_bucket(), Key=key, UploadId=upload_id,
                                                    PartNumberMarker=marker)
            parts += [{"partNumber": p["PartNumber"], "etag": p["ETag"], "size": p["Size"]}
                      for p in response.get("Parts", [])]
            if not response.get("IsTruncated"):
                return parts
            marker = response["NextPartNumberMarker"]
    except ClientError as exc:
        _raise_mapped(exc)


def complete_multipart(key: str, upload_id: str, parts: list[dict[str, Any]]) -> str:
    """Assemble the object from parts [{partNumber, etag}], sorted by part number; return its location.

    Raises NoSuchUpload if the upload is gone, InvalidParts if S3 refuses the part list.
    """
    ordered = sorted(parts, key=lambda part: int(part["partNumber"]))
    try:
        response = internal_client().complete_multipart_upload(
            Bucket=_bucket(), Key=key, UploadId=upload_id,
            MultipartUpload={"Parts": [{"PartNumber": int(p["partNumber"]), "ETag": p["etag"]} for p in ordered]})
    except ClientError as exc:
        _raise_mapped(exc)
    return str(response.get("Location") or f"s3://{_bucket()}/{key}")


def abort_multipart(key: str, upload_id: str) -> None:
    """Abort the multipart upload and discard its parts; one already gone (aborted or completed) is fine."""
    try:
        internal_client().abort_multipart_upload(Bucket=_bucket(), Key=key, UploadId=upload_id)
    except ClientError as exc:
        if _code(exc) not in {"NoSuchUpload", "404"}:
            raise
