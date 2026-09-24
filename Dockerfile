# One image for every service (R1.3). No CMD — each compose service sets its own command.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Clear the base image's CMD ["python3"] so a service without its own command fails loudly.
CMD []
