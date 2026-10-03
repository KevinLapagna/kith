# syntax=docker/dockerfile:1
#
# Kith — name & face trainer. Single-user, one SQLite file, no build step.
# Build:  podman build -t localhost/kith:0.1 -f Containerfile .
FROM python:3.13-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    KITH_DATA_DIR=/data \
    KITH_PORT=8080

WORKDIR /app

# Dependencies first, so the layer cache survives app edits.
# --require-hashes: the image is reproducible and nothing resolves at runtime.
COPY requirements.txt ./
RUN pip install --no-cache-dir --require-hashes -r requirements.txt

COPY app/ ./app/
COPY templates/ ./templates/
COPY static/ ./static/
COPY entrypoint.sh ./
RUN chmod 0755 /app/entrypoint.sh

# Non-root with a fixed uid. The uid is deliberate and load-bearing: the Quadlet
# unit maps it to the host user via `UserNS=keep-id:uid=10001,gid=10001`, which is
# what makes the bind-mounted /data writable (architecture doc §4.2).
RUN groupadd --gid 10001 kith \
 && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin kith \
 && mkdir -p /data && chown 10001:10001 /data
USER 10001

VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=4).status==200 else 1)"

ENTRYPOINT ["/app/entrypoint.sh"]         # preflight /data, then exec uvicorn
                                          # (migrations run in the app lifespan)
