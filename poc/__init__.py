"""POC-only code: stand-ins for parts of the real product and scenario helpers.

The dependency points one way: this package may import `app` and `workers`; nothing in `app/` or `workers/` may
import or reference it (tests/test_poc_boundary.py). It is deleted before production.
"""
