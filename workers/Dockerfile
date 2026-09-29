# The worker image: workers/ + shared/, for worker-ingest, worker-maint and worker-scan. Built from the repo root
# (docker-compose.yml: context ., dockerfile workers/Dockerfile). No CMD — each service sets its own command.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

COPY workers/requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt

COPY shared/ /srv/shared/
COPY workers/ /srv/workers/
ENV PYTHONPATH=/srv/workers:/srv
WORKDIR /srv/workers

# Run as an unprivileged user; the code stays root-owned and read-only to it.
RUN useradd --create-home --uid 10001 app
USER app

# Clear the base image's CMD ["python3"] so a service without its own command fails loudly.
CMD []
