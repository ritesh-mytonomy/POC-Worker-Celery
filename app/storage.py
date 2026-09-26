"""The API's own S3 calls (upload-ingest-merge design.md §4). The API never imports workers/.

Task 1.1 needs only a HEAD for /poc/seed; task 2.1 adds the public client, multipart and presign.
"""
from functools import lru_cache
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from app.config import get_settings

# The worker's short timeouts (ingest design.md §8.1): an S3 outage surfaces in seconds, not half a minute.
CLIENT_CONFIG = Config(connect_timeout=2, read_timeout=3, retries={"mode": "standard", "total_max_attempts": 2})
NOT_FOUND = {"NoSuchKey", "404", "NotFound"}


@lru_cache
def internal_client() -> Any:
    """boto3 client on AWS_ENDPOINT_URL when set (LocalStack inside Docker), otherwise real AWS."""
    return boto3.client("s3", endpoint_url=get_settings().AWS_ENDPOINT_URL or None, config=CLIENT_CONFIG)


def object_size(key: str) -> int | None:
    """Size in bytes of the object at key, or None if it does not exist. Other S3 errors propagate."""
    try:
        head = internal_client().head_object(Bucket=get_settings().S3_BUCKET, Key=key)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in NOT_FOUND:
            return None
        raise
    return int(head["ContentLength"])
