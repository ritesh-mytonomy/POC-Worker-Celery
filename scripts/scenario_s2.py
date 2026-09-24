"""S2 · Single document (design.md §10.2; R2.3, R5.4).

Upload and confirm valid.docx. Assert: processed · detected_type docx · 1 candidate · object in staging/ ·
incoming/ empty.

Usage: python3 scripts/scenario_s2.py [--keep]
"""
import uuid

import lib


def main() -> int:
    """Run S2; return the exit code."""
    s = lib.Scenario("S2 single document")
    run_id = f"s2-{uuid.uuid4().hex[:8]}"
    key = lib.incoming_key(run_id, "valid.docx")
    lib.upload("valid.docx", key)
    batch_id, (file_id,) = lib.seed([("valid.docx", key)])
    try:
        s.check("confirm enqueued", lib.confirm(file_id)["enqueued"], True)
        (final,) = lib.wait_terminal(batch_id)
        s.check("status", final["status"], "processed")
        s.check("detected_type", final["detected_type"], "docx")
        s.check("attempt_count", final["attempt_count"], 1)
        candidates = lib.staged(batch_id)
        s.check("candidates", [(c["file_name"], c["status"], c["source_entry_name"]) for c in candidates],
                [("valid.docx", "processed", None)])
        s.check("staged objects", [k.rsplit("/", 1)[-1] for k in lib.staging_keys_for(file_id)], ["0000_valid.docx"])
        s.check("incoming/ object gone", lib.s3_keys(key), [])
        lib.assert_no_undeliverable(s)
    finally:
        lib.cleanup(s, batch_id, [key])
    return s.finish()


if __name__ == "__main__":
    lib.run(main)
