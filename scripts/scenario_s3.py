"""S3 · Renamed files (design.md §10.2; R5.3).

Upload and confirm renamed_exe.docx and renamed_zip.docx. Assert: both rejected · messages name `unknown` and
`zip` respectively · zero candidates · nothing in staging/.

Usage: python3 scripts/scenario_s3.py [--keep]
"""
import uuid

import lib

EXPECTED = {"renamed_exe.docx": "unknown", "renamed_zip.docx": "zip"}


def main() -> int:
    """Run S3; return the exit code."""
    s = lib.Scenario("S3 renamed files")
    run_id = f"s3-{uuid.uuid4().hex[:8]}"
    keys = {name: lib.incoming_key(run_id, name) for name in EXPECTED}
    for name, key in keys.items():
        lib.upload(name, key)
    batch_id, _ = lib.seed(list(keys.items()))
    try:
        files = {f["file_name"]: f for f in lib.batch(batch_id)}
        for name in EXPECTED:
            s.check(f"{name} confirm enqueued", lib.confirm(files[name]["file_id"])["enqueued"], True)
        finals = {f["file_name"]: f for f in lib.wait_terminal(batch_id)}
        for name, real_type in EXPECTED.items():
            s.check(f"{name} status", finals[name]["status"], "rejected")
            message = finals[name]["status_message"] or ""
            s.check(f"{name} message names {real_type!r}", f"appears to be {real_type}" in message, True)
            print(f"         message: {message!r}")
            s.check(f"{name} detected_type", finals[name]["detected_type"], real_type)
        s.check("candidates", lib.staged(batch_id), [])
        s.check("staged objects", lib.staging_keys_for_batch(batch_id), [])
        s.check("incoming/ objects gone", [k for key in keys.values() for k in lib.s3_keys(key)], [])
        lib.assert_no_undeliverable(s)
    finally:
        lib.cleanup(s, batch_id, list(keys.values()))
    return s.finish()


if __name__ == "__main__":
    lib.run(main)
