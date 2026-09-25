# Design — Ingest Worker POC (Celery + Redis)

| | |
|---|---|
| **Feature** | `ingest-worker-poc` |
| **Implements** | `requirements.md` R1–R15, NFR-1–7 |
| **Aligns with** | ClinSync LLD §A2, §M2.2.4–M2.2.6, §D1 |

---

## 1. Overview

```
                 ┌──────────────── one Docker image ────────────────┐
                 │                                                  │
 scripts/  ──▶  api (FastAPI) ──SQL──▶ postgres                     │
 scenario_*.py   │   │                                              │
                 │   └──LPUSH──▶ redis ◀──BRPOP── worker-ingest ─┐  │
                 │                  ▲             worker-scan    │  │
                 │                  └──schedule── worker-maint+beat │
                 │                                               │  │
                 │   ◀──────── /internal/* (X-Internal-Key) ─────┘  │
                 └──────────────────────────────────────────────────┘
                                         │
                           localstack (S3): ClinSync/incoming/ · ClinSync/staging/
```

Three rules hold everywhere:

1. **The API is the only component that touches PostgreSQL.** Workers and sweepers go through `/internal/*`.
2. **Redis holds only messages.** State lives in PostgreSQL. Losing Redis costs latency, not data.
3. **Every worker write is fenced by a claim token.** A worker that has lost ownership cannot change anything.

---

## 2. Components

| Service | Image command | Role |
|---|---|---|
| `localstack` | `localstack/localstack` | S3 on `:4566`. Bucket created by an init hook |
| `redis` | `redis:7-alpine --appendonly yes` | Broker. AOF on, matching ElastiCache |
| `postgres` | `postgres:15-alpine` | State. Schema applied by init script |
| `api` | `uvicorn poc.main:app --host 0.0.0.0 --port 8000` (rev 1.4: the POC wrapper around `app.main:app`) | Public + internal endpoints, enqueues tasks; `/poc/*` helpers |
| `worker-ingest` | `celery -A workers.tasks worker -Q clinsync.ingest -c ${INGEST_CONCURRENCY} -n ingest@%h` | `process_upload` |
| `worker-scan` | `celery -A poc.worker worker -Q clinsync.scan -c 1 -n scan@%h` (rev 1.4: the production app plus the stub) | Stub scan task — proves isolation |
| `worker-maint` | `celery -A workers.tasks worker -Q clinsync.maintenance -c 1 -B -n maint@%h` | Sweepers, with beat embedded |

`-B` embeds beat in one worker. Acceptable because exactly one `worker-maint` runs. In AWS, beat becomes its own single-instance ECS service.

**POC-only code lives in `poc/` and the dependency points one way** (rev 1.4): `poc/` may import `app/` and `workers/`; nothing in `app/` or `workers/` imports or names `poc/` (tested). `poc/main.py` and `poc/worker.py` are thin entry points that add the POC routes and the scan stub to the production app objects; `TASK_SCAN_STUB` lives in `poc/__init__.py`, so the scan worker never loads the FastAPI app and the API never loads the worker app (tested). Deleting `poc/` and pointing the two commands back at `app.main:app` / `workers.tasks` leaves production intact.

---

## 3. Project layout

Mirrors the house layering (LLD §D1) at POC scale. Built at the **root of the working folder**; the name below is illustrative. The compose project is named explicitly (`name: clinsync-ingest-poc`) so container and volume names do not depend on the folder name.

```
./
├── docker-compose.yml
├── Dockerfile                     one image, all services
├── .env.example
├── pyproject.toml / requirements.txt
├── infra/
│   ├── localstack/init-s3.sh      create bucket, lifecycle rule on incoming/
│   └── postgres/init.sql          schema (§4)
├── app/
│   ├── main.py                    FastAPI, routers, startup validation
│   ├── config.py                  settings + validate() (§9)
│   ├── db.py                      engine, get_db_session()
│   ├── logging.py                 JSON logger
│   ├── security.py                require_internal_key dependency
│   ├── models.py                  SQLAlchemy models
│   ├── repositories/
│   │   ├── files.py               claim, heartbeat, progress, finish, release, fenced writes
│   │   └── candidates.py          upsert, list
│   ├── services/
│   │   ├── uploads.py             confirm → enqueue
│   │   └── sweeps.py              stale + reconcile
│   ├── routes/
│   │   ├── uploads.py             confirm, batch status, staged
│   │   └── internal.py            /internal/*
│   ├── constants.py               queue and task names, shared with workers/
│   └── tasks_client.py            producer-only Celery instance (§6.2a)
├── engine/                        no FastAPI, no Celery, no network — pure functions
│   └── file_checks.py             limits · Rejected · detect_file_type()/mismatch_reason() · inspect_archive()/
│                                  assert_safe_path() · extract_streaming() — engine takes limits as arguments
├── workers/                       never opens a database connection
│   ├── tasks.py                   Celery app and config (§7) · Heartbeat · process_upload / process_document /
│   │                              process_archive · run_stale_sweep / run_reconcile_sweep
│   └── clients.py                 Transient · Internal API client (carries the claim token) · S3 store — each with
│                                  its error mapping; the only place that raises Transient
├── poc/                           POC-only (rev 1.4); imports app/ and workers/, never imported by them
│   ├── __init__.py                TASK_SCAN_STUB (shared by both entry points without loading each other)
│   ├── main.py                    api entry point: app.main:app + /poc/seed, /poc/enqueue, /poc/scan-stub
│   └── worker.py                  worker-scan entry point: workers.tasks + the scan route and scan_stub
├── fixtures/
│   └── make_fixtures.py           generates every test file (§10.1)
├── scripts/
│   ├── lib.py                     seed(), confirm(), wait_terminal(), assert_*
│   └── scenario_s1.py … s8.py
└── tests/
    ├── test_engine_file_signature.py
    ├── test_engine_archive.py
    ├── test_repository_files_claim.py
    ├── test_routes_internal_fencing.py
    └── conftest.py
```

`engine/` has no framework imports **and does not read settings** — limits are passed in as a `Limits` value. Every guard and detector is unit-testable with a file on disk, nothing running, and small limits constructed in the test.

---

## 4. Data model

