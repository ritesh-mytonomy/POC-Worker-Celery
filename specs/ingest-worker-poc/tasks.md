# Tasks — Ingest Worker POC (Celery + Redis)

| | |
|---|---|
| **Feature** | `ingest-worker-poc` |
| **Read first** | `requirements.md` · `design.md` |
| **Convention** | Each task names the requirements it satisfies and ends with a **Done when** that can be checked. Do tasks in order unless marked *parallel*. When the Done-when holds: tick the box, **`git commit`** with the task number in the message, then stop. |

---

## Build order

```
Phase 0  Scaffold ─┐
Phase 1  Environment ─┐
Phase 2  Config & logging ─┐
Phase 3  Data layer ─────────┼─▶ Phase 4  API routes ─┐
Phase 5  Engine  (parallel from Phase 0) ─────────────┼─▶ Phase 6 Celery ─▶ 7 Claim ─▶ 8 Document ─▶ 9 Archive + resume
                                                      │                                                    │
                                                      └──────────────────────▶ 10 Retry ─▶ 11 Sweepers ─▶ 12 Scan stub ─▶ 13 Scenarios ─▶ 14 Handover
```

Phase 5 has no dependencies beyond Phase 0 and can be built by a second person in parallel.

Estimates are rough, for one developer familiar with FastAPI.

---

## Phase 0 · Scaffold  ·  ~1 h

- [x] **0.1 Create the repository skeleton**
  - Create the directory tree in `design.md` §3 with empty `__init__.py` files.
  - Add `requirements.txt`: `fastapi`, `uvicorn[standard]`, `sqlalchemy>=2`, `psycopg[binary]`, `pydantic-settings`, `celery[redis]>=5.3`, `boto3`, `httpx`, `python-docx`, `pytest`, `pytest-cov`.
  - Add `.env.example` with every key in `design.md` §9, POC values.
  - `git init` if the folder is not already a repository. Add a `.gitignore` for Python, `.env` and `fixtures/out/`.
  - Add `pytest.ini` with test paths only — **no coverage gate yet**; it is switched on in task 13.2.
  - **Done when:** `python -c "import app, engine, workers"` succeeds and `pytest` runs (zero tests).
  - _Requirements: NFR-7_

---

## Phase 1 · Environment  ·  ~2 h

- [x] **1.1 Dockerfile — one image**
  - Python 3.11 slim, install requirements, copy source. No `CMD` — each service sets its own.
  - **Done when:** `docker build .` succeeds.
  - _Requirements: R1.3_

- [x] **1.2 PostgreSQL schema**
  - Write `infra/postgres/init.sql` exactly as `design.md` §4.
  - Mount into `/docker-entrypoint-initdb.d/`.
  - **Done when:** `docker compose up postgres` then `\d staged_document` shows `uq_entry` with `NULLS NOT DISTINCT`.
  - _Requirements: R1.2, R8.3_

- [x] **1.3 LocalStack and bucket**
  - `infra/localstack/init-s3.sh` in `/etc/localstack/init/ready.d/`: create `clinsync-poc`; add a lifecycle rule expiring `ClinSync/incoming/` after 1 day.
  - Also add a 7-day lifecycle rule on `ClinSync/staging/` — the backstop in `design.md` §8.5a.
  - **Done when:** `docker compose exec localstack awslocal s3 ls` lists `clinsync-poc`, and `awslocal s3api get-bucket-lifecycle-configuration --bucket clinsync-poc` shows both rules.
  - _Requirements: R1.2, R1.4_

- [x] **1.4 docker-compose.yml**
  - Top-level `name: clinsync-ingest-poc`. Seven services per `design.md` §2. Redis with `--appendonly yes` and a **named volume `redis-data`** (S8 removes it). Healthchecks on postgres, redis, localstack. `api` and workers `depends_on: condition: service_healthy`.
  - All app services use the same `build: .` with different `command:`.
  - **Done when:** `docker compose up` brings every service to healthy / running; `docker compose ps` shows seven.
  - _Requirements: R1.1, R1.3_

**Checkpoint A** — clean clone to running stack with one command.

---

## Phase 2 · Config & logging  ·  ~1.5 h

