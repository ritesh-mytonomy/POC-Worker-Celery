"""Task 10.2 live check (design.md §8.1; R11.1, R11.3), against the running stack.

LocalStack's outage is simulated by disconnecting its container from the compose network: `docker compose stop`
would wipe its in-memory state (the incoming object with it). A throwaway ingest worker runs with
ENTRY_DELAY_SECONDS=1 so an outage can land mid-archive; the normal worker-ingest is restored at the end.

A. 15-second outage mid-task: big30.zip ends processed with attempt_count <= 3 (and >= 2: the outage was felt).
B. Permanent outage: valid.docx ends error after exactly MAX_ATTEMPTS claims and is never enqueued again.

Usage: python3 scripts/check_retry.py [--keep]
"""
import json
import subprocess
import time
import uuid

import lib

NETWORK, LOCALSTACK, WORKER = "clinsync-ingest-poc_default", "clinsync-ingest-poc-localstack-1", "retry-worker"
MAX_ATTEMPTS = 3


def sh(*args: str) -> str:
    """Run a command; return stdout."""
    return subprocess.run(args, capture_output=True, check=True, text=True).stdout


def disconnect() -> None:
    """Take LocalStack off the network (its name stops resolving; its state survives)."""
    sh("docker", "network", "disconnect", NETWORK, LOCALSTACK)


def connect() -> None:
    """Put LocalStack back, with its service alias."""
    attached = json.loads(sh("docker", "inspect", "-f", "{{json .NetworkSettings.Networks}}", LOCALSTACK))
    if NETWORK not in attached:
        sh("docker", "network", "connect", "--alias", "localstack", NETWORK, LOCALSTACK)


def worker_events(file_id: str) -> list[dict]:
    """The throwaway worker's JSON log lines about file_id."""
    out = subprocess.run(["docker", "logs", WORKER], capture_output=True, text=True).stdout
    events = []
    for raw in out.splitlines():
        try:
            line = json.loads(raw)
        except ValueError:
            continue
        if line.get("file_id") == file_id:
            events.append(line)
    return events


def queued_mentions(file_id: str) -> int:
    """Messages for file_id still in Redis: ready in clinsync.ingest, or reserved/delayed in Kombu's unacked hash."""
    ready = lib.compose("exec", "-T", "redis", "redis-cli", "LRANGE", "clinsync.ingest", "0", "-1")
    unacked = lib.compose("exec", "-T", "redis", "redis-cli", "HVALS", "unacked")
    return ready.count(file_id) + unacked.count(file_id)


def wait_for(condition, timeout: float, what: str) -> None:  # type: ignore[no-untyped-def]
    """Poll until condition() is true."""
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise TimeoutError(what)
        time.sleep(0.5)


def case_a(s: lib.Scenario, run_id: str) -> tuple[str, list[str]]:
    """15 s outage in the middle of big30.zip."""
    print("  -- A. LocalStack unreachable for 15 s mid-task (big30.zip)")
    key = lib.incoming_key(run_id, "big30.zip")
    lib.upload("big30.zip", key)
    batch_id, (file_id,) = lib.seed([("big30.zip", key)])
    lib.confirm(file_id)
    wait_for(lambda: (lib.batch(batch_id)[0]["entries_done"] or 0) >= 3, 60, "entries_done >= 3")
    done_at_outage = lib.batch(batch_id)[0]["entries_done"]
    disconnect()
    time.sleep(15)
    connect()
    print(f"         outage from entries_done={done_at_outage}, 15 s")
    (final,) = lib.wait_terminal(batch_id, timeout=180)
    events = worker_events(file_id)
    s.check("status", final["status"], "processed")
    s.check("attempt_count <= 3 and >= 2", 2 <= final["attempt_count"] <= MAX_ATTEMPTS, True)
    print(f"         attempt_count = {final['attempt_count']}")
    s.check("claim_ok lines == attempt_count", sum(e["event"] == "claim_ok" for e in events), final["attempt_count"])
    retries = [e for e in events if e["event"] == "retrying"]
    s.check("retrying lines == attempt_count - 1", len(retries), final["attempt_count"] - 1)
    s.check("backoff countdowns", [e["countdown"] for e in retries], [10, 20][: len(retries)])
    s.check("entries_total, entries_done", (final["entries_total"], final["entries_done"]), (30, 30))
    candidates = lib.staged(batch_id)
    s.check("30 processed candidates", (len(candidates), {c["status"] for c in candidates}), (30, {"processed"}))
    s.check("30 staged objects", len(lib.staging_keys_for(file_id)), 30)
    return batch_id, [key]


def case_b(s: lib.Scenario, run_id: str) -> tuple[str, list[str]]:
    """LocalStack unreachable for good (until the checks are done)."""
    print("  -- B. LocalStack unreachable permanently (valid.docx)")
    key = lib.incoming_key(run_id, "valid.docx")
    lib.upload("valid.docx", key)
    batch_id, (file_id,) = lib.seed([("valid.docx", key)])
    disconnect()
    try:
        lib.confirm(file_id)
        (final,) = lib.wait_terminal(batch_id, timeout=120)
        s.check("status", final["status"], "error")
        s.check("message", (final["status_message"] or "").startswith(f"Could not be processed after {MAX_ATTEMPTS} "
                                                                      "attempts:"), True)
        print(f"         message: {final['status_message']!r}")
        s.check("attempt_count", final["attempt_count"], MAX_ATTEMPTS)
        events = worker_events(file_id)
        s.check("claim_ok lines", sum(e["event"] == "claim_ok" for e in events), MAX_ATTEMPTS)
        s.check("retrying countdowns", [e["countdown"] for e in events if e["event"] == "retrying"], [10, 20])
        print("         waiting 60 s to prove it is never enqueued again ...")
        time.sleep(60)
        later = worker_events(file_id)
        s.check("no new claims after the error", sum(e["event"] in ("claim_ok", "claim_lost") for e in later),
                MAX_ATTEMPTS)
        s.check("no message for it left in Redis (ready or unacked)", queued_mentions(file_id), 0)
        s.check("attempt_count still", lib.batch(batch_id)[0]["attempt_count"], MAX_ATTEMPTS)
    finally:
        connect()
    return batch_id, [key]


def main() -> int:
    """Run both cases with a throwaway worker; always restore LocalStack and worker-ingest."""
    s = lib.Scenario("10.2 release and retry")
    run_id = f"r102-{uuid.uuid4().hex[:8]}"
    created: list[tuple[str, list[str]]] = []
    lib.compose("stop", "worker-ingest")
    subprocess.run(["docker", "rm", "-f", WORKER], capture_output=True)
    lib.compose("run", "-d", "--no-deps", "--name", WORKER, "-e", "ENTRY_DELAY_SECONDS=1", "worker-ingest")
    try:
        time.sleep(6)                                            # worker boot
        created.append(case_a(s, run_id))
        created.append(case_b(s, run_id))
        lib.assert_no_undeliverable(s)
    finally:
        connect()
        subprocess.run(["docker", "rm", "-f", WORKER], capture_output=True)
        lib.compose("up", "-d", "--wait", "worker-ingest")
        for batch_id, keys in created:
            lib.cleanup(s, batch_id, keys)
    return s.finish()


if __name__ == "__main__":
    lib.run(main)
