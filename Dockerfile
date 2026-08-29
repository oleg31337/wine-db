# wine-db backend image.
#
# Multi-stage: dependencies are resolved in a builder, then only the virtualenv
# and application code land in the runtime image. The runtime runs as a
# non-root user with no build toolchain present.

FROM python:3.13-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential libpq-dev \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip setuptools wheel \
 && /opt/venv/bin/pip install -r requirements.txt


FROM python:3.13-slim-bookworm AS runtime

ARG APP_VERSION=local
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    DATA_DIR=/data \
    APP_VERSION=${APP_VERSION}

RUN apt-get update \
 && apt-get install -y --no-install-recommends libpq5 curl \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --gid 10001 wine \
 && useradd --uid 10001 --gid wine --create-home --shell /usr/sbin/nologin wine

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY app/ ./app/
COPY static/ ./static/

# The uploads volume is mounted here; create it so the first boot works even
# when the volume starts empty.
RUN mkdir -p /data/uploads

USER wine

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8080/healthz || exit 1

# One worker keeps the in-process rate limiter and login throttle coherent.
# wine-db is a household-scale app; scale the reverse proxy, not this.
CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", "--port", "8080", \
     "--workers", "1", \
     "--proxy-headers", "--forwarded-allow-ips", "*", \
     "--no-server-header", \
     "--log-level", "info"]
