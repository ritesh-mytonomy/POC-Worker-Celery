"""S4 · Concurrency (design.md §10.2; R12).

INGEST_CONCURRENCY=2, ENTRY_DELAY_SECONDS=1: confirm three big30.zip together; poll every 500 ms. Assert: never more
than 2 files processing at once · the third stays uploaded until one finishes · all three processed · every file
attempt_count = 1 (the duplicate re-enqueues accepted in 11.1 must not change the outcome).

Usage: python3 scripts/scenario_s4.py [--keep]
"""
import os
import time
import uuid

import lib

CONCURRENCY = 2


def main() -> int:
    """Run S4; return the exit code."""
    s = lib.Scenario("S4 concurrency")
    run_id = f"s4-{uuid.uuid4().hex[:8]}"
    keys = {f"big30_{c}.zip": lib.incoming_key(run_id, f"big30_{c}.zip") for c in "abc"}
    batch_id = ""
    os.environ["SCENARIO_ENTRY_DELAY"] = "1"
    try:
        lib.compose_delay("up", "-d", "--wait", "--force-recreate", "worker-ingest")
        time.sleep(3)
        for key in keys.values():
            lib.upload("big30.zip", key)
        batch_id, _ = lib.seed(list(keys.items()))
        ids = [f["file_id"] for f in lib.batch(batch_id)]
        started_wall = time.time()                     # only to compare with container log timestamps
        started = time.monotonic()                     # all offsets and the timeout: immune to clock jumps
        for file_id in ids:
            lib.confirm(file_id)

        max_processing, first_processing, first_done, polls = 0, {}, None, 0
        timeout, statuses = 3 * 30 + 90, {}
        while True:
            statuses = {f["file_id"]: f["status"] for f in lib.batch(batch_id)}
            now = time.monotonic() - started
            polls += 1
            busy = [f for f, st in statuses.items() if st == "processing"]
            max_processing = max(max_processing, len(busy))
            for f in busy:
                first_processing.setdefault(f, now)
            if first_done is None and any(st == "processed" for st in statuses.values()):
                first_done = now
            if all(st in lib.TERMINAL for st in statuses.values()) or now > timeout:
                break
            time.sleep(0.5)

        finished = all(st in lib.TERMINAL for st in statuses.values())
        s.check(f"all three reached a terminal status within {timeout} s", finished, True)
        if not finished:
            print(f"         timed out after {polls} polls; statuses: {sorted(statuses.values())}")
        else:
            print(f"         {polls} polls; first file finished at +{first_done:.1f} s; files first seen processing at "
                  + ", ".join(f"+{t:.1f} s" for t in sorted(first_processing.values())))
        s.check(f"never more than {CONCURRENCY} processing at once", max_processing <= CONCURRENCY, True)
        s.check("both slots were used", max_processing, CONCURRENCY)
        third_start = max(first_processing.values()) if len(first_processing) == 3 else None
        s.check("the third stayed uploaded until one finished",
                third_start is not None and first_done is not None and third_start >= first_done - 0.5, True)
        finals = lib.batch(batch_id)
        s.check("all three processed", [f["status"] for f in finals], ["processed"] * 3)
        s.check("attempt_count = 1 for every file", [f["attempt_count"] for f in finals], [1, 1, 1])

        since = [l for l in lib.worker_lines("api") if lib.ts(l) >= started_wall]
        requeues = sum(len(set(ids) & set(l.get("file_ids", []))) for l in since if l.get("event") == "reconcile_sweep")
        lost = sum(1 for l in lib.worker_lines("worker-ingest") if l.get("event") == "claim_lost"
                   and l.get("file_id") in ids and lib.ts(l) >= started_wall)
        print(f"         duplicate re-enqueues by reconcile: {requeues}; claim_lost from duplicates: {lost} "
              "(accepted in 11.1 — outcome unchanged)")
        lib.assert_no_undeliverable(s)
    finally:
        os.environ.pop("SCENARIO_ENTRY_DELAY", None)
        lib.restore_worker_ingest()
        if batch_id:
            lib.cleanup(s, batch_id, list(keys.values()))
    return s.finish()


if __name__ == "__main__":
    lib.run(main)
