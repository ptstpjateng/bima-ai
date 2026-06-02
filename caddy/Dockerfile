# Custom Caddy build with the caddy-ratelimit plugin baked in.
#
# WHY: Caddy core ships without a rate limiter. We need IP-level throttling
# to blunt webhook abuse (Telegram + APTANA fan-out, anyone who finds
# /webhook/* and tries to flood Gemma) and to protect the admin/api surface
# from credential-stuffing while Laravel's throttle:120,1 is the only
# defense in depth (an attacker who skips auth still hits the app).
#
# Plugin: https://github.com/mholt/caddy-ratelimit
#   - In-memory token-bucket per zone-key (we key by client_ip).
#   - No external store needed; container-local state is fine for our
#     single-proxy deploy. If we ever shard Caddy, swap to a distributed
#     store (Redis) by changing the `storage` block in the Caddyfile.
#
# Two-stage build keeps the runtime image close to caddy:2-alpine size
# (xcaddy + Go toolchain only live in the build stage).
#
# Build / run is wired in docker-compose.yml; the `proxy` service now uses
# `build: ./caddy` instead of `image: caddy:2-alpine`.

FROM caddy:2-builder-alpine AS builder

RUN xcaddy build \
    --with github.com/mholt/caddy-ratelimit

FROM caddy:2-alpine

COPY --from=builder /usr/bin/caddy /usr/bin/caddy
