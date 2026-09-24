"""Task 9.3 end to end (design.md §8.5a; R6.8), against the running stack.

A. lying3.zip — the third entry fails the OUTER CRC: the archive ends rejected, entries 1–2 become rejected with
   "Archive rejected: …" and no s3_key, nothing is left under its staging prefix, incoming/ is deleted.
B. inner_bomb.zip — an INNER .docx is a zip bomb: the archive ends partial, only that entry is rejected ("unsafe").

Usage: python3 scripts/check_archive_rejection.py [--keep]
"""
import os
import uuid

import lib


def s3_keys_in_db(file_id: str) -> dict[str, str | None]:
    """entry name → s3_key, read from Postgres (the public API does not expose s3_key)."""
    user, db = os.environ.get("POSTGRES_USER", "clinsync"), os.environ.get("POSTGRES_DB", "clinsync")
    out = lib.compose("exec", "-T", "postgres", "psql", "-U", user, "-d", db, "-tA", "-F", "|", "-c",
                      f"SELECT source_entry_name, coalesce(s3_key, '') FROM staged_document "
                      f"WHERE source_file_id = '{uuid.UUID(file_id)}'")
    pairs = (line.split("|", 1) for line in out.splitlines() if line)
    return {name: key or None for name, key in pairs}


def main() -> int:
    """Run both cases; return the exit code."""
    s = lib.Scenario("9.3 archive rejected mid-way / inner bomb")
    run_id = f"a93-{uuid.uuid4().hex[:8]}"
    keys = {name: lib.incoming_key(run_id, name) for name in ("lying3.zip", "inner_bomb.zip")}
    for name, key in keys.items():
        lib.upload(name, key)
    batch_id, _ = lib.seed(list(keys.items()))
    try:
        files = {f["file_name"]: f for f in lib.batch(batch_id)}
        for name in keys:
            lib.confirm(files[name]["file_id"])
        finals = {f["file_name"]: f for f in lib.wait_terminal(batch_id)}
        staged = lib.staged(batch_id)

        print("  -- A. lying3.zip (outer CRC failure on the third entry)")
        lying = finals["lying3.zip"]
        lying_id = lying["file_id"]
        s.check("status", lying["status"], "rejected")
        s.check("message names the CRC failure of d2.docx",
                "'d2.docx' does not match its index" in (lying["status_message"] or "") and
                "CRC" in (lying["status_message"] or ""), True)
        print(f"         message: {lying['status_message']!r}")
        s.check("entries_done", lying["entries_done"], 2)
        mine = {c["source_entry_name"]: c for c in staged if c["source_file_id"] == lying_id}
        s.check("candidates", sorted(mine), ["d0.docx", "d1.docx"])
        s.check("entries 1–2 rejected", [mine[n]["status"] for n in ("d0.docx", "d1.docx")], ["rejected", "rejected"])
        s.check("reasons 'Archive rejected: …'",
                all((mine[n]["reject_reason"] or "") == f"Archive rejected: {lying['status_message']}"
                    for n in ("d0.docx", "d1.docx")), True)
        s.check("s3_key cleared (Postgres)", s3_keys_in_db(lying_id), {"d0.docx": None, "d1.docx": None})
        s.check("nothing under its staging prefix", lib.staging_keys_for(lying_id), [])
        s.check("incoming/ object gone", lib.s3_keys(keys["lying3.zip"]), [])

        print("  -- B. inner_bomb.zip (inner .docx is a zip bomb)")
        bomb = finals["inner_bomb.zip"]
        bomb_id = bomb["file_id"]
        s.check("status", bomb["status"], "partial")
        mine = {c["source_entry_name"]: c for c in staged if c["source_file_id"] == bomb_id}
        s.check("statuses", {n: c["status"] for n, c in mine.items()},
                {"a.docx": "processed", "bomb.docx": "rejected", "c.docx": "processed"})
        s.check("bomb.docx reason is 'unsafe'", (mine["bomb.docx"]["reject_reason"] or "").startswith("File is unsafe:"),
                True)
        print(f"         reason: {mine['bomb.docx']['reject_reason']!r}")
        s.check("objects for processed entries only",
                sorted(k.rsplit("/", 1)[-1] for k in lib.staging_keys_for(bomb_id)), ["0000_a.docx", "0002_c.docx"])
        s.check("incoming/ object gone", lib.s3_keys(keys["inner_bomb.zip"]), [])
        lib.assert_no_undeliverable(s)
    finally:
        lib.cleanup(s, batch_id, list(keys.values()))
    return s.finish()


if __name__ == "__main__":
    lib.run(main)
