"""Tests for repositories.candidates.upsert (design.md §6.2; R8.3)."""
import threading
import time
import uuid
from typing import Any

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from app.errors import CandidateIdentityMismatch, ClaimSuperseded
from app.repositories.candidates import upsert
from app.repositories.files import _CLAIM_SQL, claim, finish, release
from tests.db_helpers import MAX_ATTEMPTS, STALE, claimed, set_heartbeat_age, triggers_off

HASH = "ab" * 32                         # a content_hash: 64 lowercase hex (U5.2)

ENTRY = {"source_entry_name": "docs/a.docx", "entry_index": 3, "file_name": "a.docx", "file_ext": "docx"}
DIRECT = {"source_entry_name": None, "entry_index": None, "file_name": "valid.docx", "file_ext": "docx"}
PROCESSED = {"status": "processed", "s3_key": "ClinSync/staging/k1", "size_bytes": 100, "content_hash": HASH}
REJECTED = {"status": "rejected", "reject_reason": ".exe is not supported"}


def candidates(session: Session, file_id: uuid.UUID) -> list[dict[str, Any]]:
    """Every candidate row of the file."""
    return [dict(r) for r in session.execute(
        text("SELECT * FROM staged_document WHERE source_file_id = :id ORDER BY created_at"), {"id": file_id}
    ).mappings()]


def backdate_candidates(session: Session, file_id: uuid.UUID) -> None:
    """Age the file's candidates' updated_at, so an overwrite stamping now() visibly changes it."""
    with triggers_off(session):
        session.execute(text("UPDATE staged_document SET updated_at = now() - interval '60 seconds' "
                             "WHERE source_file_id = :id"), {"id": file_id})
    session.commit()


# --- the upsert ---

@pytest.mark.parametrize(("first", "second"), [(REJECTED, PROCESSED),
                                              (PROCESSED, {**PROCESSED, "s3_key": "ClinSync/staging/k2",
                                                           "size_bytes": 222})])
def test_same_archive_entry_twice_gives_one_row_with_second_values(
    db_session: Session, first: dict[str, Any], second: dict[str, Any]
) -> None:
    """Replaying an archive entry overwrites its row (R8.3)."""
    file_id, token = claimed(db_session)
    staged_id = upsert(db_session, file_id, token, **ENTRY, **first)
    backdate_candidates(db_session, file_id)
    before = candidates(db_session, file_id)[0]
    assert upsert(db_session, file_id, token, **ENTRY, **second) == staged_id
    rows = candidates(db_session, file_id)
    assert len(rows) == 1
    row = rows[0]
    assert row["staged_id"] == staged_id and row["created_at"] == before["created_at"]
    for column in ("status", "s3_key", "size_bytes", "reject_reason"):
        assert row[column] == second.get(column)
    assert row["updated_at"] > before["updated_at"]


def test_same_direct_upload_twice_gives_one_row(db_session: Session) -> None:
    """A direct upload (NULL entry name) replayed also overwrites — this needs NULLS NOT DISTINCT."""
    file_id, token = claimed(db_session)
    upsert(db_session, file_id, token, **DIRECT, **PROCESSED)
    upsert(db_session, file_id, token, **DIRECT, **{**PROCESSED, "size_bytes": 999})
    rows = candidates(db_session, file_id)
    assert len(rows) == 1 and rows[0]["size_bytes"] == 999 and rows[0]["source_entry_name"] is None


def test_distinct_entries_and_files_give_distinct_rows(db_session: Session) -> None:
    """Different entry names, or the same name on another file, are separate candidates."""
    file_id, token = claimed(db_session)
    other_id, other_token = claimed(db_session)
    upsert(db_session, file_id, token, **ENTRY, **PROCESSED)
    upsert(db_session, file_id, token, **{**ENTRY, "source_entry_name": "docs/b.docx", "entry_index": 4},
           **PROCESSED)
    upsert(db_session, other_id, other_token, **ENTRY, **PROCESSED)
    assert len(candidates(db_session, file_id)) == 2 and len(candidates(db_session, other_id)) == 1


def test_batch_and_organization_come_from_the_parent(db_session: Session) -> None:
    """batch_id and organization_id are copied from the parent upload_file row."""
    file_id, token = claimed(db_session)
    upsert(db_session, file_id, token, **ENTRY, **PROCESSED)
    parent = db_session.execute(text("SELECT batch_id, organization_id FROM upload_file WHERE file_id = :id"),
                                {"id": file_id}).one()
    row = candidates(db_session, file_id)[0]
    assert (row["batch_id"], row["organization_id"]) == (parent.batch_id, parent.organization_id)


