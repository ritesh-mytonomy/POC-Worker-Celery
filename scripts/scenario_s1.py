"""S1 · Happy path, mixed archive (design.md §10.2; R5, R6, R7, R10).

Upload mixed.zip (3 valid .docx + notes.pdf + readme.txt), seed, confirm, wait for terminal. Assert: partial ·
entries_total = entries_done = 5 · 3 processed candidates with objects in staging/ · 2 rejected candidates with
reasons and no objects · incoming/ object deleted.

Usage: python3 scripts/scenario_s1.py [--keep]
"""
import uuid

import lib

PROCESSED = ["doc1.docx", "doc2.docx", "doc3.docx"]
REJECTED = {"notes.pdf": ".pdf is not supported", "readme.txt": ".txt is not supported"}


def main() -> int:
    """Run S1; return the exit code."""
    s = lib.Scenario("S1 happy path, mixed archive")
    run_id = f"s1-{uuid.uuid4().hex[:8]}"
    key = lib.incoming_key(run_id, "mixed.zip")
    lib.upload("mixed.zip", key)
    batch_id, (file_id,) = lib.seed([("mixed.zip", key)])
    try:
        s.check("confirm enqueued", lib.confirm(file_id)["enqueued"], True)
        (final,) = lib.wait_terminal(batch_id)
        s.check("status", final["status"], "partial")
        s.check("detected_type", final["detected_type"], "zip")
        s.check("entries_total, entries_done", (final["entries_total"], final["entries_done"]), (5, 5))
        s.check("attempt_count", final["attempt_count"], 1)
        candidates = {c["source_entry_name"]: c for c in lib.staged(batch_id)}
        s.check("candidates", sorted(candidates), sorted(PROCESSED + list(REJECTED)))
        s.check("processed candidates", sorted(n for n, c in candidates.items() if c["status"] == "processed"),
                PROCESSED)
        s.check("rejected candidates and reasons",
                {n: c["reject_reason"] for n, c in candidates.items() if c["status"] == "rejected"}, REJECTED)
        staged = sorted(k.rsplit("/", 1)[-1] for k in lib.staging_keys_for(file_id))
        s.check("objects in staging/ (processed only)", staged,
                ["0000_doc1.docx", "0001_doc2.docx", "0002_doc3.docx"])
        s.check("incoming/ object gone", lib.s3_keys(key), [])
        lib.assert_no_undeliverable(s)
    finally:
        lib.cleanup(s, batch_id, [key])
    return s.finish()


if __name__ == "__main__":
    lib.run(main)
