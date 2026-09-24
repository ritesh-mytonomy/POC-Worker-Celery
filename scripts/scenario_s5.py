"""S5 · Kill mid-archive, resume (design.md §10.2; R8, R11.4) — the demo.

ENTRY_DELAY_SECONDS=2, confirm big30.zip, wait until entries_done >= 10, SIGKILL the whole worker-ingest container,
start it again. The stale sweeper resets the file; attempt 2 resumes at the kill point.
Assert: processed · exactly 30 candidates · 30 distinct staging objects, no orphans · attempt_count = 2 ·
attempt 2's first entry is >= 10.

Usage: python3 scripts/scenario_s5.py [--keep] [--wait-visibility]
  --wait-visibility  also wait for the killed task's original message to come back after VISIBILITY_TIMEOUT
                     (300 s) and prove it simply loses its claim (adds ~5 minutes)
"""
import sys
import time

import lib
import resume_common as rc

VISIBILITY_TIMEOUT = 300


def main() -> int:
    """Run S5; return the exit code."""
    s = lib.Scenario("S5 kill mid-archive, resume")
    batch_id, file_id, key = "", "", ""
    try:
        batch_id, file_id, key, confirmed_at = rc.start_big30(rc.new_run_id("s5"))
        lib.wait_until(lambda: rc.entries_done(batch_id) >= 10, 90, "entries_done >= 10")
        before = rc.file_lines("worker-ingest", file_id)          # attempt 1, captured before the kill
        k = rc.entries_done(batch_id)
        killed_at = time.time()
        lib.compose_delay("kill", "-s", "SIGKILL", "worker-ingest")
        lib.compose_delay("up", "-d", "worker-ingest")
        print(f"  killed worker-ingest (SIGKILL) at entries_done={k}; waiting for the stale sweeper and attempt 2")
        lib.wait_terminal(batch_id, timeout=30 + 2 * 15 + 60 + (30 - k) * 2 + 30)
        lines = rc.file_lines("worker-ingest", file_id, before)
        facts = rc.check_recovery(s, batch_id, file_id, lines, killed_at, k, min_resume=10)
        lib.assert_no_undeliverable(s)
        rc.timeline("S5, whole container killed", confirmed_at, killed_at, k, facts)

        if "--wait-visibility" in sys.argv:
            first_delivery = lib.ts(next(l for l in lines if l["event"] == "claim_ok"))
            print(f"  --wait-visibility: waiting for the original message (unacked since the kill) to return "
                  f"~{VISIBILITY_TIMEOUT} s after its first delivery ...")
            lib.wait_until(lambda: any(l["event"] == "claim_lost" for l in rc.file_lines("worker-ingest", file_id)),
                           max(0.0, first_delivery + VISIBILITY_TIMEOUT + 60 - time.time()), "original redelivery")
            lost = next(l for l in rc.file_lines("worker-ingest", file_id) if l["event"] == "claim_lost")
            print(f"         returned {lib.ts(lost) - first_delivery:.0f} s after its first delivery")
            s.check("original message redelivered and lost its claim", lost["event"], "claim_lost")
            (final,) = lib.batch(batch_id)
            s.check("attempt_count still", final["attempt_count"], 2)
            s.check("status still", final["status"], "processed")
            s.check("candidates still", len(lib.staged(batch_id)), 30)
            s.check("nothing for it left in Redis", lib.queued_mentions(file_id), 0)
    finally:
        lib.restore_worker_ingest()
        if batch_id:
            lib.cleanup(s, batch_id, [key])
    return s.finish()


if __name__ == "__main__":
    lib.run(main)
