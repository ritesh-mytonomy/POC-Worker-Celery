# The API image: backend/ + shared/. Built from the repo root (docker-compose.yml: context ., dockerfile
# backend/Dockerfile). No CMD — the api service sets its own command.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

COPY backend/requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt

COPY shared/ /srv/shared/
COPY backend/ /srv/backend/
# tests/test_models.py checks sql/schema.sql is the spec's schema byte for byte
COPY specs/upload-ingest-merge/schema.sql /srv/specs/upload-ingest-merge/schema.sql
ENV PYTHONPATH=/srv/backend:/srv
WORKDIR /srv/backend

# Run as an unprivileged user; the code stays root-owned and read-only to it.
RUN useradd --create-home --uid 10001 app
USER app

# Clear the base image's CMD ["python3"] so a service without its own command fails loudly.
CMD []