def test_long_reject_reason_is_truncated(db_session: Session) -> None:
    """A reject_reason over 512 characters is cut to 512, ending with an ellipsis."""
    file_id, token = claimed(db_session)
    upsert(db_session, file_id, token, **ENTRY, status="rejected", reject_reason="r" * 600)
    reason = candidates(db_session, file_id)[0]["reject_reason"]
    assert len(reason) == 512 and reason.endswith("…")


@pytest.mark.parametrize("change", [{"entry_index": 4}, {"file_name": "b.docx"}, {"file_ext": "pdf"}])
def test_replay_with_different_identity_raises_and_changes_nothing(
    db_session: Session, change: dict[str, Any]
) -> None:
    """A replay must reproduce entry_index, file_name and file_ext; otherwise it raises and the row is untouched."""
    file_id, token = claimed(db_session)
    upsert(db_session, file_id, token, **ENTRY, **REJECTED)
    before = candidates(db_session, file_id)
    with pytest.raises(CandidateIdentityMismatch):
        upsert(db_session, file_id, token, **{**ENTRY, **change}, **PROCESSED)
    assert candidates(db_session, file_id) == before


# --- fencing ---

def test_old_token_after_stale_takeover_raises_and_writes_nothing(db_session: Session) -> None:
    """After a takeover the old token can neither insert nor overwrite; the new token can."""
    file_id, old_token = claimed(db_session)
    upsert(db_session, file_id, old_token, **ENTRY, **REJECTED)
    set_heartbeat_age(db_session, file_id, STALE + 5)
    takeover = claim(db_session, file_id, max_attempts=MAX_ATTEMPTS, stale_after_seconds=STALE)
    assert takeover is not None
    before = candidates(db_session, file_id)
    with pytest.raises(ClaimSuperseded):
        upsert(db_session, file_id, old_token, **ENTRY, **PROCESSED)          # overwrite attempt
    with pytest.raises(ClaimSuperseded):
        upsert(db_session, file_id, old_token, **{**ENTRY, "source_entry_name": "docs/new.docx",
                                                  "entry_index": 9}, **PROCESSED)   # insert attempt
    assert candidates(db_session, file_id) == before
    upsert(db_session, file_id, takeover.claim_token, **ENTRY, **PROCESSED)
    assert candidates(db_session, file_id)[0]["status"] == "processed"


@pytest.mark.parametrize("how", ["finish", "release", "random_token"])
def test_upsert_without_ownership_raises(db_session: Session, how: str) -> None:
    """After finish or release (even with the right token), or with a random token, nothing is written."""
    file_id, token = claimed(db_session)
    if how == "finish":
        finish(db_session, file_id, token, "processed")
    elif how == "release":
        release(db_session, file_id, token, reason="retry")
    else:
        token = uuid.uuid4()
    with pytest.raises(ClaimSuperseded):
        upsert(db_session, file_id, token, **ENTRY, **PROCESSED)
    assert candidates(db_session, file_id) == []


def test_unknown_file_raises(db_session: Session) -> None:
    """A file that does not exist cannot receive candidates."""
    with pytest.raises(ClaimSuperseded):
        upsert(db_session, uuid.uuid4(), uuid.uuid4(), **ENTRY, **PROCESSED)


# --- validation the database does not do ---

@pytest.mark.parametrize("bad", [
    {"status": "bogus", "s3_key": "k"},
    {"status": "processed"},                                           # no s3_key
    {"status": "processed", "s3_key": ""},
    {"status": "processed", "s3_key": "k", "reject_reason": "why"},
    {"status": "processed", "s3_key": "k"},                            # no content_hash (U5.2)
    {"status": "rejected"},                                            # no reason
    {"status": "rejected", "reject_reason": ""},
    {"status": "rejected", "reject_reason": "why", "s3_key": "k"},
])
def test_invalid_candidate_raises_value_error(db_session: Session, bad: dict[str, Any]) -> None:
    """Contradictory candidates are refused before any SQL."""
    file_id, token = claimed(db_session)
    with pytest.raises(ValueError):
        upsert(db_session, file_id, token, **ENTRY, **bad)
    assert candidates(db_session, file_id) == []


@pytest.mark.parametrize("identity", [{"source_entry_name": "a.docx", "entry_index": None},
                                      {"source_entry_name": None, "entry_index": 0}])
