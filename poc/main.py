"""The POC's API entry point: the production app plus the POC-only routes. The api service runs `poc.main:app`."""
from app.main import app
from poc.routes import router

app.include_router(router)

__all__ = ["app"]
