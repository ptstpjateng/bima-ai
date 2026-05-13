# BIMA admin-api — slim Python 3.12 base, non-root, port 8001.
# Port 8001 is deliberate: ai-engine already binds 8000, so the two services
# can coexist on the same Docker network without a port clash.
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# System deps. libpq is NOT required because asyncpg ships its own protocol
# implementation. Keep this image lean.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/*

# ----------------------------------------------------------------------------
# Non-root user — devops-security.md flagged running-as-root as a known issue.
# ----------------------------------------------------------------------------
RUN groupadd --system --gid 1001 bima \
    && useradd --system --uid 1001 --gid bima --create-home --shell /sbin/nologin bima

WORKDIR /app

# Install Python deps first (better Docker layer caching).
COPY --chown=bima:bima requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Now copy source.
COPY --chown=bima:bima . ./

USER bima

EXPOSE 8001

# 2 workers gives us a small concurrency margin without making the SQLAlchemy
# pool blow up. Tune via env in compose if needed.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001", "--workers", "2"]