```sql
CREATE TABLE upload_batch (
  batch_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id  UUID NOT NULL,
  status           VARCHAR(16) NOT NULL DEFAULT 'in_progress'
                     CHECK (status IN ('in_progress','staged','committed','abandoned')),
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE upload_file (
  file_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  batch_id         UUID NOT NULL REFERENCES upload_batch(batch_id) ON DELETE CASCADE,
  organization_id  UUID NOT NULL,
  file_name        VARCHAR(512) NOT NULL,
  file_ext         VARCHAR(10)  NOT NULL,
  is_archive       BOOLEAN      NOT NULL,
  s3_key           VARCHAR(1024) NOT NULL,
  status           VARCHAR(16)  NOT NULL DEFAULT 'uploading'
                     CHECK (status IN ('uploading','uploaded','processing',
                                       'processed','partial','rejected','error')),
  status_message   VARCHAR(512),
  detected_type    VARCHAR(32),
  entries_total    INTEGER,
  entries_done     INTEGER NOT NULL DEFAULT 0,
  attempt_count    INTEGER NOT NULL DEFAULT 0,
  claim_token      UUID,
  heartbeat_at     TIMESTAMPTZ,
  uploaded_at      TIMESTAMPTZ,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_file_batch     ON upload_file (batch_id);
CREATE INDEX idx_file_sweep     ON upload_file (status, heartbeat_at);
CREATE INDEX idx_file_reconcile ON upload_file (status, uploaded_at);

CREATE TABLE staged_document (
  staged_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  batch_id           UUID NOT NULL REFERENCES upload_batch(batch_id) ON DELETE CASCADE,
  organization_id    UUID NOT NULL,
  source_file_id     UUID NOT NULL REFERENCES upload_file(file_id) ON DELETE CASCADE,
  source_entry_name  VARCHAR(1024),              -- NULL for a direct upload
  entry_index        INTEGER,                    -- position among file entries (dirs excluded); NULL for direct
  file_name          VARCHAR(512) NOT NULL,
  file_ext           VARCHAR(10)  NOT NULL,
  size_bytes         BIGINT,
  s3_key             VARCHAR(1024),              -- NULL when rejected
  status             VARCHAR(16) NOT NULL CHECK (status IN ('processed','rejected')),
  reject_reason      VARCHAR(512),
  created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_entry UNIQUE NULLS NOT DISTINCT (source_file_id, source_entry_name)
);
```

`uq_entry` is what makes a replayed entry an overwrite (R8.3). `NULLS NOT DISTINCT` (PostgreSQL 15+) makes it bind for direct uploads too, where `source_entry_name` is NULL.

---

## 5. State machine — `upload_file`

```
uploading ──confirm──▶ uploaded ──claim──▶ processing ─┬─▶ processed
                          ▲                   │        ├─▶ partial
                          │                   │        ├─▶ rejected   (deterministic — never retried)
                          │                   │        └─▶ error      (attempts exhausted)
                          ├── release ────────┤  transient failure, attempts remain
                          └── stale sweeper ──┘  heartbeat older than STALE_AFTER_SECONDS
```

| Transition | Guard | Set by |
|---|---|---|
| `uploading → uploaded` | — | API, on confirm. Sets `uploaded_at` |
| `uploaded → processing` | claim (§6.2) | API, on worker's claim |
| `processing → processing` | stale takeover (§6.2) | API, on another worker's claim |
| `processing → uploaded` | token matches | API, on worker's release before retry. Resets `uploaded_at` |
| `processing → uploaded` | heartbeat stale, attempts remain | API, stale sweeper |
| `processing → error` | heartbeat stale, attempts exhausted | API, stale sweeper |
| `processing → error` | transient failure on the last attempt | Worker, via `finish` |
| `uploaded → error` | attempts exhausted — backstop | API, reconcile sweeper |
| `processing → terminal` | token matches | API, on worker's finish |

Terminal states are never left. A confirm or claim against a terminal file is a no-op.

---

## 6. Interfaces

### 6.1 Public — POC

| Method · path | Purpose |
|---|---|
| `POST /poc/seed` | Create a batch and file rows for objects already placed in `incoming/`. Stands in for the real presign flow |
| `POST /api/v1/uploads/{file_id}/confirm` | R2 |
| `GET /api/v1/uploads/batches/{batch_id}` | R14.1 |
| `GET /api/v1/uploads/batches/{batch_id}/staged` | R14.2 |
| `POST /poc/scan-stub` | Enqueue `scan_stub(seconds, enqueued_at)` on `clinsync.scan` — R13 |
| `POST /poc/enqueue/{file_id}?times=N` | Publish `process_upload` N times without changing state — S6 only |
| `GET /health` | Liveness |

```jsonc
// POST /poc/seed
{ "files": [ { "file_name": "mixed.zip", "s3_key": "ClinSync/incoming/…/mixed.zip" } ] }
// → 201
{ "batch_id": "…", "files": [ { "file_id": "…", "file_name": "mixed.zip",
                                "is_archive": true, "status": "uploading" } ] }

// POST /api/v1/uploads/{file_id}/confirm
// → 200
{ "file_id": "…", "status": "uploaded", "enqueued": true }
// → 200 when already past uploading — unchanged, not re-enqueued (R2.5)
{ "file_id": "…", "status": "processing", "enqueued": false }

// GET /api/v1/uploads/batches/{batch_id}
{ "batch_id": "…", "files": [
    { "file_id": "…", "file_name": "mixed.zip", "status": "processing",
      "entries_total": 30, "entries_done": 11, "attempt_count": 2,
      "detected_type": "zip", "status_message": null } ] }
```

`is_archive` is set by `/poc/seed` from the extension. The worker then verifies it (R5).

### 6.2 Internal — workers only

Every request carries `X-Internal-Key`. Every write after claim carries `X-Claim-Token`. A token mismatch returns **409 `claim_superseded`** and changes nothing (R4.4).

| Method · path | Body | Returns |
|---|---|---|
| `POST /internal/files/{id}/claim` | — | 200 `FileClaim` · 409 `not_claimable` |
| `POST /internal/files/{id}/heartbeat` | — | 204 · 409 |
| `PATCH /internal/files/{id}/progress` | `{entries_total?, entries_done?, detected_type?}` | 204 · 409 |
| `PUT /internal/files/{id}/candidates` | `Candidate` | 200 · 409 — **upsert** on `uq_entry` |
| `POST /internal/files/{id}/finish` | `{status, status_message?}` | 204 · 409 · 400 — rejecting an archive also rejects its `processed` candidates (§8.5a). For `processed` / `partial` the candidates decide (rev 1.3): `processed` → `partial` if any is rejected; an archive needs exactly `entries_total` candidates, else 400 |
| `POST /internal/files/{id}/release` | `{reason}` | 204 · 409 — back to `uploaded` before a retry; **sets `uploaded_at = now()`** so the reconcile sweeper does not pre-empt the backoff |
| `POST /internal/sweeps/stale` | — | `{reset, errored, enqueue_failed}` |
| `POST /internal/sweeps/reconcile` | — | `{requeued, errored, enqueue_failed}` |

**Claim** — atomic, one statement:

