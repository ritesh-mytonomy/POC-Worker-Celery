# Ingest Worker POC

Build strictly from the spec in `specs/ingest-worker-poc/`:
- `requirements.md` — what must be true (EARS acceptance criteria)
- `design.md` — how to build it (schema, SQL, algorithms, config)
- `tasks.md` — the ordered build steps

## Rules
- Work one task at a time, in the order in `tasks.md`.
- A task is done only when its **Done when** holds. Run the check and show me the output.
- Tick the task's checkbox in `tasks.md` when done. Then stop and wait for me.
- Follow `design.md` exactly for SQL, Celery settings, and algorithms. If something
  in the spec looks wrong or ambiguous, stop and ask — do not improvise.
- Workers never open a database connection. They call `/internal/*` on the API.
- Every worker write after claim carries the claim token.
- Python 3.11. Type hints on every function. `str | None`, not `Optional[str]`.
- One-line docstring on every public function.

## Commands
- Start everything: `docker compose up -d --wait`
- Unit tests: `pytest tests/`
- All scenarios: `./scripts/run_all.sh`