"""Test doubles for the Upload API tests: an in-memory app.storage and a recording enqueue."""
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any

import pytest

from app import storage, tasks_client
from app.config import get_settings

NO_SUCH_UPLOAD = "The specified upload does not exist."                    # S3's messages
INVALID_PART = "One or more of the specified parts could not be found."


@dataclass
class FakeStorage:
    """app.storage's multipart and object calls, in memory. Records every call by name."""

    uploads: dict[str, dict[int, str]] = field(default_factory=dict)     # upload_id -> {part_number: etag}
    keys: dict[str, str] = field(default_factory=dict)                   # upload_id -> key
    objects: set[str] = field(default_factory=set)
    calls: list[str] = field(default_factory=list)
    copies: list[tuple[str, str]] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def create_multipart(self, key: str, content_type: str | None) -> str:
        """Start an upload; its id is random, like S3's."""
        with self._lock:
            self.calls.append("create_multipart")
            upload_id = f"up-{uuid.uuid4().hex}"
            self.uploads[upload_id], self.keys[upload_id] = {}, key
            return upload_id

    def presign_part(self, key: str, upload_id: str, part_number: int, expires_in: int | None = None) -> str:
        """A URL on the public host, like the real one."""
        self.calls.append("presign_part")
        return f"http://localhost:4566/{get_settings().S3_BUCKET}/{key}?uploadId={upload_id}&partNumber={part_number}"

    def put_part(self, upload_id: str, part_number: int) -> str:
        """What the browser's PUT does: store a part, return its ETag."""
        etag = f'"etag-{upload_id}-{part_number}"'
        self.uploads[upload_id][part_number] = etag
        return etag

    def list_parts(self, key: str, upload_id: str) -> list[dict[str, Any]]:
        """Stored parts in order; a gone upload raises NoSuchUpload."""
        self.calls.append("list_parts")
        if upload_id not in self.uploads:
            raise storage.NoSuchUpload(NO_SUCH_UPLOAD, "NoSuchUpload")
        return [{"partNumber": n, "etag": e, "size": 8} for n, e in sorted(self.uploads[upload_id].items())]

    def complete_multipart(self, key: str, upload_id: str, parts: list[dict[str, Any]]) -> str:
        """Assemble: every part must be stored with that ETag, else InvalidParts; the upload is then gone."""
        self.calls.append("complete_multipart")
        if upload_id not in self.uploads:
            raise storage.NoSuchUpload(NO_SUCH_UPLOAD, "NoSuchUpload")
        stored = self.uploads[upload_id]
        if any(stored.get(int(p["partNumber"])) != p["etag"] for p in parts):
            raise storage.InvalidParts(INVALID_PART, "InvalidPart")
        del self.uploads[upload_id]
        self.objects.add(key)
        return f"http://localstack:4566/{get_settings().S3_BUCKET}/{key}"

    def abort_multipart(self, key: str, upload_id: str) -> None:
        """Drop the upload and its parts; one already gone is fine."""
        self.calls.append("abort_multipart")
        self.uploads.pop(upload_id, None)

    def copy(self, source_key: str, dest_key: str) -> None:
        """Server-side copy: the destination now exists."""
        self.calls.append("copy")
        self.copies.append((source_key, dest_key))
        self.objects.add(dest_key)

    def delete_many(self, keys: Any) -> None:
        """Delete every key given (missing ones are fine, as in S3)."""
        self.calls.append("delete_many")
        batch = [k for k in keys if k]
        self.deleted.extend(batch)
        self.objects.difference_update(batch)

    def exists(self, key: str) -> bool:
        """True for a completed object."""
        self.calls.append("exists")
        return key in self.objects


@pytest.fixture
def fake_storage(monkeypatch: pytest.MonkeyPatch) -> FakeStorage:
    """Replace every app.storage call the Upload API makes with the in-memory fake."""
    fake = FakeStorage()
    for name in ("create_multipart", "presign_part", "list_parts", "complete_multipart", "abort_multipart", "exists",
                 "copy", "delete_many"):
        monkeypatch.setattr(storage, name, getattr(fake, name))
    return fake


class Recorder:
    """A thread-safe fake enqueue that records (file_id, organization_id)."""

    def __init__(self) -> None:
        """Start with no calls."""
        self.calls: list[tuple[str, str]] = []
        self._lock = threading.Lock()

    def __call__(self, file_id: str, organization_id: str) -> None:
        """Record one publish."""
        with self._lock:
            self.calls.append((file_id, organization_id))


@pytest.fixture
def enqueued(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    """process_upload publishes, recorded instead of sent."""
    recorder = Recorder()
    monkeypatch.setattr(tasks_client, "enqueue_process_upload", recorder)
    return recorder
