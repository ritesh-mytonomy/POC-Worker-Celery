# Requirements — Ingest Worker POC (Celery + Redis)

| | |
|---|---|
| **Feature** | `ingest-worker-poc` |
| **Status** | Ready to build |
| **Spec files** | `requirements.md` (this) · `design.md` · `tasks.md` |
| **Source** | Tasneem Sharma — design calls of 18 Sep and 24 Sep 2026 · ClinSync LLD §M2.2.4–M2.2.6 |
| **Notation** | Acceptance criteria use EARS: *WHEN / IF / WHILE / WHERE … THE [component] SHALL …* |
| **Revision** | 1.3 — build-time review; see §6 |

---

## 1. Introduction

ClinSync replaces an S3 → EventBridge → SQS → Lambda → ECS chain with **Redis as a message broker and Celery workers**, to reduce moving parts, allow the whole pipeline to run on a laptop and in SIT, and keep API and workers in one repository and one Docker image.

This POC proves the background half of the upload pipeline in isolation: **from the moment a file sits in `incoming/` and the API has been told, to the moment its contents are staged or rejected.** It must demonstrate, on demand, that the design survives a worker being killed mid-job — the one question left open on 18 Sep:

> "If my worker service terminates in between… I have already put 10 documents… there are 20 more files in that zip file which have to be read. So I should be able to read the next 20. That's the only challenge over here." — Tasneem, 18 Sep

### 1.1 Scope

**In scope**
- Local environment: LocalStack (S3), Redis, PostgreSQL, API, two worker pools
- Enqueue on upload confirmation
- Worker claim, file-signature verification, archive guards, entry extraction, staging, cleanup
- Resume after crash, duplicate-delivery safety, retry and terminal failure
- Stale-job and lost-message recovery (sweepers)
- Queue isolation between ingest and scan
- Per-file status visible through the API
- A scripted scenario for each property above

**Out of scope** — already designed in the LLD, not what this POC proves
- Presigned URLs, multipart upload, browser code
- Duplicate check, staging screen, commit, library
- Authentication and organization resolution (a fixed test organization is used)
- The scan engine (a stub task stands in)
- AWS deployment

### 1.2 Glossary