```sql
UPDATE upload_file
SET status = 'processing',
    attempt_count = attempt_count + 1,
    heartbeat_at  = now(),
    claim_token   = gen_random_uuid(),
    updated_at    = now()
WHERE file_id = :file_id
  AND attempt_count < :max_attempts
  AND ( status = 'uploaded'
        OR (status = 'processing' AND heartbeat_at < now() - make_interval(secs => :stale)) )
RETURNING file_id, organization_id, batch_id, s3_key, file_name, file_ext,
          is_archive, entries_done, attempt_count, claim_token;
```

```jsonc
// 200 FileClaim
{ "file_id": "…", "organization_id": "…", "batch_id": "…",
  "s3_key": "ClinSync/incoming/…", "file_name": "mixed.zip", "file_ext": "zip",
  "is_archive": true, "entries_done": 10, "attempt_count": 2,
  "claim_token": "9c1e…" }
```

**Fenced write** — every write after claim includes the token in `WHERE`:

```sql
UPDATE upload_file SET entries_done = :n, heartbeat_at = now(), updated_at = now()
WHERE file_id = :file_id AND claim_token = :token AND status = 'processing';
-- 0 rows → 409 claim_superseded
```

Candidate upsert checks the token on the parent row in the same transaction, then:

```sql
INSERT INTO staged_document (…) VALUES (…)
ON CONFLICT ON CONSTRAINT uq_entry DO UPDATE
SET status = EXCLUDED.status, s3_key = EXCLUDED.s3_key,
    reject_reason = EXCLUDED.reject_reason, size_bytes = EXCLUDED.size_bytes,
    updated_at = now();
```

### 6.2a The API's own Celery client

The API does not import `workers.celery_app`. `app/tasks_client.py` builds its own producer-only instance, sharing queue and task names through `app/constants.py`:

(Rev 1.4: `enqueue_scan_stub` moved to `poc/main.py` and `TASK_SCAN_STUB` to `poc/__init__.py`; they use this producer. The `workers.scan.* → clinsync.scan` route moved to `poc/worker.py`. The §6.3 task name is unchanged.)

```python
# app/tasks_client.py
producer = Celery("clinsync-api", broker=settings.REDIS_URL)
producer.conf.update(
    task_publish_retry=False,                         # R2.4 — fail fast, the sweeper recovers
    broker_connection_retry=False,
    broker_connection_timeout=1,
    broker_transport_options={"socket_connect_timeout": 1, "socket_timeout": 1,
                              "max_retries": 0},                # rev 1.3 — no reconnect loop on publish
)

def enqueue_process_upload(file_id: str, organization_id: str) -> None:
    """Publish process_upload for one file. Raises if Redis is unreachable."""
    producer.send_task(TASK_PROCESS_UPLOAD, kwargs={"file_id": file_id, "organization_id": organization_id},
                       queue=QUEUE_INGEST)
```

Celery's publish defaults retry and can block for seconds when Redis is down, breaking R2.4. **`max_retries: 0`** (rev 1.3): before publishing, Kombu ensures the connection, retrying with a 2 s starting interval until `broker_connection_timeout`; `task_publish_retry` and `broker_connection_retry` do not govern that loop. With Redis stopped (its hostname stops resolving) a confirm took 2.3 s without it and 0.1 s with it. **Producer only:** the workers' Celery app (§7) keeps Kombu's default retries so it reconnects when Redis comes back. With these settings a confirm during an outage fails the publish in about a second; the file stays `uploaded` and the reconcile sweeper recovers it.

### 6.3 Queue contract

```jsonc
// clinsync.ingest
{ "task": "workers.ingest.process_upload",
  "args": [], "kwargs": { "file_id": "…", "organization_id": "…" } }

// clinsync.scan
{ "task": "workers.scan.scan_stub", "kwargs": { "seconds": 10, "enqueued_at": "2026-09-24T10:00:00.123Z" } }

// clinsync.maintenance — scheduled by beat
{ "task": "workers.sweepers.run_stale_sweep" }
{ "task": "workers.sweepers.run_reconcile_sweep" }
```

---

## 7. Celery configuration

```python
# workers/tasks.py — every task is defined in this module, so no include= (rev 1.4)
app = Celery("clinsync", broker=settings.REDIS_URL)

app.conf.update(
    task_acks_late=True,                 # R9.1 — ack after the body, not on receipt
    task_reject_on_worker_lost=True,     # R9.2 — killed child → message back on the queue
    worker_prefetch_multiplier=1,        # R9.3 — one message per process
    task_ignore_result=True,             # state is in PostgreSQL, not a result backend
    task_soft_time_limit=settings.TASK_SOFT_TIME_LIMIT,
    task_time_limit=settings.TASK_TIME_LIMIT,
    broker_transport_options={"visibility_timeout": settings.VISIBILITY_TIMEOUT},  # R9.4
    broker_connection_retry_on_startup=True,
    task_default_queue="clinsync.ingest",
    task_routes={
        "workers.ingest.*":   {"queue": "clinsync.ingest"},
        "workers.sweepers.*": {"queue": "clinsync.maintenance"},
    },
    beat_schedule={
        "stale-sweep":     {"task": "workers.sweepers.run_stale_sweep",
                            "schedule": settings.SWEEP_INTERVAL_SECONDS,
                            "options": {"expires": settings.SWEEP_INTERVAL_SECONDS}},   # rev 1.3
        "reconcile-sweep": {"task": "workers.sweepers.run_reconcile_sweep",
                            "schedule": settings.SWEEP_INTERVAL_SECONDS,
                            "options": {"expires": settings.SWEEP_INTERVAL_SECONDS}},
    },
)
settings.validate()                      # R9.5 — refuse to start on a bad combination
```

| Setting | Why |
|---|---|
| `task_acks_late` | Default Celery acknowledges on receipt. A crash would lose the job with no trace |
| `task_reject_on_worker_lost` | Without it, a SIGKILLed child's message is acknowledged anyway |
| `worker_prefetch_multiplier=1` | Default is 4 — each process would reserve four jobs, idle behind a slow one, and redeliver all four on restart |
| `visibility_timeout > task_time_limit` | Otherwise Redis hands a still-running job to a second worker |
| `task_ignore_result` | Two places recording completion is how they drift |

---

## 8. Algorithms

### 8.1 `process_upload`

