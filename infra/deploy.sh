#!/usr/bin/env bash
#
# Remedy Scribe — production deploy (Phase 5.1, decision 0036).
#
# ⚠️ WRITTEN, SHELLCHECK-CLEAN, NEVER RUN AGAINST A HOST. There is no
# deployment target. Every step below is the procedure; none of it has
# been executed end to end. docs/progress/5.1-deployment.md is explicit
# about which parts were exercised locally and which were not.
#
# Run from the repository root:
#   sops exec-env infra/env/production.enc.env 'infra/deploy.sh'
#
# The `sops exec-env` wrapper is the whole secrets mechanism: it decrypts
# the file, puts the values in this script's environment, and execs. The
# plaintext exists in this process's memory and in the containers it
# starts, and never as a file. See docs/runbooks/secrets-management.md §2.
#
# The one property this script exists to guarantee, over and above being
# convenient: **migrations run as their own gated step, and the
# long-running services are not started until that step has succeeded.**
# Checklist 5.1: "never automatically on boot."

set -euo pipefail

COMPOSE_FILE="infra/docker-compose.prod.yml"

step() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }
fail() { printf '\n\033[31mFAILED: %s\033[0m\n' "$1" >&2; exit 1; }

# ---------------------------------------------------------------------
step "0. Refusing to deploy from a state nobody can identify later"
# ---------------------------------------------------------------------

[[ -f "$COMPOSE_FILE" ]] || fail "run this from the repository root; $COMPOSE_FILE not found"

# infra/.env would be auto-loaded by docker compose for interpolation,
# which silently reintroduces the on-disk secrets file this whole design
# removes — and it would take precedence in a way nobody reviewing
# docker-compose.prod.yml would see. There is no legitimate reason for it
# to exist, so its existence is treated as a finding rather than tidied
# away automatically.
if [[ -f infra/.env ]]; then
  fail "infra/.env exists. Docker Compose auto-loads it for interpolation, which
       puts secrets back on disk (docs/runbooks/secrets-management.md §2).
       Move its contents into infra/env/production.enc.env via sops and
       delete it. Not removing it automatically: if it holds the only copy
       of a secret, deleting it is the incident."
fi

# A deploy of "whatever happened to be in the working tree" cannot be
# rolled back to, reproduced, or matched against an audit-log timestamp
# six months later. The sha becomes the image tag.
if [[ -n "$(git status --porcelain)" ]]; then
  fail "the working tree is dirty. Commit or stash first: the git sha tags the
       images, and an image tagged with a sha it does not contain makes
       'what is running in the clinic?' unanswerable."
fi

REMEDY_RELEASE="$(git rev-parse --short HEAD)"
export REMEDY_RELEASE
echo "release: $REMEDY_RELEASE"

# ---------------------------------------------------------------------
step "1. Rendering the compose file — the first place a missing secret fails"
# ---------------------------------------------------------------------
# Every required value in docker-compose.prod.yml is `${VAR:?message}`, so
# this single command is a complete presence check on the environment sops
# just injected. Cheap, and it happens before anything is built, pushed or
# stopped. Output to /dev/null: a rendered production compose file is a
# document containing every secret in plaintext, and printing it to a
# terminal puts them in a scrollback buffer and probably a shell history.
docker compose -f "$COMPOSE_FILE" config >/dev/null \
  || fail "compose could not render — a required variable is missing (the error above names it)"

# ---------------------------------------------------------------------
step "2. Building images"
# ---------------------------------------------------------------------
# Both images, tagged with the release. The web build runs `tsc -b` as part
# of `npm run build` (apps/web/Dockerfile), so a client/server shape
# mismatch against the API's generated schema fails here rather than in a
# clinic.
docker compose -f "$COMPOSE_FILE" build

# ---------------------------------------------------------------------
step "3. Migration dry-check: what would this apply?"
# ---------------------------------------------------------------------
# Printed *before* applying, so the person deploying sees the schema change
# they are about to make to a database holding real patient records. This
# is also the step that catches the deploy nobody meant to make: an empty
# diff here on a release that was supposed to add a column means the
# migration was never committed.
#
# `--profile deploy` is required for the migrate service to exist at all;
# without it compose skips the service entirely, which is exactly the
# mechanism that keeps migrations off the boot path.
docker compose -f "$COMPOSE_FILE" --profile deploy run --rm migrate \
  alembic history --indicate-current || fail "could not read migration history — check DATABASE_URL and TLS"

# ---------------------------------------------------------------------
step "4. Applying migrations — the explicit, gated step"
# ---------------------------------------------------------------------
# ⚠️ TAKE A BACKUP FIRST if this release has a migration that drops or
# rewrites anything. The managed Postgres has PITR, so the recovery point
# is "any second before this ran" — but only if you note the timestamp
# *now*, before the schema changes. docs/runbooks/backup-restore.md.
echo "pre-migration timestamp (your PITR recovery point): $(date -u +%Y-%m-%dT%H:%M:%SZ)"