def test_entry_name_and_index_must_agree(db_session: Session, identity: dict[str, Any]) -> None:
    """source_entry_name and entry_index are both set (archive entry) or both None (direct upload)."""
    file_id, token = claimed(db_session)
    with pytest.raises(ValueError):
        upsert(db_session, file_id, token, file_name="a.docx", file_ext="docx", **identity, **PROCESSED)


# --- why NULLS NOT DISTINCT ---

def test_uq_entry_is_nulls_not_distinct(db_session: Session) -> None:
    """The live constraint really has NULLS NOT DISTINCT (guards init.sql)."""
    assert db_session.execute(text(
        "SELECT i.indnullsnotdistinct FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
        "WHERE c.relname = 'uq_entry'")).scalar_one() is True


@pytest.mark.parametrize(("clause", "expected_rows"), [("", 2), ("NULLS NOT DISTINCT", 1)])
def test_null_upsert_dedupes_only_with_nulls_not_distinct(
    db_session: Session, clause: str, expected_rows: int
) -> None:
    """The same NULL-keyed upsert twice: two rows with a plain UNIQUE, one with NULLS NOT DISTINCT."""
    db_session.execute(text(f"CREATE TEMP TABLE t_nnd (a int, b text, v int, CONSTRAINT u_nnd UNIQUE {clause} (a, b))"))
    for v in (1, 2):
        db_session.execute(text("INSERT INTO t_nnd VALUES (1, NULL, :v) "
                                "ON CONFLICT ON CONSTRAINT u_nnd DO UPDATE SET v = EXCLUDED.v"), {"v": v})
    assert db_session.execute(text("SELECT count(*) FROM t_nnd")).scalar_one() == expected_rows


# --- the parent-row lock: committed rows, separate connections ---

def test_upsert_waits_for_in_flight_takeover_then_raises(race_engine: Engine, committed_file: uuid.UUID) -> None:
    """An upsert racing an uncommitted takeover blocks on the parent row, then sees the new token and raises."""
    with Session(race_engine) as session:
        old = claim(session, committed_file, max_attempts=MAX_ATTEMPTS, stale_after_seconds=STALE)
        assert old is not None
        set_heartbeat_age(session, committed_file, STALE + 5)

    takeover = race_engine.connect()                      # holds the row lock, uncommitted
    row = takeover.execute(_CLAIM_SQL, {"file_id": committed_file, "max_attempts": MAX_ATTEMPTS,
                                        "stale": STALE}).one_or_none()
    assert row is not None and row.claim_token != old.claim_token

    outcome: dict[str, Any] = {}

    def worker() -> None:
        # One connection held for the thread's life, so the recorded pid is the one that runs upsert().
        with race_engine.connect() as conn, Session(bind=conn) as session:
            outcome["pid"] = session.execute(text("SELECT pg_backend_pid()")).scalar_one()
            session.commit()
            try:
                outcome["result"] = upsert(session, committed_file, old.claim_token, **ENTRY, **PROCESSED)
            except ClaimSuperseded as exc:
                outcome["result"] = exc

    thread = threading.Thread(target=worker)
    thread.start()
    blocked = False
    with race_engine.connect() as probe:                  # wait for a condition, not a fixed time
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and thread.is_alive() and not blocked:
            if "pid" in outcome:
                wait = probe.execute(text("SELECT wait_event_type FROM pg_stat_activity WHERE pid = :p"),
                                     {"p": outcome["pid"]}).scalar_one_or_none()
                blocked = wait == "Lock"
                probe.rollback()          # pg_stat_activity is snapshotted per transaction
            time.sleep(0.02)
    takeover.commit()
    takeover.close()
    thread.join(timeout=10)

    assert blocked, "the upsert did not wait on the parent row lock"
    assert isinstance(outcome.get("result"), ClaimSuperseded), f"upsert returned {outcome.get('result')!r}"
    with Session(race_engine) as session:
        assert candidates(session, committed_file) == []


def test_non_ascii_entry_name_is_stored_exactly(db_session: Session) -> None:
    """A name with spaces and non-ASCII characters round-trips through the upsert unchanged."""
    file_id, token = claimed(db_session)
    name = "Kardiologie/Überblick Herz 2026.docx"
    upsert(db_session, file_id, token, source_entry_name=name, entry_index=0, file_name="Überblick Herz 2026.docx",
           file_ext="docx", status="processed", s3_key="ClinSync/staging/k/0000_Überblick Herz 2026.docx",
           size_bytes=1, content_hash=HASH)
    (row,) = candidates(db_session, file_id)
    assert row["source_entry_name"] == name and row["file_name"] == "Überblick Herz 2026.docx"
    assert row["s3_key"].endswith("0000_Überblick Herz 2026.docx")
