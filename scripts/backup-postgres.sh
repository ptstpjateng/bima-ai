#!/usr/bin/env bash
# shellcheck shell=bash
#
# backup-postgres.sh — daily PostgreSQL backup for BIMA-AI
#
# Dumps the bima_ai database from the running `postgres` compose service,
# gzips it to /var/backups/bima/, and rotates anything older than 30 days.
#
# Designed to be run from the project root on the VPS, e.g.:
#   cd /home/wdnsds/bima-ai && bash scripts/backup-postgres.sh
#
# See scripts/README.md for cron installation + restore instructions.

set -euo pipefail

BACKUP_DIR="/var/backups/bima"
TIMESTAMP="$(date +%Y-%m-%d-%H%M%S)"
BACKUP_FILE="${BACKUP_DIR}/postgres-${TIMESTAMP}.sql.gz"
RETENTION_DAYS=30

# Ensure the destination exists (no-op if already present).
mkdir -p "${BACKUP_DIR}"

# Stream pg_dump straight through gzip to disk. -T avoids allocating a TTY
# inside the container so this is safe to run from cron.
docker compose exec -T postgres pg_dump -U bima -d bima_ai \
    | gzip \
    > "${BACKUP_FILE}"

# Rotate: drop anything older than the retention window. -mtime +30 means
# strictly older than 30 days; today's fresh dump is never at risk.
find "${BACKUP_DIR}" -name "postgres-*.sql.gz" -mtime +${RETENTION_DAYS} -delete

# Summary — written to stdout so cron captures it via the log redirect.
BACKUP_SIZE="$(du -h "${BACKUP_FILE}" | cut -f1)"
BACKUP_COUNT="$(find "${BACKUP_DIR}" -name "postgres-*.sql.gz" -type f | wc -l | tr -d ' ')"

echo "[$(date -Iseconds)] backup ok"
echo "  file:    ${BACKUP_FILE}"
echo "  size:    ${BACKUP_SIZE}"
echo "  retain:  ${RETENTION_DAYS} days"
echo "  total:   ${BACKUP_COUNT} backup(s) on disk"
