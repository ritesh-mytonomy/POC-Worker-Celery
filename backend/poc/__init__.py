"""POC-only API code: stand-ins for parts of the real product and scenario helpers.

The dependency points one way: this package may import `app` and `shared`; nothing in `app/` or `shared/` may import
or reference it (tests/poc/test_poc_boundary.py). It is deleted before production.

  poc/main.py    the API entry point (api runs `uvicorn poc.main:app`): app.main:app + the /poc routes
"""