- [x] **2.1 Settings with startup validation**
  - `app/config.py` with `pydantic-settings`, every key in `design.md` §9.
  - `validate()` raises on: `VISIBILITY_TIMEOUT <= TASK_TIME_LIMIT`; `STALE_AFTER_SECONDS < 3 * HEARTBEAT_SECONDS`; `TASK_SOFT_TIME_LIMIT >= TASK_TIME_LIMIT`; empty `INTERNAL_API_KEY`.
  - Call `validate()` in API startup and in `workers/celery_app.py` at import.
  - **Done when:** a unit test for each invalid combination asserts it raises; setting `VISIBILITY_TIMEOUT=100` in `.env` makes both `api` and `worker-ingest` exit on start.
  - _Requirements: R9.4, R9.5_

- [ ] **2.2 JSON logging**
  - `app/logging.py`: one JSON object per line with `ts`, `level`, `event`, plus any `extra`. Used by API and workers.
  - **Done when:** `docker compose logs api` shows JSON lines with `event`.
  - _Requirements: R15.1_

---

## Phase 3 · Data layer  ·  ~4 h

- [ ] **3.1 Models and session**
  - `app/models.py` SQLAlchemy models for the three tables. `app/db.py` with `get_db_session()`.
  - **Done when:** a test inserts and reads a batch and a file.

- [ ] **3.2 Claim**
  - `repositories/files.py::claim(file_id)` using the single `UPDATE … RETURNING` in `design.md` §6.2.
  - **Done when:** `tests/test_repository_files_claim.py`:
    - claim on `uploaded` succeeds, returns a token, `attempt_count = 1`
    - claim on `processing` with fresh heartbeat returns `None`
    - claim on `processing` with heartbeat older than stale succeeds, **new** token, `attempt_count = 2`
    - claim on each terminal status returns `None`
    - claim with `attempt_count = MAX_ATTEMPTS` returns `None`
    - **10 threads** claim one `uploaded` file concurrently → exactly **1** non-`None`
  - _Requirements: R3.2, R3.3, R3.5_

- [ ] **3.3 Fenced writes**
  - `heartbeat`, `progress`, `finish`, `release` — each `UPDATE … WHERE claim_token = :token AND status = 'processing'`; raise `ClaimSuperseded` on 0 rows.
  - `finish` sets terminal status and clears `claim_token`. `release` sets `uploaded` and clears it.
  - **Done when:** tests show each write succeeds with the current token and raises with an old token after a stale takeover.
  - _Requirements: R4.4, R10.3_

- [ ] **3.4 Candidate upsert**
  - `repositories/candidates.py::upsert(...)` — check parent token in the same transaction, then `INSERT … ON CONFLICT ON CONSTRAINT uq_entry DO UPDATE`.
  - **Done when:** tests show: inserting the same `(source_file_id, source_entry_name)` twice yields **one** row with the second call's values; the same for `source_entry_name = NULL`.
  - _Requirements: R8.3_

- [ ] **3.5 Sweep queries**
  - `services/sweeps.py::stale_sweep` and `reconcile_sweep` per `design.md` §8.7. Enqueue via `tasks_client` — stub it for now.
  - **Done when:** tests seed stale and fresh rows and assert only stale ones move, and exhausted ones go to `error`.
  - _Requirements: R11.4, R11.5_

**Checkpoint B** — every state transition in `design.md` §5 is covered by a passing test.

---

## Phase 4 · API routes  ·  ~3 h

- [ ] **4.1 Internal auth**
  - `app/security.py::require_internal_key` — constant-time compare against `INTERNAL_API_KEY`; 401 otherwise.
  - **Done when:** a route test without the header returns 401.
  - _Requirements: R4.3_

- [ ] **4.2 Internal routes**
  - All eight `/internal/*` routes in `design.md` §6.2. Map `ClaimSuperseded` → 409 `claim_superseded`; claim `None` → 409 `not_claimable`.
  - **Done when:** `tests/test_routes_internal_fencing.py` covers 200/204 with the right token and 409 with a wrong one, for every write route.
  - _Requirements: R3.1, R4.2, R4.4_

- [ ] **4.3 POC seed**
  - `POST /poc/seed` — create batch and file rows (`status = uploading`, `is_archive` from extension) for objects the caller has already placed in S3. Uses `POC_ORGANIZATION_ID`. Refuses any extension not in `ALLOWED_TOP_LEVEL_EXT` with 400.
  - `POST /poc/enqueue/{file_id}?times=N` — publishes `process_upload` N times without changing state. Used by S6.
  - **Done when:** `curl` returns ids and rows exist.

