"""POC-only task names."""

# The name keeps its original "workers.scan." prefix: it is the §6.3 message contract, and renaming it would change
# what is on the wire and in Celery's log lines. The task itself now lives in poc/scan_stub.py.
TASK_SCAN_STUB = "workers.scan.scan_stub"
