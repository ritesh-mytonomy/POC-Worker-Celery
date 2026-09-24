"""S8 · Redis loss (design.md §10.2; R2.6, R11.5, R11.6, R11.7).

Phase A — Redis down at confirm: confirm still answers 200 `uploaded` within 2 s and logs enqueue_failed.
Phase B — queued messages lost: two confirmed files sit in clinsync.ingest, then Redis is replaced with an empty
one (container and volume removed — not FLUSHALL on a live broker). After it returns, worker-maint reconnects, the
sweeps resume, the API's producer publishes again, and the reconcile sweeper recovers all three files.

Usage: python3 scripts/scenario_s8.py [--keep]
"""
import subprocess
import time
import uuid

import lib

SWEEPS = ("stale_sweep", "reconcile_sweep")


def sweeps_between(t0: float, t1: float) -> list[dict]:
    """worker-maint sweep lines with t0 < ts < t1."""
    return [l for l in lib.worker_lines("worker-maint") if l.get("event") in SWEEPS and t0 < lib.ts(l) < t1]


def main() -> int:
    """Run S8; return the exit code."""
    s = lib.Scenario("S8 Redis loss")
    run_id = f"s8-{uuid.uuid4().hex[:8]}"
    keys = {n: lib.incoming_key(run_id, n) for n in ("valid_a.docx", "valid_b.docx", "valid_c.docx")}
    for key in keys.values():
        lib.upload("valid.docx", key)
    batch_id, _ = lib.seed(list(keys.items()))
    ids = {f["file_name"]: f["file_id"] for f in lib.batch(batch_id)}
    started = time.time()
    try:
        print("  -- Phase A: Redis down at confirm")
        lib.compose("stop", "redis")
        down_at = time.time()
        t0 = time.monotonic()
        status, body = lib.api("POST", f"/api/v1/uploads/{ids['valid_a.docx']}/confirm")
        elapsed = time.monotonic() - t0
        s.check("confirm answered 200 within 2 s", (status, elapsed < 2.0), (200, True))
        print(f"         confirm took {elapsed:.2f} s")
        s.check("status uploaded, not enqueued", (body["status"], body["enqueued"]), ("uploaded", False))
        failed = [l for l in lib.worker_lines("api") if l.get("event") == "enqueue_failed"
                  and l.get("file_id") == ids["valid_a.docx"]]
        s.check("API logged enqueue_failed", len(failed) >= 1, True)
        time.sleep(20)                                                    # more than one sweep interval
        maint = [l for l in lib.worker_lines("worker-maint") if lib.ts(l) > down_at]
        s.check("no sweep ran while Redis was down (beat cannot publish)",
                [l for l in maint if l.get("event") in SWEEPS and lib.ts(l) > down_at + 2], [])
        broker = [l for l in maint if "connect" in l.get("event", "").lower() or "broker" in l.get("event", "").lower()]
        s.check("worker-maint logged the broker connection loss", len(broker) >= 1, True)
        if broker:
            print(f"         worker-maint: {broker[0]['event'][:100]!r}")
        lib.compose("up", "-d", "--wait", "redis")

        print("  -- Phase B: queued messages lost")
        lib.compose("stop", "worker-ingest")
        for name in ("valid_b.docx", "valid_c.docx"):
            s.check(f"{name} confirmed and enqueued", lib.confirm(ids[name])["enqueued"], True)
        queued = int(lib.redis_cli("LLEN", "clinsync.ingest"))
        s.check("LLEN clinsync.ingest >= 2 (really queued)", queued >= 2, True)
        print(f"         LLEN clinsync.ingest = {queued}")
        lib.compose("rm", "-f", "-s", "redis")
        removed = subprocess.run(["docker", "volume", "rm", "clinsync-ingest-poc_redis-data"], capture_output=True,
                                 text=True)
        s.check("Redis container and volume removed", removed.returncode, 0)
        lib.compose("up", "-d", "--wait", "redis", "worker-ingest")
        back_at = time.time()
        bindings = lib.redis_cli("KEYS", "_kombu.binding.*")
        print(f"         Redis restarted from an empty volume; seconds later: LLEN clinsync.ingest = "
              f"{lib.redis_cli('LLEN', 'clinsync.ingest')} (republished since), bindings redeclared = "
              f"{len([b for b in bindings.splitlines() if b])}")

        lib.wait_until(lambda: all(f["status"] == "processed" for f in lib.batch(batch_id)), 30 + 2 * 15 + 30,
                       "all three processed")
        finals = {f["file_name"]: f for f in lib.batch(batch_id)}
        s.check("all three processed", {n: f["status"] for n, f in finals.items()},
                {n: "processed" for n in keys})
        s.check("attempt_count 1 each (lost messages never claimed)", {n: f["attempt_count"] for n, f in finals.items()},
                {n: 1 for n in keys})
        resumed = sweeps_between(back_at, time.time())
        s.check("sweeps resumed after the empty Redis returned", len(resumed) >= 2, True)
        if resumed:
            print(f"         first sweep {min(lib.ts(l) for l in resumed) - back_at:.1f} s after Redis returned")
        recon = [l for l in lib.worker_lines("api") if l.get("event") == "reconcile_sweep" and lib.ts(l) > started]
        requeued = sum(l.get("requeued", 0) for l in recon)
        recovered = {f for l in recon for f in l.get("file_ids", [])}
        s.check("reconcile requeued >= 3 in total", requeued >= 3, True)
        s.check("reconcile covered all three files", set(ids.values()) <= recovered, True)
        failed_publishes = sum(l.get("enqueue_failed", 0) for l in recon if lib.ts(l) > back_at)
        print(f"         reconcile after Redis returned: requeued {sum(l.get('requeued', 0) for l in recon if lib.ts(l) > back_at)}, "
              f"enqueue_failed {failed_publishes} (a dead pooled socket may fail once; the producer then resets)")
        s.check("API producer published again (files recovered through its requeues)",
                all(f["status"] == "processed" for f in finals.values()), True)
        lib.assert_no_undeliverable(s)
    finally:
        lib.compose("up", "-d", "--wait", "redis", "worker-ingest")
        lib.cleanup(s, batch_id, list(keys.values()))
    return s.finish()


if __name__ == "__main__":
    lib.run(main)
