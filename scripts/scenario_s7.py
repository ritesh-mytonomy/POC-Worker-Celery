"""S7 · Queue isolation (design.md §10.2; R13).

Saturate ingest (ENTRY_DELAY_SECONDS=2, three big30.zip on INGEST_CONCURRENCY=2 slots). While both slots are busy,
POST /poc/scan-stub {"seconds": 1}. Assert: the scan task starts within 2 s of enqueue.

Usage: python3 scripts/scenario_s7.py [--keep]
"""
import time
import uuid

import lib


def main() -> int:
    """Run S7; return the exit code."""
    s = lib.Scenario("S7 queue isolation")
    run_id = f"s7-{uuid.uuid4().hex[:8]}"
    names = [f"big30_{c}.zip" for c in "abc"]
    keys = {n: lib.incoming_key(run_id, n) for n in names}
    batch_id = ""
    try:
        lib.compose_delay("up", "-d", "--wait", "--force-recreate", "worker-ingest")
        time.sleep(3)
        for key in keys.values():
            lib.upload("big30.zip", key)
        batch_id, _ = lib.seed(list(keys.items()))
        for f in lib.batch(batch_id):
            lib.confirm(f["file_id"])

        def processing() -> int:
            return sum(f["status"] == "processing" for f in lib.batch(batch_id))

        lib.wait_until(lambda: processing() == 2, 30, "both ingest slots busy")
        s.check("both ingest slots busy before the scan", processing(), 2)
        status, body = lib.api("POST", "/poc/scan-stub", {"seconds": 1})
        s.check("scan stub accepted", status, 202)
        busy_after = processing()
        task_id = body["task_id"]
        lib.wait_until(lambda: any(l.get("event") == "scan_started" and l.get("task_id") == task_id
                                   for l in lib.worker_lines("worker-scan")), 15, "scan_started")
        started = next(l for l in lib.worker_lines("worker-scan")
                       if l.get("event") == "scan_started" and l.get("task_id") == task_id)
        delay = started["start_delay_s"]
        print(f"         scan started {delay:.3f} s after enqueue (enqueued_at {body['enqueued_at']})")
        s.check("scan started within 2 s of enqueue", delay < 2.0, True)
        s.check("ingest still saturated when the scan was enqueued", busy_after, 2)
        lib.wait_until(lambda: any(l.get("event") == "scan_finished" and l.get("task_id") == task_id
                                   for l in lib.worker_lines("worker-scan")), 15, "scan_finished")
        s.check("scan finished", True, True)
        finals = lib.wait_terminal(batch_id, timeout=3 * 30 * 2 + 60)
        s.check("all three archives processed", [f["status"] for f in finals], ["processed"] * 3)
        s.check("attempt_count 1 each", [f["attempt_count"] for f in finals], [1, 1, 1])
        lib.assert_no_undeliverable(s)
    finally:
        lib.restore_worker_ingest()
        if batch_id:
            lib.cleanup(s, batch_id, list(keys.values()))
    return s.finish()


if __name__ == "__main__":
    lib.run(main)
