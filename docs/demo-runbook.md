# Demo runbook — S5 and S5b (≈ 5 minutes)

**The question being answered** (Tasneem, 18 Sep): *"If my worker service terminates in between… I have already put
10 documents… there are 20 more files in that zip file which have to be read. So I should be able to read the next
20."*

**What the audience will see:** a 30-document archive being processed, the worker killed at document 10, the system
noticing on its own, and a fresh attempt carrying on from document 10 — ending with exactly 30 documents, none
duplicated. Then the same with only one worker process killed (the container stays up).

---

## Before the demo (10 minutes beforehand, not in front of the audience)

1. **Keep the laptop awake.** A suspend mid-demo stalls everything. Either turn off automatic suspend, or open every
   terminal below from a shell started with `systemd-inhibit --what=sleep bash`.
2. **Stack up and healthy:**
   ```bash
   docker compose up -d --build --wait
   docker compose ps          # 7 services; api, postgres, redis, localstack "healthy"
   ```
3. **Smoke test** (30 s; also builds the fixtures if needed): `python3 scripts/scenario_s6.py` → `PASSED`.
4. **Have a known-good run to fall back on:** `python3 scripts/scenario_s5.py | tee /tmp/s5-good.txt`
   (2 minutes). If anything misbehaves live, this output tells the same story.
5. Make the font big. Arrange four terminals as below, all in the repo directory.

## The four terminals

| Terminal | Purpose | Command |
|---|---|---|
| **T1 — driver** | Runs the scenario; prints PASS lines and the timeline at the end | (commands in the steps below) |
| **T2 — progress** | The file's state from the database, once a second | see below |
| **T3 — worker log** | What the worker is doing, entry by entry | see below |
| **T4 — recovery** | The stale sweeper noticing the dead worker | see below |

**T2 — progress** (survives any kill; it reads the database):
```bash
watch -n1 'docker compose exec -T postgres psql -U clinsync -d clinsync -c "SELECT file_name, status, entries_done || chr(47) || coalesce(entries_total::text, chr(63)) AS progress, attempt_count AS attempt FROM upload_file ORDER BY created_at DESC LIMIT 1"'
```

**T3 — worker log** (re-attaches by itself after the container is killed):
```bash
while true; do docker compose logs -f --no-log-prefix --since 2s worker-ingest 2>/dev/null \
  | jq -R -c --unbuffered 'fromjson? | select(.event|test("claim_ok|claim_lost|entry_staged|finished|WorkerLost|exited")) | {t: .ts[11:19], event: .event[0:60], attempt, entry: .entry_index, pid}'; \
  echo "---- worker-ingest gone; re-attaching ----"; sleep 1; done
```

**T4 — recovery** (only the sweeps that actually reset something):
```bash
docker compose logs -f --no-log-prefix --since 1s api \
  | jq -R -c --unbuffered 'fromjson? | select(.event=="stale_sweep" and .reset>0) | {t: .ts[11:19], event, reset, files: [.file_ids[]|.[0:8]]}'
```

---

## Part 1 — S5: kill the whole worker (≈ 2 min)

**Say:** "This archive has 30 Word documents. I've slowed the worker to 2 seconds per document so we can watch."

**T1:**
```bash
python3 scripts/scenario_s5.py
```

The script restarts `worker-ingest` with the 2-second delay, uploads `big30.zip`, confirms it, waits for document 10,
then runs `docker compose kill -s SIGKILL worker-ingest` and starts it again. You don't type anything else.

| Time (from confirm) | What happens | Point at |
|---|---|---|
| +0 s | `big30.zip` confirmed | T2: `processing 0/30`, attempt 1 |
| +2 … +19 s | Entries 0–9 staged | T3: `entry_staged` with `entry` 0, 1, 2 … 9, `attempt: 1` |
| **~+19 s** | **SIGKILL** at entry 10 | T3: stream ends, "worker-ingest gone; re-attaching". T2 freezes at `10/30` |
| +19 … ~+55 s | Silence. The heartbeat has stopped; the stale sweeper is waiting 30 s | T2 unchanged — "nobody is working on it, and the system knows that by the silent heartbeat" |
| **~+50 … +65 s** (30–45 s after the kill) | Stale sweeper resets the file and re-enqueues it | T4: `stale_sweep`, `reset: 1`, with the file id |
| right after | Claimed as **attempt 2** | T3: `claim_ok`, `attempt: 2`; T2: attempt 2 |
| right after | **Resumes at entry 10** | T3: first `entry_staged` of attempt 2 is `entry: 10` — **not 0** |
| ~+95 … +105 s | Done | T2: `processed 30/30`, attempt 2. T1: the PASS lines and the timeline |

