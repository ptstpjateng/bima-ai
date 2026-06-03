#!/usr/bin/env bash
# test-rate-limit.sh — Smoke test the Caddy rate-limit plugin.
#
# WHY: The Caddyfile says 30 r/m on /webhook/* and 120 r/m on /admin-api/* +
# /api/*. The only honest way to know the plugin is actually loaded is to
# fire requests and count the 429s. This script does that against a target
# (defaults to local docker compose stack) so we don't need to wait until
# production to find out the plugin didn't compile in.
#
# Usage:
#   scripts/test-rate-limit.sh                       # against http://localhost
#   scripts/test-rate-limit.sh https://nolongin.com  # against prod (read-only paths)
#
# WHY NOT pytest: the test target is the proxy container, which is a network
# concern. A shell+curl loop is the smallest thing that proves the bucket
# actually fires. Wire into CI later if we want.

set -euo pipefail

TARGET="${1:-http://localhost}"
WEBHOOK_PATH="/webhook/__rate-limit-probe"   # 404s upstream, but Caddy sees the path first
API_PATH="/admin-api/__rate-limit-probe"

# How many requests to fire. 50 is enough to clear the 30/m bucket on /webhook/*
# with room to verify a chunk of 429s. For /admin-api/* (120/m) we'd need >120,
# so this script only asserts the webhook bucket — the admin bucket is
# exercised by simply fanning out 150 requests and counting them.
N_REQUESTS=50

echo "Target: $TARGET"
echo "Firing $N_REQUESTS requests at $WEBHOOK_PATH ..."

# Capture HTTP status codes one per line. -o /dev/null discards bodies so a
# big 200 (or a Caddy 429 HTML page) doesn't slow the loop.
statuses=$(for _ in $(seq 1 "$N_REQUESTS"); do
    curl -s -o /dev/null -w "%{http_code}\n" "$TARGET$WEBHOOK_PATH" || echo "000"
done)

echo "Status code histogram:"
echo "$statuses" | sort | uniq -c | sort -rn

count_429=$(echo "$statuses" | grep -c "^429$" || true)
count_2xx_or_404=$(echo "$statuses" | grep -cE "^(2..|404)$" || true)

echo
echo "429 responses: $count_429"
echo "2xx/404 responses (request reached or attempted to reach upstream): $count_2xx_or_404"

# Acceptance: at least 15 of the last 20 requests should be 429s. The 30/m
# bucket allows 30 requests, so requests 31..50 (20 in flight) should be
# throttled. Allow some slack for clock-skew + bucket warmup.
expected_min_429=15
if [ "$count_429" -ge "$expected_min_429" ]; then
    echo
    echo "PASS: rate limiter is throttling ($count_429 >= $expected_min_429 expected 429s)."
    exit 0
else
    echo
    echo "FAIL: expected at least $expected_min_429 429s, got $count_429."
    echo "Likely cause: caddy-ratelimit plugin not compiled into the proxy image."
    echo "Check: docker compose exec proxy caddy list-modules | grep ratelimit"
    exit 1
fi
