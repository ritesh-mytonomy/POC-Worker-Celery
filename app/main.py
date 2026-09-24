"""FastAPI entry point — minimal stub for Checkpoint A; replaced by tasks 2.x."""
from fastapi import FastAPI

app = FastAPI(title="ClinSync Ingest POC")


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness probe."""
    return {"status": "ok"}