| Term | Meaning |
|---|---|
| **API** | The FastAPI process. The only component that reads or writes PostgreSQL |
| **Worker** | A Celery worker process consuming `clinsync.ingest` |
| **Scan worker** | A Celery worker process consuming `clinsync.scan` |
| **Sweeper** | Periodic tasks run by Celery beat |
| **Internal API** | `/internal/*` endpoints on the API, guarded by `X-Internal-Key`, called only by workers |
| **Claim** | A conditional database update that makes one worker the owner of a file |
| **Claim token** | A random value written at claim time; every subsequent worker write must present it |
| **Candidate** | A `staged_document` row — one potential library document |
| **Entry** | One file inside an archive |
| **incoming/ · staging/** | S3 prefixes under `ClinSync/` in the POC bucket |

---

## 2. Requirements

### R1 · Local environment

**User story** — As a developer, I want the whole pipeline to start with one command, so that anyone can run and test it without an AWS account.

1. WHEN `docker compose up` is run THE environment SHALL start LocalStack, Redis, PostgreSQL, the API, one ingest worker pool, one scan worker pool and Celery beat.
2. WHEN the environment starts THE environment SHALL create the S3 bucket and apply the database schema without manual steps.
3. THE API, ingest worker, scan worker and beat SHALL run from **one Docker image**, differing only in start command.
4. WHERE the POC runs against LocalStack THE code SHALL use the standard boto3 client with only the endpoint URL changed, so that no code change is needed to target AWS.

### R2 · Enqueue on confirmation

**User story** — As the API, I want to hand a file to the background the moment its upload is confirmed, so that the request returns immediately.

1. WHEN `POST /api/v1/uploads/{file_id}/confirm` is called for a file in status `uploading` THE API SHALL set its status to `uploaded` and enqueue `process_upload` on queue `clinsync.ingest`.
2. THE message SHALL contain only `file_id` and `organization_id`. It SHALL NOT contain an S3 key or any file content.
3. THE API SHALL enqueue for **every** file, archive or not.
4. WHILE Redis is reachable THE API SHALL return within 200 ms regardless of file size.
   - IF Redis is unreachable THEN THE API SHALL return within 2 s. Publishing uses a bounded connect timeout and no publish retry.
5. IF confirm is called for a file already in `uploaded`, `processing`, `processed`, `partial` or `rejected` THEN THE API SHALL return the current status and SHALL NOT change it.
6. IF Redis is unreachable at confirm time THEN THE API SHALL still set status `uploaded` and return 200, and recovery SHALL be left to the reconciliation sweeper (R11).

### R3 · Claim — exactly one owner

**User story** — As the system, I want at most one worker processing a file at a time, so that no file is processed twice concurrently.

1. WHEN a Worker receives `process_upload` THE Worker SHALL claim the file through the Internal API before any other action.
2. THE claim SHALL succeed only if the file is `uploaded`, or is `processing` with a heartbeat older than `STALE_AFTER_SECONDS`.
3. WHEN a claim succeeds THE API SHALL set status `processing`, increment `attempt_count`, set `heartbeat_at` to now, and issue a new claim token.
4. IF a claim does not succeed THEN THE Worker SHALL acknowledge the message and exit **without downloading the file**.
5. WHEN two Workers claim the same file simultaneously THE API SHALL let exactly one succeed.

### R4 · Worker isolation

**User story** — As an architect, I want workers to reach state only through the API, so that there is one access path and one audit surface for all data.

1. THE Worker SHALL NOT open a database connection.
2. THE Worker SHALL read and write file and candidate state only through the Internal API.
3. WHEN an Internal API request lacks a valid `X-Internal-Key` THE API SHALL respond 401.
4. WHEN a Worker write presents a claim token that is not the file's current token THE API SHALL respond 409 and SHALL NOT apply the write.

### R5 · Single-document verification

**User story** — As the system, I want every uploaded document checked for what it really is, so that a renamed executable or archive is never staged as a Word document.

1. WHEN the claimed file is not an archive THE Worker SHALL download it and determine its type from its content, not its name.
2. THE Worker SHALL identify a `.docx` as a zip container holding `[Content_Types].xml` that declares a WordprocessingML main part, and `word/document.xml`.
3. IF the detected type differs from the declared extension THEN THE Worker SHALL mark the file `rejected` with a message naming both, and SHALL NOT create a candidate.
4. WHEN the detected type matches THE Worker SHALL copy the file to `staging/` and create one `processed` candidate.
5. BECAUSE a `.docx` is a zip, THE Worker SHALL apply the archive guards of R6 to every `.docx`.

### R6 · Archive guards

**User story** — As an operator, I want unsafe archives refused before they are unpacked, so that one malicious upload cannot exhaust a worker.

1. WHEN the claimed file is an archive THE Worker SHALL read its central directory before extracting anything.
2. IF the archive has more than `MAX_ZIP_ENTRIES` entries THEN THE Worker SHALL reject the whole archive.
3. IF the declared uncompressed total exceeds `MAX_ZIP_UNCOMPRESSED_BYTES` THEN THE Worker SHALL reject the whole archive.
4. IF any entry's compression ratio exceeds `MAX_COMPRESSION_RATIO` THEN THE Worker SHALL reject the whole archive.
5. IF any entry path is absolute, contains `..`, or contains a drive letter THEN THE Worker SHALL reject the whole archive.
6. IF any entry is encrypted THEN THE Worker SHALL reject the whole archive.
7. WHILE extracting an entry THE Worker SHALL read in chunks of at most 64 KB and SHALL NOT write more bytes than the entry's declared size.
8. IF an entry's bytes do not match its central-directory record — size or CRC — THEN THE Worker SHALL reject the whole archive.
9. IF two entries have the same name THEN THE Worker SHALL reject the whole archive.

### R7 · Archive entry processing

**User story** — As a content operator, I want every file in a zip accounted for, so that nothing I dropped silently disappears.

1. WHEN an archive passes the guards THE Worker SHALL record `entries_total`.
2. FOR each entry, IF its extension is not in `ALLOWED_ZIP_ENTRY_EXT` THEN THE Worker SHALL create a `rejected` candidate with a reason and SHALL NOT store the entry.
3. FOR each entry with an allowed extension, IF its detected type does not match THEN THE Worker SHALL create a `rejected` candidate with a reason and SHALL NOT store the entry.
4. FOR each entry that passes, THE Worker SHALL upload it to `staging/` and create a `processed` candidate.
5. THE Worker SHALL process entries in central-directory order.
6. WHEN all entries are handled THE Worker SHALL set the archive's status to `processed` if every entry passed, or `partial` if any entry was rejected.

### R8 · Resume after interruption

**User story** — As the system, I want a restarted job to continue where the previous one stopped, so that a crash on entry 11 of 30 does not redo entries 1–10 or leave them duplicated.

1. AFTER each entry is handled THE Worker SHALL record `entries_done` through the Internal API.
2. WHEN a Worker claims an archive with `entries_done = N` THE Worker SHALL skip the first N entries.
3. THE API SHALL write candidates with an upsert keyed on `(source_file_id, source_entry_name)`, so that replaying an entry overwrites rather than duplicates.
4. WHEN a Worker process is killed during entry K of an M-entry archive and the job is later retried THEN the final state SHALL contain exactly M candidates and no duplicates. This SHALL hold both when the whole worker container dies (S5) and when only one child process dies (S5b).

### R9 · Delivery guarantees

**User story** — As the system, I want a job to survive a worker crash, so that no confirmed upload is ever silently dropped.

1. THE Worker SHALL acknowledge a message only after the task body completes (`acks_late`).
2. WHEN a worker child process dies during a task THE message SHALL return to the queue (`reject_on_worker_lost`).
3. THE Worker SHALL prefetch at most one message per process.
4. THE Redis visibility timeout SHALL exceed the task hard time limit.
5. IF the configured visibility timeout does not exceed the hard time limit THEN THE Worker and THE API SHALL refuse to start.

### R10 · Liveness and cleanup

1. WHILE processing THE Worker SHALL send a heartbeat at least every `HEARTBEAT_SECONDS`, including during download.
2. WHEN the Worker sets a terminal status THE Worker SHALL delete the object from `incoming/`. Files set to `error` by a sweeper are removed by the `incoming/` lifecycle rule.
3. WHEN processing finishes THE Worker SHALL set exactly one terminal status: `processed`, `partial`, `rejected` or `error`.
4. A file SHALL NOT remain in `processing` with a stale heartbeat for longer than `STALE_AFTER_SECONDS` plus one sweeper interval.

### R11 · Retry, terminal failure and recovery

1. IF a transient error occurs (S3 unavailable, Internal API 5xx, connection reset) THEN THE Worker SHALL retry the task with exponential backoff, up to `MAX_ATTEMPTS`.
2. IF a deterministic error occurs (guard tripped, signature mismatch) THEN THE Worker SHALL NOT retry, and SHALL set `rejected`.
3. IF a transient error occurs on a claim whose `attempt_count` has reached `MAX_ATTEMPTS` THEN THE Worker SHALL set status `error` with a message **instead of** releasing and retrying.
   - `attempt_count`, incremented at every claim, is the single authority on attempts. Celery's own retry counter is not used as a limit, because sweeper re-enqueues start new tasks at zero.
4. THE stale sweeper SHALL, every `SWEEP_INTERVAL_SECONDS`, return to `uploaded` and re-enqueue any file in `processing` whose heartbeat is older than `STALE_AFTER_SECONDS` and whose `attempt_count` is below `MAX_ATTEMPTS`, and set `error` otherwise.
5. THE reconciliation sweeper SHALL, every `SWEEP_INTERVAL_SECONDS`, re-enqueue any file in `uploaded` for longer than `RECONCILE_AFTER_SECONDS` whose `attempt_count` is below `MAX_ATTEMPTS`, and set `error` on any such file whose `attempt_count` has reached it.
6. WHEN Redis restarts with its data lost while files are `uploaded` THE reconciliation sweeper SHALL cause every such file to reach a terminal status.
7. THE system SHALL NOT silently drop a published message: after any scenario, the Kombu dead-letter list `ae.undeliver` SHALL be empty.

### R12 · Concurrency

1. THE number of files processed at once SHALL be at most `INGEST_CONCURRENCY`.
2. WHEN more files are queued than `INGEST_CONCURRENCY` THE remaining files SHALL wait in `uploaded` until a slot frees.

### R13 · Queue isolation

1. THE ingest and scan workloads SHALL use separate queues, `clinsync.ingest` and `clinsync.scan`, consumed by separate worker pools.
2. WHILE every ingest worker is busy THE scan worker SHALL still start a `clinsync.scan` task within 2 seconds of it being enqueued.

### R14 · Status visibility

1. WHEN `GET /api/v1/uploads/batches/{batch_id}` is called THE API SHALL return every file in the batch with `status`, `status_message`, `entries_total`, `entries_done`, `attempt_count` and `detected_type`.
2. WHEN `GET /api/v1/uploads/batches/{batch_id}/staged` is called THE API SHALL return every candidate with `status`, `file_name`, `source_entry_name` and `reject_reason`.

### R15 · Observability

1. EVERY log line from the API and Worker SHALL be JSON and SHALL carry `event`, and where applicable `file_id`, `batch_id`, `task_id`, `attempt`, `claim_token` and — in worker logs — `pid`.
2. THE Worker SHALL log one line per claim, per entry, per rejection, per retry and per terminal status.

---

## 3. Non-functional requirements

| ID | Requirement | Target |
|---|---|---|
| NFR-1 | Confirm endpoint latency | p95 < 200 ms |
| NFR-2 | Time from confirm to claim, idle worker | < 2 s |
| NFR-3 | 100-entry archive of ~50 KB documents | < 60 s end to end |
| NFR-4 | Worker memory while extracting a 1 GB archive | < 512 MB — streaming, never whole-file in memory |
| NFR-5 | Duplicate candidates after any failure scenario | 0 |
| NFR-6 | Files left non-terminal after any scenario, once sweepers have run | 0 |
| NFR-7 | Unit test coverage of `engine/` and `workers/` | ≥ 80 %, matching the house `--cov-fail-under` |

---

## 4. Assumptions and open questions

| # | Item | POC position | Confirm with |
|---|---|---|---|
| A1 | Worker reaches state via the API, not the database directly. Tasneem said API on 18 Sep and database on 24 Sep | **Internal API** — matches the LLD and HLD principle of one access path | Tasneem |
| A2 | Valid files go to `staging/` (LLD) or `processed/` (Tasneem's description) | **`staging/`** — metadata confirmation happens before the library | Tasneem |
| A3 | Allowed top-level extensions | `docx`, `zip` — per the Product Brief. OD-11 is still open | Product |
| A4 | Allowed archive entry extensions | `docx` | Product |
| A6 | A bad `.docx` **inside** an archive — unsafe or unreadable | **Entry-level rejection**; the archive continues and ends `partial`. Only failures of the outer archive itself reject the whole archive | Decided, rev 1.2 |
| A7 | Host tooling | Docker with the Compose plugin is required. Host Python 3.12 is acceptable for scripts; the image is 3.11 and unit tests run inside it. S3 checks use `awslocal` inside the LocalStack container, not a host AWS CLI | Decided, rev 1.2 |
| A5 | POC timing values are shortened so scenarios run in minutes, not hours | See `design.md` §9 | — |

---

## 5. Definition of done

The POC is complete when **all nine scenarios in `design.md` §10 — S1–S8 and S5b — pass from a clean `docker compose up`**, each by running one script, with the assertions in that section holding. Scenario S5 — kill a worker mid-archive, restart, and end with exactly M candidates — is the one to demonstrate.

---

## 6. Revision history

| Rev | Change | Affects |
|---|---|---|
| 1.1 | A transient failure on the final attempt now sets `error` instead of releasing; the reconcile sweeper also errors exhausted `uploaded` files. Previously such a file never reached a terminal state | R11.3, R11.5 · design §5, §8.1, §8.7 |
| 1.1 | A lying archive index surfaces as a CRC failure in CPython, not an oversize read. `extract_streaming` converts `BadZipFile` to `Rejected` | R6.7, R6.8 · design §8.5 |
| 1.1 | Rejecting an archive mid-way now rejects its already-staged candidates and deletes their objects | R6.8 · design §8.5a · task 9.3 |
| 1.1 | New scenario S5b kills a single child process, which is the only way to exercise `reject_on_worker_lost` | R8.4, R9.2 · design §10.2 · task 11.2 |
| 1.1 | S8 restarts Redis empty rather than running `FLUSHALL` on a live broker, and asserts no message reached `ae.undeliver` | R11.6, R11.7 · design §10.2 |
| 1.2 | `release` resets `uploaded_at`, so the reconcile sweeper cannot pre-empt the retry backoff | design §5, §6.2 |
| 1.2 | One `Transient` class raised by the S3 helper and internal client; the worker retries `(Transient, SoftTimeLimitExceeded)`. Task 10.2 aligned with §8.1 | design §8.1 · task 10.1, 10.2 |
| 1.2 | `detect_file_type` returns a `Detection` and never raises for bad content. A bad inner `.docx` is an **entry** rejection; only outer-archive failures reject the archive. The outer file of an archive upload is now itself verified | A6 · design §8.2–§8.5a · task 5.2, 9.1, 9.3 |
| 1.2 | Archive rejection deletes the staging prefix **before** `finish`, and overwrites only `processed` candidates. `staging/` gets a 7-day lifecycle backstop | design §8.5a · task 1.3, 9.3 |
| 1.2 | S5 and S5b recovery described separately. S5b kills via `sh -c 'kill -9 …'` | design §8.7, §10.2 |
| 1.2 | S8 split into phase A (Redis down at confirm) and phase B (queued messages lost). `ae.undeliver` checked after **every** scenario. Named compose project and Redis volume | R11.7 · design §10.2 · task 1.4, 13.1 |
| 1.2 | The API uses its own producer-only Celery instance with bounded connect timeouts and no publish retry; R2.4 bounds a Redis-down confirm to 2 s | R2.4 · design §6.2a · task 4.4 |
| 1.2 | `engine/` takes a `Limits` value instead of reading settings. New settings `POC_ORGANIZATION_ID`, `INTERNAL_API_BASE_URL`, `ALLOWED_TOP_LEVEL_EXT`; `/poc/seed` enforces the latter. `/poc/enqueue` route for S6; `enqueued_at` added to `scan_stub` | design §3, §6.1, §6.3, §9 · task 4.3 |
| 1.2 | Coverage gate moves to task 13.2. Git commit after each task. Build at the working-folder root. R12.1 says "at most". `entry_index` defined as position among file entries | tasks convention, 0.1, 13.2 · R12.1 · design §4 |
| 1.3 | A zip can hold two entries with the same name; they would collide on `uq_entry`, so the second would overwrite the first's candidate and orphan its staging object. Such an archive is now rejected whole. The stale sweeper's reset sets `uploaded_at = now()`, like `release`, so the next reconcile sweep does not enqueue the file twice. Both sweeps enqueue per file after commit, log failures as `enqueue_failed` and count them in their results; the sweepers' `error` updates set `updated_at` | R6.9 · task 5.3 · design §6.2, §8.7, §10.1 |
