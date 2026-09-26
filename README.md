# ClinSync Ingest Worker POC (Celery + Redis)

A proof of concept for the background half of the ClinSync upload pipeline: from the moment a file sits in
`incoming/` and the API has been told, to the moment its contents are staged or rejected. It shows that a worker
killed mid-archive resumes where it stopped — no document redone, none duplicated, none lost.

The specification lives in [`specs/ingest-worker-poc/`](specs/ingest-worker-poc/) (`requirements.md`, `design.md`,
`tasks.md`). Results and lessons for the real build: [`docs/findings-note.md`](docs/findings-note.md). The live demo:
[`docs/demo-runbook.md`](docs/demo-runbook.md).

## Prerequisites

- **Docker with the Compose plugin** (`docker compose version` should print v2 or later). Your user must be able to
  run `docker` without `sudo` (be in the `docker` group).
- **Python 3.10+ on the host**, standard library only — for the scenario scripts. Nothing to `pip install` on the
  host; everything else runs inside the image (Python 3.11).
- **A LocalStack auth token.** The `localstack/localstack:2026.8.4` image is LocalStack's Pro image and refuses to
  start without `LOCALSTACK_AUTH_TOKEN`. *Whether that token and licence are acceptable for the team, or whether the
  POC should switch to a free S3 emulator such as moto, is still an open question with Tasneem.*
- About 4 GB of free RAM and ~3 GB of disk for images.

## Setup

```bash
git clone <this repo> && cd <repo>
cp .env.example .env
# edit .env and set LOCALSTACK_AUTH_TOKEN=<your token>   (never commit .env — it is git-ignored)
docker compose up -d --build --wait
```

`--wait` returns once every service with a healthcheck is healthy. `.env.example` holds the POC values for every
setting (`design.md` §9); the defaults work as they are. `DATABASE_URL` is built from the `POSTGRES_*` values unless
you set it explicitly. The API and LocalStack are published on `127.0.0.1` only (ports 8000 and 4566).

Stop with `docker compose down`; `docker compose down -v` also wipes the database, Redis and LocalStack volumes.

## Run everything

```bash
./scripts/run_all.sh
```

From a clean stack (`down -v`, `up --build --wait`, fixtures regenerated) it runs S1–S8, S5b, the three check scripts
and the NFR timing checks — 13 entries, about 16 minutes — and prints a pass/fail table. Each script's output goes
to `run_logs/<name>.log`. It blocks system sleep while it runs (`systemd-inhibit`): a suspended laptop stalls
every scenario.

Every scenario asserts, before it exits, that Kombu's dead-letter list `ae.undeliver` is empty, that no candidate
is duplicated (NFR-5) and that no file of its batch is left non-terminal (NFR-6).

| Script | What it proves |
|---|---|
| `scenario_s1.py` | Mixed archive → `partial`: 3 staged, 2 rejected with reasons |
| `scenario_s2.py` / `scenario_s3.py` | A valid `.docx` is staged; renamed files are rejected by content |
| `scenario_s4.py` | Never more than `INGEST_CONCURRENCY` files processing at once |
| `scenario_s5.py` | **The demo:** kill the worker container mid-archive; it resumes at the kill point |
| `scenario_s5b.py` | Kill one child process; the message is requeued at once (`reject_on_worker_lost`) |
| `scenario_s6.py` | Six deliveries of one file → exactly one claim |
| `scenario_s7.py` | The scan queue starts work in < 2 s while ingest is saturated |
| `scenario_s8.py` | Redis down at confirm, and Redis lost with messages in it — everything recovers |
| `check_confirm_redis_down.sh` | Confirm answers in < 2 s with Redis stopped |
| `check_archive_rejection.py` | An archive whose index lies is rejected whole; an inner zip bomb rejects one entry |
| `check_retry.py` | S3 out for 15 s → retried and processed; S3 out for good → `error` after 3 attempts |
| `check_nfr.py` | Confirm latency, confirm-to-claim, 100-entry archive timings (measured values printed) |

Any script runs on its own against a running stack, e.g. `python3 scripts/scenario_s5.py`. Options:

- **`--keep`** — skip the cleanup at the end, leaving the batch rows and S3 objects in place for inspection.
- `scenario_s5.py --wait-visibility` — also wait (~5 min) for the killed task's original message to come back after
  `VISIBILITY_TIMEOUT` and show it simply loses its claim.

## Tests

```bash
docker compose run --rm --no-deps api pytest tests/
```

About 700 tests. The run needs the compose Postgres and Redis for the integration tests (they skip if unreachable;
set `REQUIRE_DB=1` to make them fail instead). Tests use their own database, `clinsync_test`, which Postgres creates
from the same `init.sql` when its volume is first initialised (`infra/postgres/test_db.sh`); the running stack uses
only `clinsync`, so its sweepers never touch test rows and the suite can run with the whole stack up. A volume
created before this existed needs `docker compose down -v` once. A coverage gate for `engine/` and `workers/` (`--cov-fail-under=80`,
currently ~97 %) is part of `pytest.ini`, so **partial runs add `--no-cov`**:

