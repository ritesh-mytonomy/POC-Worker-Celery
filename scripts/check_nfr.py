"""Non-functional timing checks (task 13.2; requirements.md §3), with the MEASURED values reported.

NFR-1  confirm latency: p95 over 50 calls < 200 ms (worker-ingest stopped so only the endpoint is measured)
NFR-2  confirm → claim with an idle worker < 2 s
NFR-3  100-entry archive of ~50 KB documents end to end < 60 s

Each check runs several rounds so the spread across runs is visible. Every round must pass on its own.

Usage: python3 scripts/check_nfr.py [--rounds N] [--keep]
"""
import statistics
import sys
import time
import uuid

import lib

ROUNDS = int(sys.argv[sys.argv.index("--rounds") + 1]) if "--rounds" in sys.argv else 3


def percentile(values: list[float], p: float) -> float:
    """Nearest-rank percentile."""
    ordered = sorted(values)
    return ordered[max(0, int(round(p / 100 * len(ordered) + 0.5)) - 1)]


def nfr1(s: lib.Scenario, run_id: str) -> None:
    """Confirm p95 over 50 calls, per round."""
    print("  -- NFR-1 confirm latency (50 calls per round, limit p95 < 200 ms)")
    lib.compose("stop", "worker-ingest")
    try:
        p95s = []
        for r in range(ROUNDS):
            key = lib.incoming_key(run_id, f"n1_{r}.docx")
            lib.upload("valid.docx", key)                    # seed HEADs each object; the 50 rows share one
            batch_id, ids = lib.seed([(f"n1_{i}.docx", key) for i in range(50)])
            times = []
            for file_id in ids:
                t0 = time.perf_counter()
                status, body = lib.api("POST", f"/api/v1/uploads/{file_id}/confirm")
                times.append((time.perf_counter() - t0) * 1000)
                assert status == 200 and body["enqueued"], body
            p95 = percentile(times, 95)
            p95s.append(p95)
            print(f"         round {r + 1}: p50 {statistics.median(times):.1f} ms · p95 {p95:.1f} ms · "
                  f"max {max(times):.1f} ms")
            s.check(f"NFR-1 round {r + 1} p95 < 200 ms", p95 < 200, True)
            lib.redis_cli("DEL", "clinsync.ingest")          # drop the queued messages; these files are never processed
            lib.cleanup(s, batch_id, [key], check_terminal=False)
        print(f"         spread of p95 across rounds: {min(p95s):.1f}–{max(p95s):.1f} ms")
    finally:
        lib.restore_worker_ingest()


def nfr2(s: lib.Scenario, run_id: str) -> None:
    """Confirm → claim_ok with an idle worker, per round."""
    print("  -- NFR-2 confirm → claim, idle worker (limit < 2 s)")
    time.sleep(3)
    delays = []
    for r in range(ROUNDS + 2):
        key = lib.incoming_key(run_id, f"n2_{r}.docx")
        lib.upload("valid.docx", key)
        batch_id, (file_id,) = lib.seed([("valid.docx", key)])
        sent = time.time()
        lib.confirm(file_id)
        lib.wait_until(lambda: any(l["event"] == "claim_ok" for l in lib.events("worker-ingest", file_id)), 10,
                       "claim_ok")
        claimed = lib.ts(next(l for l in lib.events("worker-ingest", file_id) if l["event"] == "claim_ok"))
        delays.append(claimed - sent)
        lib.wait_terminal(batch_id)
        lib.cleanup(s, batch_id, [key])
        s.check(f"NFR-2 round {r + 1} < 2 s", claimed - sent < 2.0, True)
    print(f"         confirm → claim: " + ", ".join(f"{d * 1000:.0f} ms" for d in delays)
          + f"  (spread {min(delays) * 1000:.0f}–{max(delays) * 1000:.0f} ms)")


def nfr3(s: lib.Scenario, run_id: str) -> None:
    """big100.zip from confirm to processed, per round."""
    print("  -- NFR-3 100-entry archive of ~50 KB documents, end to end (limit < 60 s)")
    durations = []
    for r in range(ROUNDS):
        key = lib.incoming_key(run_id, f"big100_{r}.zip")
        lib.upload("big100.zip", key)
        batch_id, (file_id,) = lib.seed([("big100.zip", key)])
        sent = time.time()
        lib.confirm(file_id)
        (final,) = lib.wait_terminal(batch_id, timeout=120)
        done = lib.ts(next(l for l in lib.events("worker-ingest", file_id) if l["event"] == "finished"))
        durations.append(done - sent)
        s.check(f"NFR-3 round {r + 1}: processed, 100 candidates",
                (final["status"], len(lib.staged(batch_id))), ("processed", 100))
        s.check(f"NFR-3 round {r + 1} < 60 s", done - sent < 60, True)
        lib.cleanup(s, batch_id, [key])
    print(f"         end to end: " + ", ".join(f"{d:.1f} s" for d in durations)
          + f"  (spread {min(durations):.1f}–{max(durations):.1f} s)")


def main() -> int:
    """Run the three timing checks."""
    s = lib.Scenario(f"13.2 non-functional timings ({ROUNDS} rounds)")
    run_id = f"nfr-{uuid.uuid4().hex[:8]}"
    nfr1(s, run_id)
    nfr2(s, run_id)
    nfr3(s, run_id)
    lib.assert_no_undeliverable(s)
    return s.finish()


if __name__ == "__main__":
    lib.run(main)
