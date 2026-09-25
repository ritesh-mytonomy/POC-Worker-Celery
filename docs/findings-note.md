# Ingest Worker POC — findings

*For Tasneem · 25 Sep 2026 · repo tag `poc-complete` · full detail in `specs/ingest-worker-poc/` and the appendix*

## What the POC proved

**Your 18 Sep question is answered: a worker killed mid-archive resumes where it stopped.** Killed at document 10
of 30, the file was reset by the stale sweeper 30–45 s later, and attempt 2 carried on **from document 10** — ending
with exactly 30 candidates, 30 staged files, no duplicates, no orphans (S5). Killing only the worker process gets the
message requeued in **0.3–0.6 s** while the container stays up (S5b). All 13 scenario and check scripts pass from a
clean `docker compose up` (`./scripts/run_all.sh`, ~16 min); 560 tests, 96.8 % coverage of `engine/` and `workers/`.

| Guarantee | Measured |
|---|---|
| Exactly one owner per file | 10 threads racing × 20 rounds → 1 winner every time; 6 deliveries of one file → 1 claim (S6) |
| No duplicates after a crash, at any point | 16 crash points (after upload / after upsert / after progress) → exact end state each time |
| Retries bounded | S3 out 15 s → processed on attempt 2; S3 out for good → `error` after exactly 3 claims, never re-enqueued |
| Redis loss survived | Redis down at confirm → 200 in 0.09–0.31 s (limit 2 s); Redis wiped with messages queued → all recovered by reconcile, sweeps back 6–10 s after Redis returned, 0 messages dead-lettered (S8) |
| Unsafe archives refused | zip bomb, path traversal, encrypted, duplicate names, lying index — rejected whole; a bad document inside a good archive rejects only that entry (`partial`) |
| Queues isolated | scan task starts in 0.005–0.008 s while both ingest slots are busy (limit 2 s) |
| Confirm latency p95 (NFR-1) | 9–15 ms over 50 calls (limit 200 ms) |
| Confirm → claim, idle worker (NFR-2) | 22–239 ms (limit 2 s; the high one is a cold start) |
| 100 × 50 KB archive end to end (NFR-3) | 2.8–3.3 s (limit 60 s) |
| Memory, 200 MB entry | 0.36 MB peak — streamed in 64 KB chunks |

**POC timings are shortened; the constraints are the production ones.** Heartbeat 5 s (prod 30 s), stale after 30 s
(600 s), sweeps every 15 s (300 s), visibility timeout 300 s (10 800 s). So in production a killed worker's file is
picked up again **10–15 minutes** after the kill, not 30–45 s.

## Open questions from requirements §4 — answered

- **A1 — how workers reach state: the Internal API, not the database.** Workers have no database connection at
  all; every read and write goes through `/internal/*` with a shared key, and every write after the claim carries
  the claim token, so a worker that lost ownership cannot change anything (tested for every write). This kept one
  access path and gave one place to enforce the rules that mattered most — atomic claim, fenced writes, and the
  `partial` decision below.
- **A2 — where valid files go: `staging/`.** Keys are deterministic (`staging/{org}/{batch}/{file}/{index}_{name}`),
  so a replayed entry overwrites its own object instead of leaving an orphan; a 7-day lifecycle rule is the
  backstop. Metadata confirmation happens before anything reaches the library.

## Still open

- **LocalStack licence.** The pinned image is LocalStack's Pro image and needs an auth token in `.env`. Is that
  token and licence acceptable, or should the POC move to a free S3 emulator (e.g. moto)? Only the dev environment
  is affected — the code uses the standard boto3 client.
- **File types (OD-11).** The POC accepts `docx` and `zip` top-level and `docx` inside archives (A3, A4), per the
  Product Brief. The final list is still Product's decision.

## For the real build — the lessons that matter

1. **Decide outcomes from persisted state, never from a worker's memory.** A resumed run forgot the previous run's
   rejections and would have reported `processed`; the API now decides `partial` from the candidate rows.
2. **Test the failure path you ship.** Our own pool-reset fix left every later publish failing after one Redis
   outage; only the live check caught it. S3's default timeouts hid a 15 s outage entirely (30 s to fail).
3. **Re-enqueue by evidence, not a timer.** Reconcile can't tell a lost message from a slow queue: dedupe at publish
   or use measured queue lag, and alert when it actually requeues.