```bash
# the engine alone, nothing running, no network:
docker run --rm --network none clinsync-ingest-poc:dev pytest tests/test_engine*.py --no-cov
```

## The seven services

| Service | Role |
|---|---|
| `api` | FastAPI on `:8000`. The only component that touches PostgreSQL. Public routes (confirm, batch status, staged candidates), the `/internal/*` routes workers call (claim, heartbeat, progress, candidates, finish, release, sweeps — all need `X-Internal-Key`, writes also `X-Claim-Token`), and POC-only `/poc/*` helpers (seed, enqueue, scan-stub), mounted by the POC entry point `poc.main:app`, which wraps the production `app.main:app`. It publishes `process_upload` to Redis on confirm. |
| `worker-ingest` | Celery worker on queue `clinsync.ingest`, `INGEST_CONCURRENCY` (2) processes. Runs `process_upload`: claim, download from `incoming/`, detect the real file type, check archive guards, stage valid documents to `staging/`, record candidates, finish, delete from `incoming/`. Resumes archives from `entries_done`. Never opens a database connection. |
| `worker-scan` | Celery worker on queue `clinsync.scan` (1 process), running a stub scan task. Exists to prove queue isolation: a saturated ingest pool cannot delay it. Starts through `poc.worker` (the production Celery app plus the stub). |
| `worker-maint` | Celery worker on `clinsync.maintenance` with **beat embedded** (`-B`). Every 15 s beat publishes the stale sweep (reset files whose heartbeat went silent) and the reconcile sweep (re-enqueue confirmed files nobody claimed — lost messages). The API does the sweeping; this worker only calls it. |
| `redis` | The broker. Append-only file on (like ElastiCache), data on the named volume `redis-data`. Holds messages only — all state is in PostgreSQL. |
| `postgres` | PostgreSQL 15 with the LLD's tables: `organizations` (one POC row), `upload_batch`, `upload_file` (the state machine, claim token, heartbeat, progress), `staged_document` (candidates), `documents` (the library), `audit_log`. Schema in `infra/postgres/init.sql`, a copy of `specs/upload-ingest-merge/schema.sql`. |
| `localstack` | S3 emulator on `:4566`, bucket `clinsync-poc` with lifecycle rules on `ClinSync/incoming/` (1 day) and `ClinSync/staging/` (7 days). |

## Reading the logs

Every line our code emits is one JSON object with `ts`, `level`, `event`, `logger` and `pid`, plus context such as
`file_id`, `batch_id`, `task_id`, `attempt` and `claim_token`. Celery's own lifecycle notices (for example
`worker: Warm shutdown`) may be plain text, so the examples use `jq -R 'fromjson? | …'` to skip them:

```bash
docker compose logs -f --no-log-prefix worker-ingest | jq -R -c 'fromjson? | {ts, event, file_id, attempt, entry_index}'
docker compose logs -f --no-log-prefix worker-maint  | jq -R -c 'fromjson? | select(.event|test("sweep")) | {ts, event, reset, requeued, errored}'
docker compose logs --no-log-prefix api | jq -R -c 'fromjson? | select(.file_id == "<file-id>")'
```

Events worth knowing:

| Event | Where | Meaning |
|---|---|---|
| `claim_ok` / `claim_lost` | worker-ingest | Took ownership (with `attempt`, `claim_token`) / someone else owns it or it's finished — message acked, nothing downloaded |
| `entry_staged` / `entry_rejected` | worker-ingest | One archive entry handled, with `entry_index` |
| `retrying` / `finished` | worker-ingest | Transient failure: released, retry in `countdown` s / terminal status set |
| `claim_superseded` / `task_failed` | worker-ingest | Ownership lost mid-task / a non-retryable error (logged with the traceback) |
| `stale_sweep` / `reconcile_sweep` | worker-maint, api | Sweep counts (`reset`, `requeued`, `errored`, `enqueue_failed`); the API's line lists the `file_ids` |
| `enqueue_failed` | api | Redis unreachable at publish — the file stays `uploaded`; reconcile recovers it |
| `released` | api | A worker handed a file back before a retry, with `attempt` and `reason` |

Batch progress is also visible without logs: `curl -s localhost:8000/api/v1/uploads/batches/<batch-id> | jq` and
`…/staged`.

## Layout

```
app/        FastAPI app: routes, services, repositories (SQL), settings, JSON logging, producer-only Celery client
workers/    tasks.py: the Celery app, the heartbeat, process_upload, the sweeper tasks
            clients.py: the Internal API client and the S3 store, each with its error mapping (and Transient)
engine/     file_checks.py: pure functions — limits, file-type detection, archive guards, streaming extraction
poc/        POC-only: main.py (the api entry point: app.main:app + /poc/*), worker.py (worker-scan's entry point:
            workers.tasks + the scan stub). Depends on app/ and workers/; nothing depends on it, so it can be deleted.
infra/      postgres/init.sql, localstack/init-s3.sh
fixtures/   make_fixtures.py builds every test file into fixtures/out/
scripts/    scenario scripts, check scripts, run_all.sh
tests/      unit and integration tests
docs/       findings note, demo runbook, running findings list
```