```python
# workers/tasks.py
# Adapters raise one class. The S3 helper maps botocore connection errors and 5xx, and the
# internal client maps httpx transport errors and 5xx, to workers.clients.Transient.
RETRYABLE = (Transient, SoftTimeLimitExceeded, OSError, MemoryError)   # rev 1.3 — see below
LIMITS = Limits.from_settings(settings)          # engine/ takes limits as arguments, not settings

@app.task(bind=True, max_retries=None)   # attempt_count, not Celery's counter, bounds attempts
def process_upload(self, file_id: str, organization_id: str) -> None:
    """Verify an uploaded file and stage whatever it contains."""
    claim = internal.claim(file_id)
    if claim is None:
        log.info("claim_lost", file_id=file_id)                  # R3.4 — ack and exit
        return

    api = internal.with_token(file_id, claim.claim_token)
    beat = Heartbeat(api, every=settings.HEARTBEAT_SECONDS).start()   # R10.1
    try:
        local = s3.download_to_tmp(claim.s3_key)                 # heartbeat keeps running
        if claim.is_archive:
            final = process_archive(api, claim, local, beat, LIMITS)
        else:
            final = process_document(api, claim, local, LIMITS)
        api.finish(final)                                        # R10.3
        s3.delete_quietly(claim.s3_key)                          # R10.2 — lifecycle rule is the backstop
    except Rejected as r:                                        # R11.2 — never retried
        api.finish(FinalStatus("rejected", str(r)))
        s3.delete_quietly(claim.s3_key)
    except ClaimSuperseded:
        log.warning("claim_superseded", file_id=file_id)         # another worker owns it now
    except RETRYABLE as e:                                       # R11.1
        if claim.attempt_count >= settings.MAX_ATTEMPTS:         # R11.3 — last attempt: fail, don't release
            api.finish(FinalStatus("error",
                f"Could not be processed after {claim.attempt_count} attempts: {e}"))
            s3.delete_quietly(claim.s3_key)
            return
        api.release(reason=str(e))
        raise self.retry(exc=e, countdown=2 ** claim.attempt_count * 5)
    finally:
        beat.stop()
        cleanup_tmp()
```

**Rev 1.3 — what is retryable, and in what order.** `OSError` and `MemoryError` are environment problems (a full disk) and join `RETRYABLE`, bounded by `MAX_ATTEMPTS` like any other retry. The `except` clauses run in this order: `Rejected` → `S3ObjectNotFound` → `ClaimSuperseded` → the deliberately non-retryable `S3ConfigError` / `WorkerContractError` (incl. `InternalAuthError`), logged as `task_failed` and re-raised → `RETRYABLE` → any other exception (a bug), logged as `task_failed` and re-raised. Re-raised failures leave the file `processing`; the stale sweeper recovers it. None of the non-retryable classes subclasses `OSError`, `MemoryError` or `Transient` (tested). The heartbeat is stopped before `release`; `release` is best effort (a `Transient` there is logged and the retry still raised), and `ClaimSuperseded` during `release` stops without retrying.

**S3 client timeouts** (rev 1.3). `connect_timeout=2`, `read_timeout=3`, standard retries with 2 attempts in total. With a 10 s read timeout and 3 attempts a hung S3 took 30.6 s to fail, silently absorbing a short outage; now 6.7 s (hung) or 0.9 s (unreachable), and the task's release + backoff takes over.

**Why release before retry.** The retried task must claim again. Without releasing, the file stays `processing` with a fresh heartbeat and the retry would lose its own claim race.

**Why release resets `uploaded_at`.** Without it, a released file already looks older than `RECONCILE_AFTER_SECONDS`, and the next reconcile sweep re-enqueues it before the backoff ends — burning attempts in seconds during a short outage.

**Why the last attempt finishes instead of releasing.** Releasing on the final attempt would put the file back in `uploaded` with `attempt_count = MAX_ATTEMPTS`. The claim query refuses it, the reconcile sweeper re-enqueues it every interval, and it never reaches a terminal state. `attempt_count` is the authority because it is incremented by every claim, including claims of sweeper-enqueued tasks that Celery counts as fresh.

**Why finish before delete.** If the delete fails the file is already terminal; the S3 lifecycle rule on `incoming/` removes the orphan. The other order would leave a `processing` row pointing at a deleted object.

### 8.2 `process_document`

```python
def process_document(api, claim, path: Path, limits: Limits) -> FinalStatus:
    d = detect_file_type(path, limits)                           # R5.1
    api.progress(detected_type=d.type)
    if d.type != claim.file_ext:                                 # R5.3
        raise Rejected(mismatch_reason(claim.file_ext, d))
    key = staging_key(claim, index=0, name=claim.file_name)
    s3.copy(claim.s3_key, key)                                   # server-side copy
    api.upsert_candidate(entry_name=None, entry_index=None, file_name=claim.file_name,
                         file_ext=claim.file_ext, size_bytes=path.stat().st_size,
                         s3_key=key, status="processed")        # R5.4
    return FinalStatus("processed")
```

### 8.3 `process_archive` — with resume

```python
def process_archive(api, claim, path: Path, beat, limits: Limits) -> FinalStatus:
    outer = detect_file_type(path, limits)                       # is it really an archive?
    api.progress(detected_type=outer.type)
    if outer.type != "zip":
        raise Rejected(mismatch_reason("zip", outer))            # e.g. a .docx renamed .zip

    try:
      with zipfile.ZipFile(path) as zf:
        entries = inspect_archive(zf, limits)                    # R6 — archive-level
        api.progress(entries_total=len(entries))                 # R7.1
        rejected_any = False

        for i, e in enumerate(entries):                          # R7.5 — directory order
            if i < claim.entries_done:                           # R8.2 — resume
                continue
            beat.raise_if_superseded()                           # stop fast if ownership lost
            name = PurePosixPath(e.filename).name
            ext  = PurePosixPath(name).suffix.lower().lstrip(".")

            if ext not in limits.allowed_entry_ext:              # R7.2
                api.upsert_candidate(entry_name=e.filename, entry_index=i, file_name=name,
                                     file_ext=ext, status="rejected",
                                     reject_reason=f".{ext} is not supported")
                rejected_any = True
            else:
                tmp = extract_streaming(zf, e)                   # R6.7, R6.8 — outer CRC → archive-level
                d = detect_file_type(tmp, limits)                # inner problems → entry-level
                if d.type != ext:                                # R7.3
                    api.upsert_candidate(entry_name=e.filename, entry_index=i, file_name=name,
                                         file_ext=ext, status="rejected",
                                         reject_reason=mismatch_reason(ext, d))
                    rejected_any = True
                else:
                    key = staging_key(claim, index=i, name=name)
                    s3.upload(tmp, key)
                    api.upsert_candidate(entry_name=e.filename, entry_index=i, file_name=name,
                                         file_ext=ext, size_bytes=e.file_size,
                                         s3_key=key, status="processed")   # R7.4
                tmp.unlink()

            api.progress(entries_done=i + 1)                     # R8.1 — after, never before
            poc_delay()                                          # ENTRY_DELAY_SECONDS — POC only

    except Rejected:                                             # §8.5a — archive-level only
        s3.delete_prefix(staging_prefix(claim))                  # delete FIRST, then finish
        raise

    return FinalStatus("partial" if rejected_any else "processed")   # R7.6
```