4. **Remove the POC shortcuts:** `/poc/*` routes, unauthenticated confirm (make it org-scoped), dummy AWS
   credentials and `AWS_ENDPOINT_URL`, raw exception text in user-facing messages; lock all dependencies.
5. **Show an archive that stopped part-way** on the staging screen, so nobody commits an incomplete set.

---

## Appendix — full findings list (`docs/findings.md`)

- **`status_message` carries raw exception text to users.** `finish` stores messages such as
  `Could not be processed after 3 attempts: {e}` (design.md §8.1), and the batch status endpoint
  shows them. The real build should store a friendly, user-facing message and log the raw error
  (with `file_id`, `attempt`) instead. The POC only truncates to 512 characters, ending with "…".
- **Fence tests need a status-only case.** In the task 3.3 one-off run with the status check removed from
  `heartbeat`, only the status-only test caught it; the after-finish and after-release tests cannot,
  because those states also clear the token.
- **The 3.2 race test's pid proof measured closed connections.** With `NullPool`, `session.commit()` closed
  each thread's connection, so the recorded `pg_backend_pid()` was not the connection that ran `claim()`.
  Found and fixed in 3.4 (each thread now holds one connection for its life and asserts the pid is
  unchanged after the claim). The exactly-one-winner result was unaffected: NullPool gave every thread its
  own connection regardless.
- **`/poc/*` routes must not exist in the real build.** `/poc/seed` stands in for the presign flow and
  `/poc/enqueue` publishes duplicate messages on demand; both are unauthenticated test aids.
- **Lock dependencies fully in the real build, transitive ones included.** The POC pins only the direct
  dependencies in `requirements.txt`; use a lock file (for example `pip-compile` or `uv lock`) with hashes.
- **Confirm must be authenticated and organization-scoped in the real build** (LLD Module 1). The POC's
  `POST /api/v1/uploads/{file_id}/confirm` is unauthenticated and accepts any `file_id`.
- **Kombu's `force_close_all()` closes the shared per-broker pool permanently.** Kombu keeps one connection pool
  per broker in a process-wide registry, so a new producer for the same broker gets the closed pool back and every
  publish fails with `Acquire on closed pool`. After a failed publish, call `kombu.pools.reset()` and rebuild the
  producer (found in 4.4's live check; regression test `test_publish_works_again_after_a_failed_publish`).
- **The dummy AWS credentials must never reach production.** Compose defaults `AWS_ACCESS_KEY_ID` /
  `AWS_SECRET_ACCESS_KEY` to `test` so boto3 can sign requests to LocalStack; production must use an IAM role.
- **`AWS_ENDPOINT_URL` must be absent in production.** boto3 (1.28+) reads that environment variable by itself,
  so even with the setting unset it would redirect every S3 call if the variable leaked into the environment.
- **Outcome decisions after a resume must come from persisted state, never from counters in the worker's memory.**
  Found by the 9.1 crash-window tests: a resumed archive run lost the rejections of the run before the crash and
  would have reported `processed`; the API now decides `partial` from the candidate rows at finish (R7.6, rev 1.3).
- **Show an archive that ended in `error` part-way on the staging screen.** `finish(error)` (attempts exhausted)
  leaves the entries already staged as `processed`, which is right — they are valid documents — but the real
  build's staging screen must make clear the archive stopped part-way, so a user does not commit an incomplete set
  believing it is the whole archive.
- **Set explicit S3 timeouts in the real build.** boto3's defaults (60 s connect/read, retries) — and even our first
  settings (10 s read, 3 attempts) — made a hung S3 take ~30 s to fail, long enough to hide a short outage from the
  retry logic. The POC uses 2 s connect, 3 s read, 2 attempts: a hung S3 fails in ~7 s, an unreachable one in ~1 s.
- **The reconcile sweeper cannot tell a lost message from one waiting in a long queue.** Any file `uploaded` for
  longer than `RECONCILE_AFTER_SECONDS` is re-enqueued (once per that period, since a requeue resets `uploaded_at`).
  In the POC this is accepted: a duplicate loses its claim and is acked, and `attempt_count` only counts successful
  claims. The real build should re-enqueue by evidence, not a fixed timer: dedupe at publish (e.g. SQS FIFO
  `MessageDeduplicationId = file_id`, or a `SETNX enqueued:{file_id}` marker with a TTL, cleared on claim), or base
  the threshold on measured queue lag (oldest-message age plus a margin) — and alert when reconcile actually
  requeues, since that should mean a lost message.