**Say at the end** (read the timeline in T1): "Killed at entry 10. The sweeper reset it about 30 seconds later.
Attempt 2 picked up at entry 10 — the first ten weren't redone. Exactly 30 documents, 30 files in staging, no
duplicates, no orphans."

**Why it isn't instant:** the system only knows a worker died because its heartbeat goes silent for
`STALE_AFTER_SECONDS` (30 s here, 10 minutes in production), and the sweep runs every 15 s. So the reset lands
**30–45 s after the kill**, sometimes a few seconds more. That variation is normal.

## Part 2 — S5b: kill one worker process, container stays up (≈ 2 min)

**Say:** "Now I kill only the process doing the work. The container — and Celery's parent process — stay up."

**T1:**
```bash
python3 scripts/scenario_s5b.py
```

| Time | What happens | Point at |
|---|---|---|
| ~+9 s | Child process killed at entry 5 (its pid comes from its own `claim_ok` line) | T3: `WorkerLostError … signal 9` from the parent |
| **< 1 s later** | The parent put the message straight back; the redelivery **loses its claim** because the dead child's heartbeat is still fresh | T3: `claim_lost` — "within a second, not 30: that's `reject_on_worker_lost`" |
| 30–45 s after the kill | Stale sweeper resets it | T4: `stale_sweep`, `reset: 1` |
| right after | Attempt 2 resumes at entry 5 | T3: `claim_ok` attempt 2, then `entry: 5` |
| ~+95 s | Done | T2: `processed 30/30`, attempt 2. T1: timeline, "container still running, never restarted: PASS" |

Optional: `docker compose ps worker-ingest` — the "Up" time has not reset.

---

## What varies between runs (all normal)

- **Reset delay:** 30–45 s after the kill (heartbeat stale after 30 s, plus up to one 15 s sweep interval).
- **Kill point:** the script waits for `entries_done ≥ 10` (S5) or `≥ 5` (S5b); it may land on 10 or 11.
- **Resume point:** equal to the kill point, or one earlier if the kill landed between an upload and its progress
  update — that entry is simply redone and overwritten (same key, same candidate). Both pass.
- **`claim_lost` in S5b:** 0.3–0.6 s after the kill in our runs; the check allows 5 s.
- **Total time:** S5 ~95–105 s from confirm, S5b ~95 s.
- **T3 re-attaches more than once:** also at the start and end, because the script restarts `worker-ingest` with
  the 2-second delay and restores it afterwards. Right after a re-attach, the last line may appear twice
  (`--since 2s` overlaps) — the same entry, not a duplicate document.

Rehearsed on 25 Sep: killed at entry 10 at +19.0 s, reset 36 s after the kill, attempt 2 resumed at entry 10,
processed at +96.2 s; T4 showed exactly one `stale_sweep reset: 1`, at the second attempt 2 claimed.

## If something goes wrong mid-demo

| Symptom | What to do |
|---|---|
| T1 fails straight away (connection refused, LocalStack unhealthy) | `docker compose ps`; if LocalStack is not healthy, check `LOCALSTACK_AUTH_TOKEN` in `.env`, then `docker compose up -d --wait`. Show `/tmp/s5-good.txt` meanwhile. |
| T3 shows nothing | It only shows new lines — it starts showing once T1 confirms. If it stays empty, press Ctrl-C and re-run it. |
| T4 shows nothing after 60 s | Check the sweeper: `docker compose logs --since 2m worker-maint \| jq -R -c 'fromjson? \| select(.event|test("sweep"))'`. `sweep_failed` or no lines → `docker compose restart worker-maint`; the next sweep resets it. |
| The reset takes longer than ~60 s | Still fine up to ~90 s (the script allows 120 s + remaining work). Keep talking about why heartbeats, not timers, detect dead workers. |
| A `FAIL` line appears | Re-run with `--keep` after the demo to inspect; in the room, show `/tmp/s5-good.txt` and the timeline from the last good run. |
| Anything leaves the worker in a strange state | `docker compose up -d --force-recreate worker-ingest` restores the normal worker (no entry delay). The scripts do this themselves at the end, even on failure. |

## After the demo

Nothing to clean up: each script deletes its batch and S3 objects and restores the normal `worker-ingest`. Stop the
terminals with Ctrl-C. To show everything else, `./scripts/run_all.sh` (about 16 minutes; 13 entries).
