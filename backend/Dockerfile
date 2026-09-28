# One image for every backend service (R1.3): api, workers, poc. No CMD — each compose service sets its own command.
# Built from the repo root (docker-compose.yml: context ., dockerfile backend/Dockerfile), so the schema test can
# compare infra/postgres/init.sql with specs/…/schema.sql; the code runs from /srv/backend.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv

COPY backend/requirements.txt .
RUN pip install -r requirements.txt

COPY . .
WORKDIR /srv/backend

# Run as an unprivileged user; the code stays root-owned and read-only to it.
RUN useradd --create-home --uid 10001 app
USER app

# Clear the base image's CMD ["python3"] so a service without its own command fails loudly.
CMD []
