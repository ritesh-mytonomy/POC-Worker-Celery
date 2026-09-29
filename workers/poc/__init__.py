"""POC-only worker code: the scan stub.

The dependency points one way: this package may import `app`, `engine` and `shared`; nothing in them may import or
reference it (tests/poc/test_poc_boundary.py). It is deleted before production.

  poc/worker.py  the scan worker's entry point (worker-scan runs `celery -A poc.worker`): app.tasks + scan_stub
"""
