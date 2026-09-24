# Findings — running notes for task 14.3

Items noticed during the build, to fold into the findings note back to Tasneem.

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
