"""POC-only code: stand-ins for parts of the real product and scenario helpers.

The dependency points one way: this package may import `app` and `workers`; nothing in `app/` or `workers/` may
import or reference it (tests/test_poc_boundary.py). It is deleted before production.

  poc/main.py    the API entry point (api runs `uvicorn poc.main:app`): app.main:app + the /poc routes
  poc/worker.py  the scan worker's entry point (worker-scan runs `celery -A poc.worker`): workers.tasks + scan_stub

TASK_SCAN_STUB lives here so both can import it without loading each other: the scan worker never loads the
FastAPI app, and the API never loads the worker app (design.md §6.2a).
"""

# The name keeps its original "workers.scan." prefix: it is the §6.3 message contract, and renaming it would change
# what is on the wire and in Celery's log lines. The task itself is defined in poc/worker.py.
TASK_SCAN_STUB = "workers.scan.scan_stub"
