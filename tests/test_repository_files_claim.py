"""Tests for repositories.files.claim (design.md §6.2; R3.2, R3.3, R3.5)."""
import threading
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from functools import partial

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.repositories.files import FileClaim, claim
from tests.db_helpers import db_row, make_file, set_heartbeat_age

MAX_ATTEMPTS = 3
STALE = 30                               # the POC value; never 0 in the race (see claim() docstring)
ClaimFn = Callable[..., FileClaim | None]


def claim_now(session: Session, file_id: uuid.UUID) -> FileClaim | None:
    """Call claim() with the test limits."""
    return claim(session, file_id, max_attempts=MAX_ATTEMPTS, stale_after_seconds=STALE)


# --- single-connection cases (rolled back after each test) ---

def test_claim_uploaded_succeeds(db_session: Session) -> None:
    """Claim on uploaded returns a token and sets processing, attempt_count 1, a heartbeat."""
    file_id = make_file(db_session)
    result = claim_now(db_session, file_id)
    assert result is not None and result.file_id == file_id
    assert isinstance(result.claim_token, uuid.UUID) and result.attempt_count == 1
    row = db_row(db_session, file_id)
    assert row.status == "processing" and row.attempt_count == 1 and row.claim_token == result.claim_token


def test_claim_processing_with_fresh_heartbeat_returns_none(db_session: Session) -> None:
    """A processing file with a fresh heartbeat belongs to another worker."""
    file_id = make_file(db_session)
    set_heartbeat_age(db_session, file_id, 0)
    assert claim_now(db_session, file_id) is None


def test_claim_stale_processing_takes_over_with_new_token(db_session: Session) -> None:
    """Stale takeover succeeds with a different token and attempt_count 2."""
    file_id = make_file(db_session)
    first = claim_now(db_session, file_id)
    assert first is not None
    set_heartbeat_age(db_session, file_id, STALE + 5)
    second = claim_now(db_session, file_id)
    assert second is not None and second.attempt_count == 2
    assert second.claim_token != first.claim_token
    assert db_row(db_session, file_id).claim_token == second.claim_token


@pytest.mark.parametrize("status", ["processed", "partial", "rejected", "error", "uploading"])
def test_claim_non_claimable_status_returns_none(db_session: Session, status: str) -> None:
    """Terminal files (and not-yet-confirmed ones) are never claimed."""
    file_id = make_file(db_session, status=status)
    assert claim_now(db_session, file_id) is None
    assert db_row(db_session, file_id).status == status


def test_claim_at_max_attempts_returns_none(db_session: Session) -> None:
    """An uploaded file that has used every attempt is not claimed."""
    file_id = make_file(db_session, attempt_count=MAX_ATTEMPTS)
    assert claim_now(db_session, file_id) is None


def test_stale_takeover_at_max_attempts_returns_none(db_session: Session) -> None:
    """A stale processing file that has used every attempt is left for the stale sweeper."""
    file_id = make_file(db_session, attempt_count=MAX_ATTEMPTS)
    set_heartbeat_age(db_session, file_id, STALE + 5)
    assert claim_now(db_session, file_id) is None


# --- concurrency: committed rows, one connection per thread ---

@pytest.fixture
def race_engine() -> Iterator[Engine]:
    """Engine without pooling, so every Session opens its own physical connection."""
    engine = create_engine(get_settings().DATABASE_URL, poolclass=NullPool)
    yield engine
    engine.dispose()


def race(engine: Engine, claim_fn: ClaimFn, file_id: uuid.UUID, n: int = 10) -> tuple[list[FileClaim | None], set[int]]:
    """Release n threads at once, each claiming file_id on its own connection; return results and backend pids."""
    start = threading.Barrier(n)

    def one() -> tuple[FileClaim | None, int]:
        with Session(engine) as session:
            pid = session.execute(text("SELECT pg_backend_pid()")).scalar_one()
            session.commit()             # end that transaction: claim must start a fresh one
            start.wait(timeout=10)
            return claim_fn(session, file_id, max_attempts=MAX_ATTEMPTS, stale_after_seconds=STALE), pid

    with ThreadPoolExecutor(max_workers=n) as pool:
        outcomes = list(pool.map(lambda _: one(), range(n)))
    return [result for result, _ in outcomes], {pid for _, pid in outcomes}


@pytest.mark.parametrize("round_", range(20))
def test_ten_threads_exactly_one_wins(race_engine: Engine, committed_file: uuid.UUID, round_: int) -> None:
    """10 concurrent claims on one uploaded file: exactly one succeeds (R3.5)."""
    results, pids = race(race_engine, claim, committed_file)
    winners = [r for r in results if r is not None]
    assert len(pids) == 10, "each thread must use its own connection"
    assert len(winners) == 1, f"{len(winners)} winners != 1"
    with Session(race_engine) as session:
        row = db_row(session, committed_file)
    assert row.status == "processing" and row.attempt_count == 1
    assert row.claim_token == winners[0].claim_token


def claim_read_then_write(session: Session, file_id: uuid.UUID, *, max_attempts: int,
                          stale_after_seconds: int, read_done: threading.Barrier) -> FileClaim | None:
    """A deliberately BROKEN claim: checks status, then updates without re-checking it."""
    row = session.execute(
        text("SELECT status, attempt_count FROM upload_file WHERE file_id = :id"), {"id": file_id}
    ).one()
    read_done.wait(timeout=10)           # every thread has now seen 'uploaded'
    if row.status != "uploaded" or row.attempt_count >= max_attempts:
        session.commit()
        return None
    claimed = session.execute(
        text("""UPDATE upload_file SET status = 'processing', attempt_count = attempt_count + 1,
                       heartbeat_at = now(), claim_token = gen_random_uuid(), updated_at = now()
                WHERE file_id = :id
                RETURNING file_id, organization_id, batch_id, s3_key, file_name, file_ext,
                          is_archive, entries_done, attempt_count, claim_token"""),
        {"id": file_id},
    ).mappings().one()
    session.commit()
    return FileClaim(**claimed)


def test_race_harness_detects_read_then_write(race_engine: Engine, committed_file: uuid.UUID) -> None:
    """Canary: the same harness reports several winners for a read-then-write claim."""
    broken = partial(claim_read_then_write, read_done=threading.Barrier(10))
    results, _ = race(race_engine, broken, committed_file)
    assert sum(r is not None for r in results) > 1