- [ ] **4.4 Confirm and enqueue**
  - `POST /api/v1/uploads/{file_id}/confirm`:
    - from `uploading`: set `uploaded` and `uploaded_at`, commit, **then** enqueue; return `enqueued: true`
    - from any later status: return current status, `enqueued: false`
    - if enqueue raises (Redis down): log `enqueue_failed`, still return 200 `uploaded`
  - `tasks_client` is a **producer-only Celery instance** with the settings in `design.md` §6.2a — the API never imports `workers.celery_app`.
  - **Done when:** tests cover all three branches; with Redis stopped, confirm returns 200 **within 2 s**.
  - _Requirements: R2.1–R2.6, NFR-1_

- [ ] **4.5 Status routes**
  - `GET /api/v1/uploads/batches/{batch_id}` and `/staged` per `design.md` §6.1.
  - **Done when:** both return the fields listed in R14.
  - _Requirements: R14.1, R14.2_

---

## Phase 5 · Engine  ·  ~4 h  ·  *parallel from Phase 0*

- [ ] **5.1 Fixtures**
  - `fixtures/make_fixtures.py` builds every fixture in `design.md` §10.1 into `fixtures/out/`.
  - `encrypted.zip`: write normally, then set `flag_bits |= 0x1` on the `ZipInfo` in both local header and central directory.
  - `lying.zip`: write one entry, then patch its declared uncompressed size in the central directory to a smaller value.
  - **Done when:** running it produces all nine files.

- [ ] **5.2 `detect_file_type`**
  - Per `design.md` §8.4. Returns a `Detection`; **never raises** for bad content. Takes a `Limits` argument — see `engine/limits.py`.
  - **Done when:** `tests/test_engine_file_signature.py` asserts `valid.docx → docx`, `renamed_exe.docx → unknown`, `renamed_zip.docx → zip`, `mixed.zip → zip`, a PDF header → `pdf`, `bomb.zip` renamed `.docx` → `unsafe` with a reason, a truncated zip → `corrupt`.
  - _Requirements: R5.1, R5.2, R5.5_

- [ ] **5.3 `inspect_archive` and `assert_safe_path`**
  - Per `design.md` §8.5.
  - **Done when:** `tests/test_engine_archive.py` rejects `bomb.zip` (ratio), `slip.zip` (path), `encrypted.zip`, an archive with `MAX_ZIP_ENTRIES + 1` entries, and one over the size cap; accepts `mixed.zip`. Path table covers `/abs`, `a/../b`, `C:\x`, `..\\x`.
  - _Requirements: R6.1–R6.6_

- [ ] **5.4 `extract_streaming`**
  - 64 KB chunks, running total checked against declared size.
  - Wrap the read loop to convert `zipfile.BadZipFile` into `Rejected` — see the note under `design.md` §8.5.
  - **Done when:** extracting from `lying.zip` raises `Rejected` whose message mentions the CRC; peak memory while extracting a 200 MB entry stays under 64 MB (`tracemalloc`).
  - _Requirements: R6.7, R6.8, NFR-4_

**Checkpoint C** — `pytest tests/test_engine_*` passes with nothing running.

---

## Phase 6 · Celery  ·  ~1.5 h

- [ ] **6.1 Celery app**
  - `workers/celery_app.py` exactly per `design.md` §7, including routes and beat schedule.
  - **Done when:** `celery -A workers.celery_app inspect conf` from `worker-ingest` shows `task_acks_late: True`, `worker_prefetch_multiplier: 1`, and the three queues.
  - _Requirements: R9.1–R9.4, R13.1_

- [ ] **6.2 Internal client**
  - `workers/internal_client.py` — `httpx.Client` with `X-Internal-Key`; `claim()` returns `FileClaim | None`; `with_token()` returns a bound client adding `X-Claim-Token`; 409 `claim_superseded` → `ClaimSuperseded`; 5xx and transport errors → `Transient`.
  - **Done when:** unit tests with `httpx.MockTransport` cover each mapping.
  - _Requirements: R4.1, R4.2_

---

## Phase 7 · Claim-only task  ·  ~1.5 h

- [ ] **7.1 `process_upload` skeleton**
  - Claim; on `None` log `claim_lost` and return. On success log `claim_ok`, then immediately `finish(processed)` — no file work yet.
  - **Done when:** seeding + confirming a file ends `processed` with `attempt_count = 1`.
  - _Requirements: R3.1, R3.4_