**Rev 1.3 notes on this loop.**
- **Partial is decided by the API, not by `rejected_any`.** After a resume, entries rejected in an earlier run are skipped, so the worker's `rejected_any` misses them and it would send `processed`. `finish` counts the candidate rows instead: `processed` becomes `partial` if any is rejected (never the reverse), and an archive must have exactly `entries_total` candidates or the finish is refused with 400. The worker keeps sending `partial` when it knows.
- **Per-entry temp files are removed on every path** — success, entry rejection, a failed upload or upsert — in a `finally` around each extracted entry, not only after a successful upload.
- **An entry with no extension** is rejected with "Files without an extension are not supported". An extension longer than the 10-character `file_ext` column is stored truncated on its rejected candidate.
- **Name limits** (R6.10) are checked by `inspect_archive`: file name ≤ 255 UTF-8 bytes, full path ≤ 1024 characters.

**The staging key is deterministic.** `staging_key` is `ClinSync/staging/{org}/{batch}/{file_id}/{index:04d}_{name}`, where `index` is the entry's position **among file entries** — directories are excluded by `inspect_archive`, so this is not the raw central-directory position. `staging_prefix(claim)` is the same path up to `{file_id}/`. A replayed entry writes the same key, so a crash between `s3.upload` and `upsert_candidate` leaves no orphan and no duplicate. A random key per attempt would leak an object every time an entry is replayed.

**The crash windows**, entry by entry:

| Worker dies after… | On resume | Result |
|---|---|---|
| `s3.upload`, before `upsert_candidate` | Entry replayed; same key overwritten; candidate inserted | Correct |
| `upsert_candidate`, before `progress` | Entry replayed; same key; candidate **updated**, not duplicated | Correct |
| `progress` | Entry skipped | Correct |

### 8.4 `detect_file_type`

```python
# engine/file_checks.py — detection section
ZIP = b"PK\x03\x04"
PDF = b"%PDF-"
WORDML = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"

@dataclass(frozen=True)
class Detection:
    type: str                 # docx | zip | pdf | unknown | unsafe | corrupt
    reason: str | None = None

def detect_file_type(path: Path, limits: Limits) -> Detection:
    """Return what the file's bytes are, regardless of its name. Never raises for bad content."""
    with path.open("rb") as f:
        head = f.read(8)
    if head.startswith(PDF):
        return Detection("pdf")
    if not head.startswith(ZIP):
        return Detection("unknown")
    try:
        with zipfile.ZipFile(path) as zf:
            inspect_archive(zf, limits)              # R5.5 — a .docx is a zip; guard it too
            names = set(zf.namelist())
            if {"[Content_Types].xml", "word/document.xml"} <= names:
                with zf.open("[Content_Types].xml") as part:
                    head = part.read(64 * 1024)      # rev 1.3 — bounded; never load the whole part
                if WORDML in head.decode("utf-8", "ignore"):
                    return Detection("docx")
            return Detection("zip")
    except Rejected as r:
        return Detection("unsafe", str(r))
    except UNREADABLE as exc:                        # rev 1.3 — format errors only, see below
        return Detection("corrupt", str(exc))

def mismatch_reason(declared: str, d: Detection) -> str:
    """Plain-language reason a file's content is not what its name claims."""
    if d.type == "unsafe":
        return f"File is unsafe: {d.reason}"
    if d.type == "corrupt":
        return "File is damaged and cannot be read"
    return f"File content does not match .{declared} — it appears to be {d.type}"
```

A `.docx` and a `.zip` share the signature `PK\x03\x04`. Only the contents distinguish them.

**`UNREADABLE`** (rev 1.3) = `BadZipFile`, `LargeZipFile`, `zlib.error`, `EOFError`, `NotImplementedError` — every way `zipfile` reports a **format** problem. With `BadZipFile` alone, a damaged deflate stream escaped as `zlib.error` and an unknown compression method as `NotImplementedError`. Only format errors mean `corrupt` (here) or `Rejected` (in `extract_streaming`). **`OSError` and `MemoryError` are environment problems** — a full disk must not permanently reject a good file — so they propagate and stay retryable.

**Bounded read** (rev 1.3). Only the first 64 KB of `[Content_Types].xml` is read. The part could otherwise be hundreds of MB while passing every guard (ratio 200 allows ~400 MB from 2 MB), breaking NFR-4. The WordML content type always appears near the start.

**Detection classifies; it never raises for bad content.** A `.docx` that fails its own guards returns `unsafe`, and one that cannot be read returns `corrupt`. The caller decides what that means:

| Where | Bad content means | Outcome |
|---|---|---|
| A top-level document | The upload is bad | File `rejected` |
| **An entry inside an archive** | **That one document is bad** | **Entry rejected; the archive continues and ends `partial`** |
| The top-level archive itself | The archive is untrustworthy | Archive `rejected` (§8.5a) |

A bad document inside a good archive is a fact about that document, not evidence that the archive lied.

### 8.5 `inspect_archive` and `extract_streaming`

```python
# engine/file_checks.py — archive checks and extraction sections
def inspect_archive(zf: zipfile.ZipFile, limits: Limits) -> list[zipfile.ZipInfo]:
    """Refuse an archive whose central directory describes something unsafe."""
    entries = [e for e in zf.infolist() if not e.is_dir()]
    if len(entries) > limits.max_entries:
        raise Rejected(f"{len(entries)} files; limit is {limits.max_entries}")        # R6.2
    if sum(e.file_size for e in entries) > limits.max_uncompressed_bytes:
        raise Rejected("Archive is too large once unpacked")                                  # R6.3
    for e in entries:
        if e.compress_size and e.file_size / e.compress_size > limits.max_compression_ratio:
            raise Rejected(f"'{e.filename}' has an implausible compression ratio")           # R6.4
        assert_safe_path(e.filename)                                                          # R6.5
        if e.flag_bits & 0x1:
            raise Rejected(f"'{e.filename}' is encrypted")                                    # R6.6
        # rev 1.3 — R6.9 duplicate names; R6.10 name ≤ 255 UTF-8 bytes, path ≤ 1024 characters
    return entries

def assert_safe_path(name: str) -> None:
    p = PurePosixPath(name.replace("\\", "/"))
    if p.is_absolute() or ".." in p.parts or re.match(r"^[A-Za-z]:", name):
        raise Rejected(f"'{name}' has an unsafe path")

def extract_streaming(zf, e, chunk: int = 64 * 1024) -> Path:
    """Extract one entry; an entry whose bytes disagree with its index is rejected."""
    out, total = tmp_path(), 0
    try:
        with zf.open(e) as src, out.open("wb") as dst:
            while block := src.read(chunk):
                total += len(block)
                if total > e.file_size:          # defence in depth — CPython already truncates
                    raise Rejected(f"'{e.filename}' is larger than its index claims")
                dst.write(block)
    except UNREADABLE as exc:                    # R6.8 — how a lying index actually surfaces (rev 1.3: §8.4)
        out.unlink(missing_ok=True)
        raise Rejected(f"'{e.filename}' does not match its index ({exc})") from exc
    except BaseException:                        # incl. OSError, MemoryError: propagate, stay retryable
        out.unlink(missing_ok=True)              # never leave a partial temp file
        raise
    return out
```

