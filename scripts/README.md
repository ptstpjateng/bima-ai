# BIMA Ops Scripts

Helper scripts kept in the repo so they're version-controlled alongside the stack
they operate on. All paths assume the canonical VPS layout (`/home/wdnsds/bima-ai`).

---

## `backup-postgres.sh` — Daily PostgreSQL backup

Dumps the `bima_ai` database out of the running `postgres` compose service and
writes a timestamped gzip to `/var/backups/bima/`. Rotates anything older than
**30 days**. Designed for cron — quiet on success, fails loud (`set -euo pipefail`).

### What it does

1. `mkdir -p /var/backups/bima` (no-op if the directory already exists).
2. `docker compose exec -T postgres pg_dump -U bima -d bima_ai | gzip > /var/backups/bima/postgres-YYYY-MM-DD-HHMMSS.sql.gz`
3. `find /var/backups/bima/ -name "postgres-*.sql.gz" -mtime +30 -delete`
4. Prints filename, size, retention window, and remaining backup count.

### Test it manually

```bash
cd /home/wdnsds/bima-ai
bash scripts/backup-postgres.sh
ls -lh /var/backups/bima/
```

You should see `postgres-2026-05-13-HHMMSS.sql.gz` and a one-line summary on stdout.

### Install the cron job

Edit the user's crontab:

```bash
crontab -e
```

Paste this single line (runs at 02:00 every day, logs to `/var/log/bima-backup.log`):

```cron
0 2 * * * cd /home/wdnsds/bima-ai && bash scripts/backup-postgres.sh >> /var/log/bima-backup.log 2>&1
```

Make sure the log file is writable by the user running the cron:

```bash
sudo touch /var/log/bima-backup.log
sudo chown wdnsds:wdnsds /var/log/bima-backup.log
```

Verify the cron landed:

```bash
crontab -l | grep backup-postgres
```

### Restore from a backup

```bash
# Pick the dump you want to restore from /var/backups/bima/.
gunzip -c /var/backups/bima/postgres-2026-05-13-020001.sql.gz \
    | docker compose exec -T postgres psql -U bima -d bima_ai
```

> **Heads up:** `pg_dump` produces a plain SQL dump that includes `CREATE TABLE`
> statements. Restoring on top of a populated database will fail on existing
> objects. For a clean restore, drop and recreate the DB first:
>
> ```bash
> docker compose exec -T postgres psql -U bima -d postgres \
>     -c "DROP DATABASE bima_ai; CREATE DATABASE bima_ai OWNER bima;"
> ```
>
> then re-run the `gunzip | psql` line above.

### Where backups live

- **On disk:** `/var/backups/bima/postgres-*.sql.gz` (host filesystem, not a Docker volume).
- **Retention:** 30 days, enforced on every run.
- **Offsite:** not yet configured. A future improvement is to `rclone copy` the
  backup directory to S3 / Backblaze B2 after each dump — track that as a
  follow-up if data sensitivity warrants it.

---

## `test-rate-limit.sh` — Smoke test the Caddy rate limiter

Fires 50 rapid requests at `/webhook/__rate-limit-probe` and counts how many
come back as `429 Too Many Requests`. The Caddyfile sets the webhook bucket to
**30 requests / minute / source IP**, so the back half of the burst should be
throttled.

### What it asserts

- At least **15 of the 50** responses must be `429`. (Requests 31..50 — the 20
  past the 30/m bucket — should all be throttled; 15 leaves slack for clock
  skew and bucket warmup.)
- If fewer than 15 come back `429`, the script exits non-zero and prints a hint
  to check whether the `caddy-ratelimit` plugin actually compiled into the
  proxy image (`docker compose exec proxy caddy list-modules | grep ratelimit`).

### Run it

```bash
# Local stack (default target http://localhost)
bash scripts/test-rate-limit.sh

# Against production (read-only — the probe path 404s upstream)
bash scripts/test-rate-limit.sh https://nolongin.com
```

### Why not pytest

The thing under test is the proxy container's behavior on the wire, not Python
code. A shell + curl loop is the smallest thing that proves the bucket fires.
Wire into CI later if we want.
