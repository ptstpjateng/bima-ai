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

# Pre-create the uploads mountpoint with the correct ownership BEFORE the
# volume gets mounted at runtime. Docker's "copy on first use" semantics for
# named volumes preserve this chown onto a fresh `ingestion_uploads` volume,
# so admin-api (running as uid 1001) can write without a host-side chmod.
# Without this, a brand-new volume comes up as root:root 0755 and every
# upload 500s until someone manually `chmod 777`s the host bind path.
RUN mkdir -p /app/uploads && chown bima:bima /app/uploads

USER bima

EXPOSE 8001

# 2 workers gives us a small concurrency margin without making the SQLAlchemy
# pool blow up. Tune via env in compose if needed.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001", "--workers", "2"]
