"""Shared by S5 and S5b: run big30.zip slowly, then check the resumed run and print the demo timeline."""
import time
import uuid
from typing import Any

import lib

ENTRIES = 30
EXPECTED_OBJECTS = [f"{i:04d}_doc{i + 1:02d}.docx" for i in range(ENTRIES)]


def start_big30(run_id: str) -> tuple[str, str, str, float]:
    """Recreate worker-ingest with the entry delay, then upload, seed and confirm big30.zip."""
    lib.compose_delay("up", "-d", "--wait", "--force-recreate", "worker-ingest")
    time.sleep(3)                                            # let the worker finish booting
    key = lib.incoming_key(run_id, "big30.zip")
    lib.upload("big30.zip", key)
    batch_id, (file_id,) = lib.seed([("big30.zip", key)])
    confirmed_at = time.time()
    lib.confirm(file_id)
    return batch_id, file_id, key, confirmed_at


def entries_done(batch_id: str) -> int:
    """The file's current entries_done."""
    return lib.batch(batch_id)[0]["entries_done"] or 0


def file_lines(service: str, file_id: str, extra: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """The service's log lines for file_id, merged with an earlier snapshot, de-duplicated, in time order."""
    seen, merged = set(), []
    for line in [*(extra or []), *lib.worker_lines(service)]:
        if line.get("file_id") != file_id:
            continue
        marker = (line["ts"], line["event"], line.get("pid"))
        if marker not in seen:
            seen.add(marker)
            merged.append(line)
    return sorted(merged, key=lambda l: l["ts"])


def check_recovery(s: lib.Scenario, batch_id: str, file_id: str, lines: list[dict[str, Any]], killed_at: float,
                   k: int, min_resume: int) -> dict[str, Any]:
    """Assert the resumed run and the end state; return the facts the timeline prints."""
    (final,) = lib.wait_terminal(batch_id, timeout=5)
    s.check("status", final["status"], "processed")
    s.check("attempt_count", final["attempt_count"], 2)
    s.check("entries_total, entries_done", (final["entries_total"], final["entries_done"]), (ENTRIES, ENTRIES))
    candidates = lib.staged(batch_id)
    s.check("candidates (exactly 30, all processed)", (len(candidates), {c["status"] for c in candidates}),
            (ENTRIES, {"processed"}))
    objects = sorted(k_.rsplit("/", 1)[-1] for k_ in lib.staging_keys_for(file_id))
    s.check("30 distinct staging objects == 0000_doc01 … 0029_doc30, no orphans", objects == EXPECTED_OBJECTS, True)

    claims = [l for l in lines if l["event"] == "claim_ok"]
    s.check("claim_ok lines (attempts)", [l["attempt"] for l in claims], [1, 2])
    resets = [l for l in lib.worker_lines("api") if l.get("event") == "stale_sweep" and file_id in l.get("file_ids", [])
              and lib.ts(l) > killed_at]
    s.check("stale sweeper reset the file after the kill", len(resets) >= 1, True)
    second = claims[1] if len(claims) > 1 else None
    if resets and second:
        s.check("reset happened before attempt 2's claim", lib.ts(resets[0]) <= lib.ts(second), True)
    attempt2 = [l["entry_index"] for l in lines if l.get("attempt") == 2 and l["event"] in ("entry_staged",
                                                                                          "entry_rejected")]
    first2 = min(attempt2) if attempt2 else None
    s.check(f"attempt 2 resumed at entry >= {min_resume} (not restarted)", first2 is not None and first2 >= min_resume,
            True)
    s.check("attempt 2 started at the kill point K (or K-1: entry in flight replayed)", first2 in (k, k - 1), True)
    finished = [l for l in lines if l["event"] == "finished"]
    return {"final": final, "reset_at": lib.ts(resets[0]) if resets else None,
            "done_at": lib.ts(finished[-1]) if finished else None,
            "claim2_at": lib.ts(second) if second else None, "resumed_at": first2, "candidates": len(candidates),
            "objects": len(objects)}


def timeline(title: str, confirmed_at: float, killed_at: float, k: int, facts: dict[str, Any],
             extra: list[str] | None = None, killed: str = "worker container killed") -> None:
    """Print a short plain-language timeline for the demo (task 14.2)."""
    def at(t: float | None) -> str:
        return f"+{t - confirmed_at:5.1f}s" if t else "   n/a"

    print(f"\n  Timeline — {title}")
    print(f"    {at(confirmed_at)}  big30.zip confirmed (30 documents, 2 s per entry)")
    print(f"    {at(killed_at)}  {killed} at entry {k} ({k} documents already staged)")
    for line in extra or []:
        print(f"    {line}")
    if facts["reset_at"]:
        print(f"    {at(facts['reset_at'])}  stale sweeper reset the file ({facts['reset_at'] - killed_at:.0f} s after "
              f"the kill: heartbeat silent > STALE_AFTER_SECONDS)")
    print(f"    {at(facts['claim2_at'])}  claimed again as attempt 2")
    print(f"    {at(facts['claim2_at'])}  resumed at entry {facts['resumed_at']} — entries 0–{k - 1} not redone")
    final = facts["final"]
    print(f"    {at(facts['done_at'])}  {final['status']}: {facts['candidates']} candidates, "
          f"{facts['objects']} staged objects, attempt_count {final['attempt_count']}\n")


def new_run_id(prefix: str) -> str:
    """A fresh run id."""
    return f"{prefix}-{uuid.uuid4().hex[:8]}"
