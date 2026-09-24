# Findings — running notes for task 14.3

Items noticed during the build, to fold into the findings note back to Tasneem.

- **`status_message` carries raw exception text to users.** `finish` stores messages such as
  `Could not be processed after 3 attempts: {e}` (design.md §8.1), and the batch status endpoint
  shows them. The real build should store a friendly, user-facing message and log the raw error
  (with `file_id`, `attempt`) instead. The POC only truncates to 512 characters, ending with "…".
- **Fence tests need a status-only case.** In the task 3.3 one-off run with the status check removed from
  `heartbeat`, only the status-only test caught it; the after-finish and after-release tests cannot,
  because those states also clear the token.