> **How the size guard really works in CPython.** `ZipExtFile` stops returning data once it reaches the entry's declared `file_size`, so an entry can never write more than its index says — and the declared total is already capped by `MAX_ZIP_UNCOMPRESSED_BYTES`. A lying entry therefore cannot exhaust disk or memory. The lie surfaces when the stream ends: the CRC of the truncated bytes does not match, and `BadZipFile("Bad CRC-32")` is raised. `extract_streaming` converts that into `Rejected`. The running-total check is kept as defence in depth for other runtimes but never fires on CPython.

### 8.5a Rejecting an archive mid-way

If an **archive-level** `Rejected` is raised on entry K, entries 0…K-1 may already be staged as `processed`. An untrustworthy archive must not leave some of its contents in the pipeline.

**What counts as archive-level.** Only failures about the *outer* archive: its own guards in `inspect_archive`, or `extract_streaming` finding that an entry's bytes disagree with the outer index (CRC). A bad document *inside* the archive is entry-level (§8.4) and never reaches this path.

**Order: delete, then finish.**
1. The Worker deletes everything under `staging_prefix(claim)` — possible in one call because staging keys are deterministic and grouped by `file_id`.
2. `finish(rejected)` then sets every candidate of that file **currently `processed`** to `rejected` with reason `Archive rejected: {message}` and clears its `s3_key`. Candidates already `rejected` keep their specific reason.

Step 2 runs in the same transaction as `finish`'s fenced UPDATE, which holds the file row lock until the single commit; a takeover or a late upsert waits for it and then sees the file rejected. **`finish(error)` leaves candidates unchanged** (rev 1.3): an archive that runs out of attempts part-way was not found untrustworthy, so its already-staged documents stay `processed`.

Deleting first means a crash between the two steps leaves rows pointing at deleted objects on a file that is still `processing` — the next claim resumes, hits the same failure, deletes again (a no-op) and finishes. The reverse order could leave objects in `staging/` with no row referring to them. A 7-day lifecycle rule on `staging/` is the backstop either way.

### 8.6 Heartbeat

A daemon thread calls `POST /internal/files/{id}/heartbeat` every `HEARTBEAT_SECONDS`. If it receives 409 it sets a `superseded` flag and stops. **Rev 1.3:** a 401 or any other non-retryable 4xx also stops it and is re-raised by the task's next check (a configuration error or worker bug should not keep processing); a 5xx or connection error is logged and beating continues. The main loop checks the flag between entries (`raise_if_superseded`); every write is fenced regardless, so the flag only makes the loser stop sooner.

### 8.7 Sweepers

```python
# app/services/sweeps.py — runs inside the API, called by the sweeper tasks in workers/tasks.py
def stale_sweep(db) -> dict:
    """Recover files whose worker stopped heartbeating."""
    reset = db.execute(text("""
        UPDATE upload_file SET status='uploaded', claim_token=NULL, uploaded_at=now(), updated_at=now()
        WHERE status='processing' AND heartbeat_at < now() - make_interval(secs => :stale)
          AND attempt_count < :max RETURNING file_id, organization_id"""), …).all()
    errored = db.execute(text("""
        UPDATE upload_file SET status='error', claim_token=NULL, updated_at=now(),
               status_message='Processing did not complete after the maximum number of attempts'
        WHERE status='processing' AND heartbeat_at < now() - make_interval(secs => :stale)
          AND attempt_count >= :max RETURNING file_id"""), …).all()
    db.commit()                                           # enqueue only after commit
    failed = enqueue_each(reset)                          # per file; failures logged as enqueue_failed
    return {"reset": len(reset), "errored": len(errored), "enqueue_failed": failed}

def reconcile_sweep(db) -> dict:
    """Re-enqueue files confirmed but never claimed — covers lost Redis messages."""
    rows = db.execute(text("""
        UPDATE upload_file SET uploaded_at = now()
        WHERE status='uploaded' AND uploaded_at < now() - make_interval(secs => :age)
          AND attempt_count < :max
        RETURNING file_id, organization_id"""), …).all()
    errored = db.execute(text("""
        UPDATE upload_file SET status='error', claim_token=NULL, updated_at=now(),
               status_message='Processing did not complete after the maximum number of attempts'
        WHERE status='uploaded' AND uploaded_at < now() - make_interval(secs => :age)
          AND attempt_count >= :max RETURNING file_id"""), …).all()   # backstop for fix in §8.1
    db.commit()                                           # enqueue only after commit
    failed = enqueue_each(rows)                           # per file; failures logged as enqueue_failed
    return {"requeued": len(rows), "errored": len(errored), "enqueue_failed": failed}
```

**The stale reset sets `uploaded_at = now()`, like `release`** (rev 1.3). Otherwise the reset file already looks older than `RECONCILE_AFTER_SECONDS` and the next reconcile sweep enqueues it a second time.

**Enqueue failures** (rev 1.3). Each file is enqueued separately after the commit; one failure does not stop the rest. A failed enqueue is logged as `enqueue_failed` and counted in the result. The file is committed as `uploaded` with a fresh `uploaded_at`, so the reconcile sweep re-enqueues it once `RECONCILE_AFTER_SECONDS` have passed.

**Sweep tasks** (rev 1.3). The sweeper tasks (now in `workers/tasks.py`, rev 1.4) call `POST /internal/sweeps/{stale,reconcile}` and logs the counts. A failed sweep (API down, wrong key, bug) is logged as `sweep_failed` and never retried: the next beat tick is the retry. Each sweep message expires after one interval, so a stuck consumer — or a separate beat service, as in AWS — does not leave a pile of stale sweeps to run at once.

Re-enqueueing a file that already has a message in flight is harmless — the second delivery loses the claim and exits (R3.4). The sweepers can therefore be generous.

**How S5 recovers — the whole container dies.** The Celery parent dies with its child, so nothing rejects the message; it sits unacknowledged in Redis. The heartbeat stops. After `STALE_AFTER_SECONDS` the stale sweeper resets the file to `uploaded` and enqueues a fresh message; the new claim succeeds with `entries_done` intact and the archive resumes. When `VISIBILITY_TIMEOUT` later expires, Kombu restores the original message; it loses the claim because the file is terminal, and is acknowledged.

**How S5b recovers — one child dies.** The parent survives and sees the child lost. `reject_on_worker_lost` puts the message straight back on the queue; it is redelivered within seconds and **loses** the claim, because the dead child's heartbeat is still fresh — that immediate `claim_lost` is the evidence S5b looks for. From there recovery is the same as S5: stale sweeper, fresh message, resume.

