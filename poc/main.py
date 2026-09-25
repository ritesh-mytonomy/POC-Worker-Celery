"""The POC's API entry point (api runs `uvicorn poc.main:app`): the production app plus the /poc routes.

Stand-ins for parts of the real product (design.md §6.1): /poc/seed, /poc/enqueue, /poc/scan-stub. Imports app/
only — never workers/ — so the API process still never loads the worker app (design.md §6.2a).
"""
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app import tasks_client
from app.config import get_settings
from app.db import get_db_session
from app.logging import get_logger
from app.repositories import files
from app.routes.errors import ApiError
from app.constants import QUEUE_SCAN
from poc import TASK_SCAN_STUB


def enqueue_scan_stub(seconds: float, enqueued_at: str) -> str:
    """Publish scan_stub on clinsync.scan (R13); return the task id. Raises if Redis is unreachable."""
    producer = tasks_client.get_producer()
    try:
        return producer.send_task(TASK_SCAN_STUB, kwargs={"seconds": seconds, "enqueued_at": enqueued_at},
                                  queue=QUEUE_SCAN).id
    except Exception:
        tasks_client.reset_producer()
        raise


router = APIRouter(prefix="/poc")
log = get_logger("poc.routes")   # the logger name since rev 1.4 commit 1


class SeedFileIn(BaseModel):
    """An object already placed in incoming/."""

    model_config = ConfigDict(extra="forbid")
    file_name: str = Field(min_length=1, max_length=512)
    s3_key: str = Field(min_length=1, max_length=1024)


class SeedIn(BaseModel):
    """Files to register as one batch."""

    model_config = ConfigDict(extra="forbid")
    files: list[SeedFileIn]


class SeededFileOut(BaseModel):
    """A created file row."""

    file_id: uuid.UUID
    file_name: str
    is_archive: bool
    status: str


class SeedOut(BaseModel):
    """The created batch and its files."""

    batch_id: uuid.UUID
    files: list[SeededFileOut]


class ScanStubIn(BaseModel):
    """How long the stub scan should take."""

    model_config = ConfigDict(extra="forbid")
    seconds: float = Field(ge=0, le=60)


class ScanStubOut(BaseModel):
    """The published scan task."""

    task_id: str
    enqueued_at: str


class EnqueueOut(BaseModel):
    """How many process_upload messages were published."""

    file_id: uuid.UUID
    published: int


@router.post("/seed", response_model=SeedOut, status_code=status.HTTP_201_CREATED)
def seed(body: SeedIn, db: Session = Depends(get_db_session)) -> SeedOut:
    """Create a batch and `uploading` file rows under POC_ORGANIZATION_ID; 400 for a disallowed extension (A3)."""
    s = get_settings()
    if s.POC_ORGANIZATION_ID is None:
        raise ApiError(503, "poc_not_configured", "POC_ORGANIZATION_ID is not set")
    batch_id, seeded = files.seed_batch(db, s.POC_ORGANIZATION_ID, [(f.file_name, f.s3_key) for f in body.files],
                                        s.ALLOWED_TOP_LEVEL_EXT)
    log.info("seeded", batch_id=str(batch_id), file_ids=[str(f.file_id) for f in seeded])
    return SeedOut(batch_id=batch_id, files=[SeededFileOut(**vars(f)) for f in seeded])


@router.post("/enqueue/{file_id}", response_model=EnqueueOut)
def enqueue(file_id: uuid.UUID, times: int = Query(default=1, ge=1, le=100),
            db: Session = Depends(get_db_session)) -> EnqueueOut:
    """Publish process_upload `times` times without changing state (S6: duplicate deliveries)."""
    found = files.file_exists(db, file_id)
    if found is None:
        raise ApiError(404, "not_found", f"file {file_id} does not exist")
    organization_id, _ = found
    for published in range(times):
        try:
            tasks_client.enqueue_process_upload(str(file_id), str(organization_id))
        except Exception as exc:
            log.warning("enqueue_failed", file_id=str(file_id), published=published, error=repr(exc))
            raise ApiError(503, "enqueue_failed", f"published {published} of {times}: {exc!r}") from exc
    log.info("poc_enqueued", file_id=str(file_id), times=times)
    return EnqueueOut(file_id=file_id, published=times)


@router.post("/scan-stub", response_model=ScanStubOut, status_code=status.HTTP_202_ACCEPTED)
def scan_stub(body: ScanStubIn) -> ScanStubOut:
    """Enqueue scan_stub(seconds, enqueued_at) on clinsync.scan (design.md §6.1, R13); 503 if Redis is down."""
    enqueued_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    try:
        task_id = enqueue_scan_stub(body.seconds, enqueued_at)
    except Exception as exc:
        log.warning("enqueue_failed", task="scan_stub", error=repr(exc))
        raise ApiError(503, "enqueue_failed", f"scan_stub not published: {exc!r}") from exc
    log.info("scan_stub_enqueued", task_id=task_id, enqueued_at=enqueued_at)
    return ScanStubOut(task_id=task_id, enqueued_at=enqueued_at)


# The production app with the POC routes added. Nothing in app/ knows about this.
from app.main import app  # noqa: E402

app.include_router(router)