- [ ] **7.2 Heartbeat thread**
  - `workers/heartbeat.py` per `design.md` §8.6. Start after claim, stop in `finally`.
  - **Done when:** with an artificial 20 s sleep in the task, `heartbeat_at` advances every `HEARTBEAT_SECONDS`.
  - _Requirements: R10.1_

- [ ] **7.3 Early scenario: S6**
  - Write `scripts/lib.py` helpers and `scripts/scenario_s6.py`.
  - **Done when:** S6 passes — one `claim_ok`, five `claim_lost`.
  - _Requirements: R3.4, R3.5_

**Checkpoint D** — the claim guarantee is proven end to end before any file processing exists.

---

## Phase 8 · Single document  ·  ~2 h

- [ ] **8.1 S3 helper**
  - `download_to_tmp` (streamed), `copy` (server-side), `upload`, `delete_quietly` — boto3 with `endpoint_url=AWS_ENDPOINT_URL` when set.
  - **Done when:** a smoke test round-trips a file through LocalStack.
  - _Requirements: R1.4_

- [ ] **8.2 `process_document`**
  - Per `design.md` §8.2. Deterministic staging key `…/{file_id}/0000_{name}`. Finish, then delete from `incoming/`.
  - **Done when:** S2 and S3 pass.
  - _Requirements: R5.1–R5.4, R10.2, R10.3_

---

## Phase 9 · Archive and resume  ·  ~4 h

- [ ] **9.1 `process_archive`**
  - Per `design.md` §8.3, including the outer `detect_file_type` check, skip-first-N, per-entry `progress`, deterministic keys `…/{file_id}/{index:04d}_{name}`, delete-prefix-then-reraise on archive-level `Rejected`, and `poc_delay()` reading `ENTRY_DELAY_SECONDS`.
  - **Done when:** a unit test with a fake internal client and `claim.entries_done = 3` on a 5-entry archive records upserts for indices 3 and 4 only, then `progress(entries_done=5)`.
  - _Requirements: R7.1–R7.6, R8.1, R8.2_

- [ ] **9.2 Scenario S1**
  - **Done when:** S1 passes — `partial`, 3 processed, 2 rejected, no objects for rejected, `incoming/` empty.
  - _Requirements: R7, R10.2_

- [ ] **9.3 Rejecting an archive mid-way**
  - Worker deletes `staging_prefix(claim)` **first**, then `finish(rejected)`; the API rejects only candidates currently `processed`. See `design.md` §8.5a.
  - **Done when:** an archive whose third entry fails the **outer** CRC ends `rejected`, candidates 1–2 rejected with `Archive rejected: …`, and no objects under its staging prefix · an archive containing one **inner** `.docx` that is a zip bomb ends `partial`, with that entry rejected as `unsafe` and every other entry `processed`.
  - _Requirements: R6.8_

- [ ] **9.4 Superseded stop**
  - Heartbeat sets `superseded` on 409; loop calls `beat.raise_if_superseded()` before each entry.
  - **Done when:** a unit test flips the flag mid-loop and asserts no further upserts.
  - _Requirements: R4.4_

---

## Phase 10 · Retry and terminal failure  ·  ~2 h

- [ ] **10.1 Exception classification**
  - `engine/errors.py`: `Rejected` (deterministic) and `Transient`. The S3 helper and the internal client are the only places that raise `Transient`. Map `BadZipFile` → `Rejected("Archive is damaged")`; missing `incoming/` object → finish `error`.
  - _Requirements: R11.1, R11.2_

- [ ] **10.2 Release and retry**
  - Exactly per `design.md` §8.1: on `Transient` or `SoftTimeLimitExceeded`, **if `claim.attempt_count >= MAX_ATTEMPTS` call `finish(error)`**; otherwise `release()` (which resets `uploaded_at`) then `self.retry(countdown=2 ** claim.attempt_count * 5)`.
  - **Done when:** with LocalStack stopped for **15 s** mid-task, the file retries and ends `processed` with `attempt_count ≤ 3`; with LocalStack stopped permanently, it ends `error` after exactly `MAX_ATTEMPTS` claims, with a message, and is never re-enqueued afterwards.
  - _Requirements: R11.1, R11.3_

---

## Phase 11 · Sweepers  ·  ~2 h

- [ ] **11.1 Sweep routes and tasks**
  - `/internal/sweeps/stale` and `/reconcile` call the services from 3.5, now with real enqueue.
  - `workers/sweepers.py` tasks call those routes. Beat schedule from 6.1 runs them in `worker-maint`.
  - **Done when:** `docker compose logs worker-maint` shows both sweeps every `SWEEP_INTERVAL_SECONDS` with counts.
  - _Requirements: R10.4, R11.4, R11.5_