---

## 9. Configuration

| Setting | POC | LLD / prod | Constraint |
|---|---|---|---|
| `INGEST_CONCURRENCY` | 2 | 4 | ≥ 1 |
| `MAX_ATTEMPTS` | 3 | 3 | ≥ 1 |
| `TASK_SOFT_TIME_LIMIT` | 120 s | 900 s | < hard limit |
| `TASK_TIME_LIMIT` | 150 s | 1200 s | — |
| `VISIBILITY_TIMEOUT` | 300 s | 10800 s | **> `TASK_TIME_LIMIT`** — startup-validated |
| `HEARTBEAT_SECONDS` | 5 s | 30 s | — |
| `STALE_AFTER_SECONDS` | 30 s | 600 s | **≥ 3 × `HEARTBEAT_SECONDS`** — startup-validated |
| `SWEEP_INTERVAL_SECONDS` | 15 s | 300 s | — |
| `RECONCILE_AFTER_SECONDS` | 30 s | 300 s | — |
| `MAX_ZIP_ENTRIES` | 500 | 500 | — |
| `MAX_ZIP_UNCOMPRESSED_BYTES` | 500 MB | 5 GB | — |
| `MAX_COMPRESSION_RATIO` | 200 | 200 | — |
| `ALLOWED_ZIP_ENTRY_EXT` | `docx` | `docx` | — |
| `ENTRY_DELAY_SECONDS` | 0 · set per scenario (S4: 1 · S5, S5b, S7: 2) | absent | POC only — makes concurrency and kills observable |
| `POC_ORGANIZATION_ID` | fixed UUID | absent | Used by `/poc/seed` in place of org resolution |
| `INTERNAL_API_BASE_URL` | `http://api:8000` | service URL | Workers' target for `/internal/*` |
| `ALLOWED_TOP_LEVEL_EXT` | `docx,zip` | `docx,zip` | `/poc/seed` refuses anything else with 400 (A3) |
| `INTERNAL_API_KEY` | from `.env` | Secrets Manager | non-empty |
| `AWS_ENDPOINT_URL` | `http://localstack:4566` | unset | R1.4 |
| `S3_BUCKET` | `clinsync-poc` | per environment | — |
| `REDIS_URL` | `redis://redis:6379/0` | ElastiCache | — |
| `DATABASE_URL` | `postgresql+psycopg://…@postgres/clinsync` | RDS | — |

POC timings are shortened so every scenario runs in under three minutes. The **constraints** are identical to production and are validated at startup by both the API and the workers.

---

## 10. Scenarios — the definition of done

Each scenario is one script in `scripts/`, runs against a clean `docker compose up`, and exits non-zero if any assertion fails. There are nine: S1–S8 plus S5b.

**Every scenario ends with `assert_no_undeliverable()`** — `LLEN ae.undeliver == 0` — checked by that scenario, not once at the end of the run. S8 wipes Redis, which would otherwise erase evidence of drops from earlier scenarios.

### 10.1 Fixtures — `fixtures/make_fixtures.py`

| Fixture | Built by | Used in |
|---|---|---|
| `valid.docx` | `python-docx`, one paragraph | S2 |
| `renamed_exe.docx` | 4 KB beginning `MZ` | S3 |
| `renamed_zip.docx` | a plain zip of two `.txt` files | S3 |
| `mixed.zip` | 3 × valid `.docx` + `notes.pdf` + `readme.txt` | S1 |
| `big30.zip` | 30 × valid `.docx` | S4, S5 |
| `bomb.zip` | one 200 MB entry of zeros, deflated | unit tests |
| `slip.zip` | one entry named `../../evil.docx` | unit tests |
| `encrypted.zip` | one entry with `flag_bits |= 0x1` | unit tests |
| `lying.zip` | entry whose declared `file_size` is patched below its real size | unit tests |
| `dupnames.zip` | two entries with the same name, `a.docx` (rev 1.3, R6.9) | unit tests |
| `lying3.zip` | three valid `.docx`, stored; the third's central-directory sizes patched to half (rev 1.3) | task 9.3 |
| `inner_bomb.zip` | `a.docx`, `bomb.docx` (= `bomb.zip`, stored), `c.docx` (rev 1.3) | task 9.3 |
| `big100.zip` | 100 valid `.docx` of ~50 KB each (rev 1.3) | NFR-3, task 13.2 |

### 10.2 Scenarios

**S1 · Happy path, mixed archive** — R5, R6, R7, R10
1. Upload `mixed.zip` to `incoming/`; seed; confirm.
2. Wait for terminal.

Assert: status `partial` · `entries_total = 5`, `entries_done = 5` · 3 `processed` candidates with objects in `staging/` · 2 `rejected` candidates with reasons and **no** objects · `incoming/` object deleted.

**S2 · Single document** — R2.3, R5.4
Upload and confirm `valid.docx`. Assert: status `processed` · `detected_type = docx` · 1 candidate · object in `staging/` · `incoming/` empty.

**S3 · Renamed files** — R5.3
Upload and confirm `renamed_exe.docx` and `renamed_zip.docx`. Assert: both `rejected` · messages name `unknown` and `zip` respectively · **zero** candidates · nothing in `staging/`.

**S4 · Concurrency** — R12
With `INGEST_CONCURRENCY=2` and `ENTRY_DELAY_SECONDS=1`, confirm three copies of `big30.zip` together. Poll every 500 ms. Assert: never more than 2 files `processing` at once · the third stays `uploaded` until one finishes · all three end `processed`.

**S5 · Kill mid-archive, resume** — R8, R11.4 · **the demo**
1. `ENTRY_DELAY_SECONDS=2`. Confirm `big30.zip`.
2. Wait until `entries_done ≥ 10`.
3. `docker compose kill -s SIGKILL worker-ingest`; `docker compose up -d worker-ingest`.
4. Wait for terminal (allow `STALE_AFTER_SECONDS + 2 × SWEEP_INTERVAL_SECONDS + 60 s`).

Assert: status `processed` · **exactly 30 candidates** · **30 distinct `staging/` objects**, no orphans · `attempt_count = 2` · logs show the second attempt's first entry is index ≥ 10.

**S5b · Kill one child process** — R9.2
S5 kills the whole container, which takes the Celery parent with it — so `reject_on_worker_lost` never acts. This scenario kills only the child running the task and leaves the parent alive.
1. `ENTRY_DELAY_SECONDS=2`. Confirm `big30.zip`.
2. Wait until `entries_done ≥ 5`. Read the child's `pid` from the `claim_ok` log line.
3. `docker compose exec worker-ingest sh -c 'kill -9 <pid>'` — the slim image has no `/bin/kill`; `kill` is a shell builtin.
4. Wait for terminal.

