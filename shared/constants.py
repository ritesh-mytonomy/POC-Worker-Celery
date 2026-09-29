"""Queue and task names (design.md §6.3), shared by the API's producer and the workers."""

QUEUE_INGEST = "clinsync.ingest"
QUEUE_SCAN = "clinsync.scan"
QUEUE_MAINTENANCE = "clinsync.maintenance"

TASK_PROCESS_UPLOAD = "workers.ingest.process_upload"
TASK_STALE_SWEEP = "workers.sweepers.run_stale_sweep"
TASK_RECONCILE_SWEEP = "workers.sweepers.run_reconcile_sweep"
TASK_ABANDONED_SWEEP = "workers.sweepers.run_abandoned_sweep"      # upload-ingest-merge U4.3

# POC only (design.md §6.3): the scan stub's task name. It keeps its original "workers.scan." prefix — it is the
# message contract, and renaming it would change what is on the wire and in Celery's log lines. The task is defined
# in workers/poc/worker.py and published by backend/poc/main.py.
TASK_SCAN_STUB = "workers.scan.scan_stub"