# A one-shot container from the same image the API will run. Not an
# entrypoint, not a hook, not `depends_on` — a step whose exit code the
# next step is conditional on. `set -e` plus the explicit `||` makes the
# failure loud rather than a line scrolling past.
docker compose -f "$COMPOSE_FILE" --profile deploy run --rm migrate \
  || fail "migration failed. NOTHING HAS BEEN RESTARTED — the old release is still
       serving. Fix forward or restore to the timestamp above; do not start
       the new images against a half-migrated schema."

# ---------------------------------------------------------------------
step "5. Starting the services"
# ---------------------------------------------------------------------
# Plain `up -d`, which is the point: it starts api, worker, beat, redis and
# edge, and — because `migrate` carries a profile — cannot start the
# migration. Anyone who types this command by hand at 2 a.m. gets the same
# guarantee without knowing it exists.
#
# `--remove-orphans` so a service deleted from the compose file actually
# stops, rather than staying up indefinitely against a schema that has
# moved on.
docker compose -f "$COMPOSE_FILE" up -d --remove-orphans

# ---------------------------------------------------------------------
step "6. Verifying — readiness, not 'the containers are up'"
# ---------------------------------------------------------------------
# `docker ps` showing five running containers proves the processes started.
# It does not prove the API can reach Postgres, that the PHI key decrypts
# what is already in the database, or that Caddy got a certificate. The
# readiness endpoint answers the first two; steps 7-8 cover the rest.
#
# Note what a failure here means: Phase 4.1's boot guard refuses to start a
# production process holding a published dev secret, a non-Secure refresh
# cookie or a localhost CORS origin. If the api container is restarting and
# its log says "Refusing to start with this configuration", that is the
# guard working exactly as designed — read the list it prints, fix the
# secret, redeploy. It is not a bug in the deploy.
deadline=$((SECONDS + 120))
until docker compose -f "$COMPOSE_FILE" exec -T api \
        python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/ready',timeout=5).status==200 else 1)" 2>/dev/null; do
  if (( SECONDS > deadline )); then
    echo "--- api log ---" >&2
    docker compose -f "$COMPOSE_FILE" logs --tail=50 api >&2
    fail "/ready did not return 200 within 120s. The body names which dependency
       failed; the reason is in the log above (never in the response — see
       app/api/routes/health.py)."
  fi
  sleep 3
done
echo "readiness: ok"

# ---------------------------------------------------------------------
step "7. Post-deploy checks a script should NOT pretend to do for you"
# ---------------------------------------------------------------------
cat <<'CHECKS'
The deploy is up. These are not automated, on purpose — each one needs a
human to look at the result, and a green tick from a script that checked
the wrong thing is worse than an unchecked box.

  1. TLS, from outside the VM:
       curl -sSI https://$REMEDY_PUBLIC_DOMAIN/health
     Expect HTTP/2 200, `strict-transport-security`, and a certificate
     chain your browser trusts. Then confirm the redirect exists:
       curl -sSI http://$REMEDY_PUBLIC_DOMAIN/ | head -1     # 301
     And that the TLS floor is real:
       nmap --script ssl-enum-ciphers -p 443 $REMEDY_PUBLIC_DOMAIN
     Expect no TLSv1.0/1.1. Caddy's defaults should give you this; the
     point of checking is that "should" is not "does".

  2. X-Forwarded-Proto cannot be set by a client. Send one and confirm it
     changes nothing:
       curl -sSI https://$REMEDY_PUBLIC_DOMAIN/health -H 'X-Forwarded-Proto: http'
     HSTS must still be present. (In production the app emits HSTS
     regardless, so this checks the proxy, not the app.)

  3. The per-IP login rate limit is per *IP*, not per clinic. Fail a login
     five times from one machine, then confirm a second machine can still
     log in. If it cannot, `--proxy-headers` / `--forwarded-allow-ips` is
     wrong and every doctor shares one bucket — see infra/caddy/Caddyfile.

  4. One beat process, exactly:
       docker compose -f infra/docker-compose.prod.yml ps beat
     Two would double-fire the hourly retention purge, which deletes PHI.

  5. A real upload round trip, which is the only thing that proves object
     storage works — readiness deliberately does not check it
     (app/api/routes/health.py explains why). Record a short consultation
     in the browser, confirm the object lands in the bucket, and confirm
     the note generates.

  6. The PHI key decrypts what was already there. Open a patient created
     before this deploy. A wrong key gives `InvalidToken`, not an empty
     field — and it would have written new rows under the wrong key by the
     time you notice. Confirm the fingerprint matches what you expect:
       docker compose -f infra/docker-compose.prod.yml exec -T api python -c \
         "from app.core.config import get_settings, secret_fingerprint as f; print(f(get_settings().phi_encryption_key or ''))"

  7. Note the release in the deploy log, with the pre-migration timestamp
     from step 4. That pair is your rollback target.
CHECKS