Assert: the `worker-ingest` container is still running · **within 5 s** of the kill, a `claim_lost` line appears for the same `file_id` — the message was requeued immediately, which only happens with `reject_on_worker_lost` · final status `processed` · exactly 30 candidates · `attempt_count = 2`.

Without `reject_on_worker_lost`, the parent acknowledges the dead child's message and no `claim_lost` appears; recovery then waits for the stale sweeper. The 5-second window is what distinguishes the two.

**S6 · Duplicate delivery** — R3.4, R3.5
Confirm `valid.docx`, then `POST /poc/enqueue/{file_id}?times=5`. Assert: exactly **one** `claim_ok` log line and five `claim_lost` · 1 candidate · `attempt_count = 1`.

**S7 · Queue isolation** — R13
Saturate ingest: `ENTRY_DELAY_SECONDS=2`, confirm three `big30.zip`. While both ingest slots are busy, `POST /poc/scan-stub {"seconds": 1}`. Assert: the scan task's `started` log is within **2 s** of enqueue.

**S8 · Redis loss** — R2.6, R11.5, R11.6, R11.7
Two phases: Redis unreachable when a file is confirmed, and Redis losing messages it already held. The second simulates a broker node replaced with its data gone — not a `FLUSHALL` on a live broker.

*Phase A — Redis down at confirm (R2.6)*
1. `docker compose stop redis`. Seed and confirm `valid_a.docx`. Assert: confirm returns 200 within 2 s, status `uploaded`, API logs `enqueue_failed`.
2. `docker compose start redis`.

*Phase B — queued messages lost (R11.6)*
3. `docker compose stop worker-ingest` so nothing consumes.
4. Seed and confirm `valid_b.docx` and `valid_c.docx` with Redis up. Assert: `LLEN clinsync.ingest ≥ 2` — the messages really are queued.
5. `docker compose stop redis && docker compose rm -f redis && docker volume rm clinsync-ingest-poc_redis-data`.
6. `docker compose up -d redis worker-ingest`.
7. Wait up to `RECONCILE_AFTER_SECONDS + 2 × SWEEP_INTERVAL_SECONDS + 30 s`.

Assert: all three files `processed` · the reconcile sweeper logged `requeued ≥ 3` · `LLEN ae.undeliver` is **0**.

The Redis volume must be named `redis-data` in `docker-compose.yml`, and the compose project named `clinsync-ingest-poc`, so step 5 can address it.

> **Why not `FLUSHALL`.** Kombu's Redis transport keeps queue routing in `_kombu.binding.*` keys. Flushing a live broker deletes them while every client still believes its queues are declared, so published messages match no binding and are diverted to the `ae.undeliver` list — silently. Consumers never see them and the reconcile sweeper's re-enqueues vanish the same way. Never `FLUSHALL` a live broker. If `ae.undeliver` is non-empty after this scenario, the API's producer kept its declaration cache across the reconnect: reset its connection pool in the enqueue error path and re-run. **Rev 1.3:** the producer is now reset after **every** failed publish, with `kombu.pools.reset()` plus discarding the producer instance. A bare `app.pool.force_close_all()` closes the pool for good, and Kombu's process-wide pool registry hands that closed pool to any new producer for the same broker, so every later publish fails with `Acquire on closed pool`.

---

## 11. Error handling

| Error | Class | Action | Terminal state |
|---|---|---|---|
| Guard tripped (R6) | Deterministic | `finish(rejected)` | `rejected` |
| Signature mismatch, single document | Deterministic | `finish(rejected)` | `rejected` |
| Signature mismatch, one entry | Deterministic, entry-level | Rejected candidate; archive continues | `partial` |
| Corrupt zip — `BadZipFile` on open | Deterministic | `finish(rejected, "Archive is damaged")` | `rejected` |
| Outer entry CRC mismatch — lying index | Deterministic, archive-level | Delete staging prefix, then `finish(rejected)`; `processed` candidates become rejected (§8.5a) | `rejected` |
| Inner `.docx` unsafe or corrupt | Deterministic, **entry-level** | Rejected candidate; archive continues | `partial` |
| Top-level `.zip` is not an archive | Deterministic | `finish(rejected)` | `rejected` |
| S3 unreachable | Transient | `release` → retry with backoff | `error` after `MAX_ATTEMPTS` |
| Internal API 5xx / connection error | Transient | `release` (best effort) → retry | `error` after `MAX_ATTEMPTS` |
| Internal API 409 `claim_superseded` | Ownership lost | Stop, no further writes | set by the new owner |
| Soft time limit exceeded | Transient | `release` → retry | `error` after `MAX_ATTEMPTS` |
| Worker process killed | — | Message redelivered; stale sweeper recovers | set by the next claim |
| `incoming/` object missing on download | Deterministic | `finish(error, "Uploaded object not found")` | `error` |

---

## 12. Testing strategy

| Layer | What | How |
|---|---|---|
| Unit — `engine/` | Detector on every fixture; each guard on its fixture; streaming extractor against `lying.zip`; path safety table | pytest, no services |
| Unit — `workers/` | `process_archive` resume logic with a fake internal client recording calls; skip-first-N; deterministic keys | pytest, fake client |
| Integration — repository | Claim race: 10 threads claim one file, exactly 1 wins · stale takeover · fenced write rejects old token · upsert replay yields one row | pytest + compose Postgres |
| Integration — routes | Missing key → 401 · wrong token → 409 · confirm idempotence | FastAPI `TestClient` + Postgres |
| End to end | S1–S8 | `scripts/scenario_s*.py` against full compose |

Coverage gate: `--cov=engine --cov=workers --cov-fail-under=80` (NFR-7), **enforced from task 13.2**. Earlier phases run `pytest` without the gate, since `workers/` has no tests until Phase 7.

---

## 13. Decisions

| # | Decision | Alternative rejected | Reason |
|---|---|---|---|
| D1 | Workers use the Internal API | Direct database access | One access path and audit surface. Pending confirmation (A1) |
| D2 | Claim token fencing on every write | Claim alone | A stale-takeover without fencing lets the old worker keep writing after it wakes |
| D3 | Deterministic staging keys | Random UUID per attempt | Replayed entries would leak an S3 object per attempt |
| D4 | `entries_done` written after each entry | Written once at the end | Resume is impossible without per-entry progress |
| D5 | Release before retry | Retry holding the claim | The retry would lose its own claim race |
| D6 | Sweepers run inside the API, triggered by beat | Sweepers query the database from a worker | Keeps D1 true for sweepers too |
| D7 | `-B` beat embedded in `worker-maint` | Separate beat container | One fewer service for the POC; safe with exactly one instance |
| D8 | Idempotent confirm | Re-enqueue on every confirm | A double-click on the client should not create extra work — the claim would absorb it, but there is no reason to generate it |