# Remedy Scribe API. One image, three roles: the web process, the Celery
# worker, and Celery beat all run from here and differ only in their
# `command` (infra/docker-compose.prod.yml). That is deliberate — a worker
# built from a different image than the API is a worker that can be a
# migration behind it, and this app's tasks write PHI columns through the
# same models the API reads.

FROM python:3.12-slim

# Phase 5.1. Unbuffered so a crash's last log lines actually leave the
# container: Python line-buffers stdout when it is a pipe, and `docker
# logs` on a process that died mid-buffer shows silence where the cause
# was. `PYTHONDONTWRITEBYTECODE` because the source layer is read-only in
# practice and .pyc files only add image weight.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Requirements before source, so a code change does not re-run the
# install. `cryptography` and `psycopg[binary]` ship wheels for this
# platform, so no compiler toolchain is needed in the image — which is
# also why there is no build stage here: adding gcc to strip it out again
# would be more machinery than the ~0 seconds it saves.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Phase 5.1: not root. The API process needs to read its own source, talk
# to Postgres/Redis/S3, and nothing else — it never writes to the
# filesystem (audio goes straight to object storage via presigned URLs,
# decision 0013; the transcript goes to Postgres). A container escape
# that lands as uid 0 inherits root on the host in the default,
# non-userns-remapped Docker configuration this deploy uses.
#
# Ownership stays with root and the app only reads: a process that cannot
# rewrite its own code cannot be made to persist a change to it.
#
# ⚠️ Celery beat is the exception — the default shelve-based scheduler
# writes `celerybeat-schedule` in its working directory. /var/lib/remedy-beat
# is created here *owned by remedy* so that when a fresh named volume is
# mounted over it (infra/docker-compose.prod.yml), Docker initialises the
# volume from this path and carries the ownership with it. A volume mounted
# onto a root-owned directory comes up root-owned and beat dies on its
# first write, which looks like a Celery bug and is a chown.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin remedy \
    && mkdir -p /var/lib/remedy-beat \
    && chown remedy:remedy /var/lib/remedy-beat
USER remedy

EXPOSE 8000

# NOTE: no `alembic upgrade head` here, and none in an entrypoint script,
# on purpose (checklist 5.1: "run migrations as an explicit deploy step,
# never automatically on boot"). Three processes start from this image; a
# migration on boot means three of them racing to hold the same Alembic
# lock, and an autoscaling or restarting container silently applying a
# schema change nobody triggered. The migration is its own one-shot
# service — see infra/docker-compose.prod.yml's `migrate` and
# infra/deploy.sh step 4.
#
# This CMD is the *development* default (infra/docker-compose.yml).
# Production overrides it with an explicit command that sets worker count
# and proxy-header trust — see infra/docker-compose.prod.yml's `api`, and
# the X-Forwarded-For reasoning in infra/caddy/Caddyfile.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
