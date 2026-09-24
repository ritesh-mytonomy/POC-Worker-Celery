"""S5b · Kill one child process (design.md §10.2; R9.2).

Only the prefork child running the task is killed; the Celery parent survives. reject_on_worker_lost puts the
message straight back: a claim_lost appears within 5 s (the dead child's heartbeat is still fresh). Then the same
recovery as S5: stale sweeper → attempt 2 → resume.
Assert: container still up · claim_lost within 5 s of the kill · processed · exactly 30 candidates ·
attempt_count = 2.

Usage: python3 scripts/scenario_s5b.py [--keep]
"""
import time

import lib
import resume_common as rc


def main() -> int:
    """Run S5b; return the exit code."""
    s = lib.Scenario("S5b kill one child process")
    batch_id, file_id, key = "", "", ""
    try:
        batch_id, file_id, key, confirmed_at = rc.start_big30(rc.new_run_id("s5b"))
        lib.wait_until(lambda: rc.entries_done(batch_id) >= 5, 60, "entries_done >= 5")
        claim = next(l for l in rc.file_lines("worker-ingest", file_id) if l["event"] == "claim_ok")
        pid = claim["pid"]
        s.check("child pid from claim_ok is not the parent (pid 1)", pid != 1, True)
        lib.compose("exec", "-T", "worker-ingest", "sh", "-c", f"kill -0 {pid}")        # alive, or this raises
        before = lib.container_info("worker-ingest")
        k = rc.entries_done(batch_id)
        killed_at = time.time()
        lib.compose("exec", "-T", "worker-ingest", "sh", "-c", f"kill -9 {pid}")
        print(f"  killed child pid {pid} at entries_done={k}")

        lib.wait_until(lambda: any(l["event"] == "claim_lost" for l in rc.file_lines("worker-ingest", file_id)), 15,
                       "claim_lost after the child kill")
        lost = next(l for l in rc.file_lines("worker-ingest", file_id) if l["event"] == "claim_lost")
        delay = lib.ts(lost) - killed_at
        s.check("claim_lost within 5 s of the kill (reject_on_worker_lost)", 0 <= delay <= 5, True)
        print(f"         claim_lost {delay:.2f} s after the kill")
        after = lib.container_info("worker-ingest")
        s.check("container still running, never restarted",
                (after["id"], after["started_at"], after["status"]), (before["id"], before["started_at"], "running"))
        lost_child = [l for l in lib.worker_lines("worker-ingest") if lib.ts(l) >= killed_at - 1 and
                      ("WorkerLostError" in l.get("event", "") + str(l.get("exc", "")) or "signal 9" in l["event"])]
        s.check("parent logged the lost child", len(lost_child) >= 1, True)
        if lost_child:
            print(f"         parent: {lost_child[0]['event'][:110]!r}")

        lib.wait_terminal(batch_id, timeout=30 + 2 * 15 + 60 + (30 - k) * 2 + 30)
        lines = rc.file_lines("worker-ingest", file_id)
        facts = rc.check_recovery(s, batch_id, file_id, lines, killed_at, k, min_resume=5)
        lib.assert_no_undeliverable(s)
        rc.timeline("S5b, one child process killed", confirmed_at, killed_at, k, facts,
                    extra=[f"+{lib.ts(lost) - confirmed_at:5.1f}s  message requeued at once; redelivery lost the claim "
                           f"({delay:.1f} s after the kill) — the parent stayed up"],
                    killed=f"one child process (pid {pid}) killed")
    finally:
        lib.restore_worker_ingest()
        if batch_id:
            lib.cleanup(s, batch_id, [key])
    return s.finish()


if __name__ == "__main__":
    lib.run(main)
