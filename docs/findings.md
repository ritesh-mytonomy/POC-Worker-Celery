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
