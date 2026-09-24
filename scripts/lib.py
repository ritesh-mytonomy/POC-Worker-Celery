"""Scenario helpers (design.md §10): drive the running stack from the host. Standard library + docker compose only."""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
API = os.environ.get("SCENARIO_API", "http://127.0.0.1:8000")
BUCKET = "clinsync-poc"
FIXTURES = ROOT / "fixtures" / "out"
TERMINAL = {"processed", "partial", "rejected", "error"}


class Scenario:
    """Collects PASS / FAIL / PENDING results and prints them; exit code is non-zero on any FAIL."""

    def __init__(self, name: str) -> None:
        """Start a scenario run. `--keep` on the command line skips cleanup, for debugging."""
        self.name, self.failed, self.pending = name, 0, 0
        self.keep = "--keep" in sys.argv
        self.started = time.monotonic()
        print(f"=== {name}" + ("  (--keep: batch and S3 objects left in place)" if self.keep else ""))

    def check(self, label: str, actual: Any, expected: Any) -> bool:
        """Record actual == expected."""
        ok = actual == expected
        self.failed += not ok
        print(f"  {'PASS' if ok else 'FAIL'}  {label}: {actual!r}" + ("" if ok else f" (expected {expected!r})"))
        return ok

    def pend(self, label: str, why: str) -> None:
        """Record a check that cannot hold yet, with the reason — never a silent pass."""
        self.pending += 1
        print(f"  PENDING  {label}: {why}")

    def finish(self) -> int:
        """Print the summary; return the process exit code."""
        verdict = "FAILED" if self.failed else "PASSED"
        extra = f", {self.pending} pending" if self.pending else ""
        print(f"=== {self.name} {verdict} ({self.failed} failed{extra}) in {time.monotonic() - self.started:.1f}s")
        return 1 if self.failed else 0


def compose(*args: str, stdin: bytes | None = None) -> str:
    """Run `docker compose ARGS` in the repo; return stdout."""
    result = subprocess.run(["docker", "compose", *args], cwd=ROOT, input=stdin, capture_output=True, check=True)
    return result.stdout.decode()


DELAY_OVERRIDE = ["-f", "docker-compose.yml", "-f", "scripts/compose.entry-delay.yml"]


def compose_delay(*args: str) -> str:
    """`docker compose` with the ENTRY_DELAY_SECONDS override for worker-ingest (S5, S5b)."""
    return compose(*DELAY_OVERRIDE, *args)


def restore_worker_ingest() -> None:
    """Put back the normal worker-ingest (no entry delay)."""
    compose("up", "-d", "--wait", "--force-recreate", "worker-ingest")


def worker_lines(service: str, since: str | None = None) -> list[dict[str, Any]]:
    """Every JSON log line of a service (optionally since an ISO time)."""
    args = ["logs", "--no-log-prefix", *(["--since", since] if since else []), service]
    lines = []
    for raw in compose(*args).splitlines():
        try:
            lines.append(json.loads(raw))
        except ValueError:
            continue
    return lines


def container_info(service: str) -> dict[str, str]:
    """The service container's id, StartedAt and state."""
    cid = compose("ps", "-a", "-q", service).strip()
    out = subprocess.run(["docker", "inspect", "-f", "{{.Id}}|{{.State.StartedAt}}|{{.State.Status}}", cid],
                         capture_output=True, text=True, check=True).stdout.strip()
    ident, started, status = out.split("|")
    return {"id": ident, "started_at": started, "status": status}


def redis_cli(*args: str) -> str:
    """Run redis-cli in the redis container."""
    return compose("exec", "-T", "redis", "redis-cli", *args).strip()


def utc_now() -> str:
    """Now, as an ISO-8601 UTC string (for `docker compose logs --since` and comparisons)."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def ts(line: dict[str, Any]) -> float:
    """A log line's timestamp as epoch seconds."""
    from datetime import datetime
    return datetime.fromisoformat(line["ts"]).timestamp()


