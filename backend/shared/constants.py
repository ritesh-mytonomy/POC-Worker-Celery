"""Queue and task names (design.md §6.3), shared by the API's producer and the workers."""

QUEUE_INGEST = "clinsync.ingest"
QUEUE_SCAN = "clinsync.scan"
QUEUE_MAINTENANCE = "clinsync.maintenance"

TASK_PROCESS_UPLOAD = "workers.ingest.process_upload"
TASK_STALE_SWEEP = "workers.sweepers.run_stale_sweep"
TASK_RECONCILE_SWEEP = "workers.sweepers.run_reconcile_sweep"
TASK_ABANDONED_SWEEP = "workers.sweepers.run_abandoned_sweep"      # upload-ingest-merge U4.3
