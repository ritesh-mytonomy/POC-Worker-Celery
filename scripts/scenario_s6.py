"""S6 · Duplicate delivery (design.md §10.2; R3.4, R3.5).

Confirm valid.docx, then POST /poc/enqueue/{file_id}?times=5. Six messages for one file:
exactly one claim_ok, five claim_lost, attempt_count = 1, one candidate.

Usage: python3 scripts/scenario_s6.py [--keep]    (--keep leaves the batch and S3 object in place)
"""
import uuid

import lib


def main() -> int:
    """Run S6; return the exit code."""
    s = lib.Scenario("S6 duplicate delivery")
    run_id = f"s6-{uuid.uuid4().hex[:8]}"
    key = lib.incoming_key(run_id, "valid.docx")
    lib.upload("valid.docx", key)
    batch_id, (file_id,) = lib.seed([("valid.docx", key)])
    try:
        confirmed = lib.confirm(file_id)
        s.check("confirm enqueued", confirmed["enqueued"], True)
        status, body = lib.api("POST", f"/poc/enqueue/{file_id}?times=5")
        s.check("/poc/enqueue published", (status, body and body.get("published")), (200, 5))

        claims = lib.wait_events("worker-ingest", file_id, {"claim_ok", "claim_lost"}, count=6)
        (final,) = lib.wait_terminal(batch_id)
        s.check("claim_ok log lines", sum(e["event"] == "claim_ok" for e in claims), 1)
        s.check("claim_lost log lines", sum(e["event"] == "claim_lost" for e in claims), 5)
        s.check("distinct task ids", len({e["task_id"] for e in claims}), 6)
        s.check("final status", final["status"], "processed")
        s.check("attempt_count", final["attempt_count"], 1)
        candidates = lib.staged(batch_id)
        if candidates:
            s.check("candidates", len(candidates), 1)
        else:
            s.pend("candidates == 1", "0 so far — process_upload is the task 7.1 skeleton; staging arrives in "
                                      "Phase 8 (task 8.2)")
        lib.assert_no_undeliverable(s)
    finally:
        lib.cleanup(s, batch_id, [key])
    return s.finish()


if __name__ == "__main__":
    lib.run(main)
