# The edge: TLS terminator, static host for the built PWA, and reverse
# proxy to the API — one container, because they are one origin.
#
# Why one origin rather than a static host (Netlify/S3+CDN) plus a separate
# api.remedy... hostname (Phase 5.1, decision 0036):
#
# 1. **The refresh cookie.** decision 0024 put the refresh token in an
#    httpOnly cookie. Split origins make that cookie cross-site, which
#    forces `SameSite=None` — a strictly weaker cookie, sent on every
#    cross-site request, and the one setting CSRF defences exist to
#    compensate for. Same-origin keeps `REFRESH_COOKIE_SAMESITE=lax`
#    working as written, and app/core/config.py's own comment ("paired
#    with samesite=none only if the API and client are cross-site") is
#    the instruction being followed here.
# 2. **CORS stops being load-bearing.** Same-origin requests are not
#    preflighted at all, so a misconfigured CORS_ALLOW_ORIGINS cannot
#    silently break the clinic — which is the failure mode main.py's own
#    comment calls out as invisible in the API log.
# 3. **The service worker.** vite.config.ts sets
#    `navigateFallbackDenylist: [/^\/api\//]`. That rule only means
#    anything if `/api/*` is on the same origin as the service worker's
#    scope — on split origins the SW could never intercept API calls and
#    the denylist would be decorative. The existing PWA config already
#    assumes this topology.
# 4. One certificate, one TLS policy, and exactly one place that sets
#    `X-Forwarded-Proto` and `X-Forwarded-For` (infra/caddy/Caddyfile).
#
# The cost, stated: no CDN. For one clinic on its own LAN or a local ISP
# link, a CDN optimises the wrong thing — the assets are precached by the
# service worker after the first load anyway (P0-2), so the second visit
# never fetches them regardless of where they are served from.

# --- build ------------------------------------------------------------
FROM node:22-alpine AS build

WORKDIR /app

# `npm ci`, not `npm install`: it installs exactly package-lock.json and
# fails if the lock and the manifest disagree. A deploy is the wrong place
# to silently resolve a new transitive version of anything.
COPY package.json package-lock.json ./
RUN npm ci

COPY . .

# Same-origin API. openapi-fetch strips a trailing slash from baseUrl, so
# "/" becomes "" and every request is issued as a relative path against
# whatever origin served the app — which is the point of the topology
# above. `/` rather than the empty string because an empty environment
# variable is easy for a shell, a compose file or Vite's own env loading
# to turn into "unset", which would fall back to
# src/api/client.ts's `http://localhost:8000` default and produce an app
# that builds cleanly and cannot reach its API.
ARG VITE_API_BASE_URL=/
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL

# `npm run build` is `tsc -b && vite build` — the typecheck is part of the
# build on purpose. An image that compiles is a weaker guarantee than an
# image that typechecks against the API's generated schema (src/api/schema.d.ts),
# and this is the last point where a client/server shape mismatch is
# cheaper to find than in a clinic.
RUN npm run build

# --- serve ------------------------------------------------------------
FROM caddy:2-alpine

# Caddy rather than nginx, and this is most of why: automatic ACME
# certificates with automatic renewal, an http->https redirect, a TLS 1.2
# floor and a modern cipher list are Caddy's *defaults*. Those are four of
# the five items docs/runbooks/key-rotation.md §"What Phase 5 still owes"
# lists, and with nginx each one is explicit config plus a certbot timer
# that nobody notices has stopped until a certificate expires.
#
# The Caddyfile is NOT baked in. It is mounted read-only from
# infra/caddy/Caddyfile (see infra/docker-compose.prod.yml), because edge
# policy — TLS floor, forwarded headers, the domain — is deployment
# configuration, and changing a cipher list should not require rebuilding
# and re-typechecking the frontend.
COPY --from=build /app/dist /srv

EXPOSE 80 443