- [ ] **11.2 Scenarios S5, S5b and S8**
  - Log `pid` (`os.getpid()`) on every worker line so S5b can target the child.
  - **Done when:**
    - S5 passes — exactly 30 candidates, 30 distinct staging objects, `attempt_count = 2`, resumed at index ≥ 10
    - S5b passes — container still up, `claim_lost` within 5 s of the child kill, 30 candidates
    - S8 passes — phase A confirm returns 200 within 2 s with Redis down; phase B messages verifiably queued, then lost, then recovered; all three `processed`
  - _Requirements: R8.4, R9.2, R11.6_

**Checkpoint E** — the open question from 18 Sep is answered and demonstrable.

---

## Phase 12 · Scan stub  ·  ~1 h

- [ ] **12.1 `scan_stub` and route**
  - `workers/scan.py::scan_stub(seconds, enqueued_at)` logs `scan_started` with `enqueued_at` and the start time, sleeps, logs `scan_finished`. `POST /poc/scan-stub` enqueues it on `clinsync.scan`.
  - **Done when:** S7 passes — scan starts within 2 s while both ingest slots are busy.
  - _Requirements: R13.2_

- [ ] **12.2 Scenario S4**
  - **Done when:** S4 passes — never more than `INGEST_CONCURRENCY` files `processing` at once.
  - _Requirements: R12.1, R12.2_

---

## Phase 13 · Scenario suite  ·  ~2 h

- [ ] **13.1 Runner**
  - `scripts/run_all.sh`: `docker compose down -v && docker compose up -d --wait`, regenerate fixtures, run S1–S8 and S5b in order, print a pass/fail table, exit non-zero on any failure.
  - Scenarios needing `ENTRY_DELAY_SECONDS` restart only `worker-ingest` with the override.
  - **Done when:** `./scripts/run_all.sh` prints nine passes from a clean machine. Each scenario asserts `LLEN ae.undeliver == 0` itself before exiting.
  - _Requirements: §5 Definition of done_

- [ ] **13.2 Non-functional checks**
  - Add timing assertions: confirm p95 over 50 calls (NFR-1), confirm-to-claim (NFR-2), 100-entry archive end to end (NFR-3).
  - Assert across all scenarios: zero duplicate candidates (NFR-5), zero non-terminal files after sweeps (NFR-6).
  - Add `--cov=engine --cov=workers --cov-fail-under=80` to `pytest.ini`.
  - **Done when:** all pass and coverage ≥ 80 % (NFR-7).

---

## Phase 14 · Handover  ·  ~1 h

- [ ] **14.1 README**
  - Prerequisites, `docker compose up`, `run_all.sh`, how to read logs, the seven services and what each does.

- [ ] **14.2 Demo runbook — S5 and S5b**
  - A five-minute script for showing the resume live: which terminal shows what, the exact kill command, what to point at in the logs, and the final assertion.

- [ ] **14.3 Findings note**
  - One page back to Tasneem: what the POC proved, the POC timings versus the production values in `design.md` §9, and answers to open questions A1 and A2 in `requirements.md` §4.

---

## Total

About **33 hours** for one developer, or about **25** with Phase 5 built in parallel.

## Traceability

| Requirement | Tasks |
|---|---|
| R1 Environment | 1.1–1.4, 8.1 |
| R2 Enqueue | 4.4 |
| R3 Claim | 3.2, 4.2, 7.1, 7.3 |
| R4 Isolation | 4.1, 4.2, 6.2, 9.3 |
| R5 Document verification | 5.2, 8.2 |
| R6 Archive guards | 5.3, 5.4, 9.3 |
| R7 Entry processing | 9.1, 9.2 |
| R8 Resume | 3.4, 9.1, 11.2 |
| R9 Delivery | 2.1, 6.1, 11.2 |
| R10 Liveness | 3.3, 7.2, 8.2, 11.1 |
| R11 Retry & recovery | 3.5, 10.1, 10.2, 11.1, 11.2 |
| R12 Concurrency | 12.2 |
| R13 Isolation | 6.1, 12.1 |
| R14 Status | 4.5 |
| R15 Observability | 2.2, 7.1 |
| NFR-1–7 | 4.4, 5.4, 13.2, 0.1 |