def wait_until(predicate: Any, timeout: float, what: str, every: float = 0.5) -> None:
    """Poll predicate() until true (raises TimeoutError naming `what`)."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise TimeoutError(what)
        time.sleep(every)


def api(method: str, path: str, body: Any = None) -> tuple[int, Any]:
    """Call the API; return (status, parsed JSON or None)."""
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(API + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        return exc.code, json.loads(raw) if raw else None


def ensure_fixtures() -> Path:
    """Build fixtures/out/ with the image (python-docx lives there), owned by the current user."""
    if not (FIXTURES / "valid.docx").exists():
        FIXTURES.mkdir(parents=True, exist_ok=True)
        subprocess.run(["docker", "run", "--rm", "--network", "none", "--user", f"{os.getuid()}:{os.getgid()}",
                        "-v", f"{FIXTURES}:/out", "clinsync-ingest-poc:dev", "python", "fixtures/make_fixtures.py",
                        "/out"], check=True, capture_output=True)
    return FIXTURES


def upload(fixture: str, key: str) -> None:
    """Put a fixture into LocalStack S3 at key (via awslocal inside the container, as A7 says)."""
    data = (ensure_fixtures() / fixture).read_bytes()
    compose("exec", "-T", "localstack", "awslocal", "s3", "cp", "-", f"s3://{BUCKET}/{key}", stdin=data)


def s3_keys(prefix: str) -> list[str]:
    """Keys under prefix in the POC bucket."""
    out = compose("exec", "-T", "localstack", "awslocal", "s3api", "list-objects-v2", "--bucket", BUCKET,
                  "--prefix", prefix, "--query", "Contents[].Key", "--output", "json")
    return json.loads(out) or []


def staging_keys_for(file_id: str) -> list[str]:
    """Staged objects of one file (…/staging/{org}/{batch}/{file_id}/…)."""
    return [k for k in s3_keys("ClinSync/staging/") if f"/{file_id}/" in k]


def staging_keys_for_batch(batch_id: str) -> list[str]:
    """Staged objects of every file in a batch."""
    return [k for k in s3_keys("ClinSync/staging/") if f"/{batch_id}/" in k]


def incoming_key(run_id: str, fixture: str) -> str:
    """A fresh incoming/ key for one scenario run."""
    return f"ClinSync/incoming/{run_id}/{fixture}"


def seed(files: list[tuple[str, str]]) -> tuple[str, list[str]]:
    """POST /poc/seed for (file_name, s3_key) pairs; return (batch_id, file_ids)."""
    status, body = api("POST", "/poc/seed", {"files": [{"file_name": n, "s3_key": k} for n, k in files]})
    if status != 201:
        raise RuntimeError(f"seed failed: {status} {body}")
    return body["batch_id"], [f["file_id"] for f in body["files"]]


def confirm(file_id: str) -> dict[str, Any]:
    """POST confirm; return the body."""
    status, body = api("POST", f"/api/v1/uploads/{file_id}/confirm")
    if status != 200:
        raise RuntimeError(f"confirm failed: {status} {body}")
    return body


def batch(batch_id: str) -> list[dict[str, Any]]:
    """GET the batch's files."""
    return api("GET", f"/api/v1/uploads/batches/{batch_id}")[1]["files"]


def staged(batch_id: str) -> list[dict[str, Any]]:
    """GET the batch's candidates."""
    return api("GET", f"/api/v1/uploads/batches/{batch_id}/staged")[1]["staged"]


def wait_terminal(batch_id: str, timeout: float = 60) -> list[dict[str, Any]]:
    """Poll until every file in the batch is terminal; return the files (raises on timeout)."""
    deadline = time.monotonic() + timeout
    while True:
        files = batch(batch_id)
        if all(f["status"] in TERMINAL for f in files):
            return files
        if time.monotonic() > deadline:
            raise TimeoutError(f"batch {batch_id} not terminal after {timeout}s: {[f['status'] for f in files]}")
        time.sleep(0.25)


def events(service: str, file_id: str, since: str | None = None) -> list[dict[str, Any]]:
    """The service's JSON log lines about file_id."""
    args = ["logs", "--no-log-prefix", *(["--since", since] if since else []), service]
    lines = []
    for raw in compose(*args).splitlines():
        try:
            line = json.loads(raw)
        except ValueError:
            continue
        if line.get("file_id") == file_id:
            lines.append(line)
    return lines


def wait_events(service: str, file_id: str, names: set[str], count: int, timeout: float = 30) -> list[dict[str, Any]]:
    """Poll the service's logs until `count` lines with an event in `names` exist for file_id."""
    deadline = time.monotonic() + timeout
    while True:
        found = [e for e in events(service, file_id) if e.get("event") in names]
        if len(found) >= count or time.monotonic() > deadline:
            return found
        time.sleep(0.5)


def redis_llen(key: str) -> int:
    """LLEN of a Redis list."""
    return int(compose("exec", "-T", "redis", "redis-cli", "LLEN", key).strip())


def queued_mentions(file_id: str) -> int:
    """Messages for file_id still in Redis: ready in clinsync.ingest, or reserved/delayed in Kombu's unacked hash."""
    return redis_cli("LRANGE", "clinsync.ingest", "0", "-1").count(file_id) + redis_cli("HVALS", "unacked").count(file_id)


def assert_no_undeliverable(scenario: Scenario) -> None:
    """Every scenario ends here: nothing was silently dropped to Kombu's dead-letter list (R11.7)."""
    scenario.check("LLEN ae.undeliver", redis_llen("ae.undeliver"), 0)


def cleanup(scenario: Scenario, batch_id: str, keys: list[str]) -> None:
    """Remove the scenario's batch (cascades to files and candidates), its incoming keys and staged objects."""
    if scenario.keep:
        print(f"  kept: batch {batch_id}, keys {keys}")
        return
    user, db = os.environ.get("POSTGRES_USER", "clinsync"), os.environ.get("POSTGRES_DB", "clinsync")
    compose("exec", "-T", "postgres", "psql", "-U", user, "-d", db, "-qtAc",
            f"DELETE FROM upload_batch WHERE batch_id = '{uuid.UUID(batch_id)}'")
    for key in [*keys, *staging_keys_for_batch(batch_id)]:
        compose("exec", "-T", "localstack", "awslocal", "s3", "rm", f"s3://{BUCKET}/{key}")


def run(main: Any) -> None:
    """Run a scenario's main() and exit with its code."""
    sys.exit(main())
