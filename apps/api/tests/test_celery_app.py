"""`app.tasks.celery_app` -- two gaps this deploy found, both live.

1. TLS. `redis.Redis.from_url()` (routes/health.py) infers TLS from a
   `rediss://` scheme with no further configuration. Kombu, Celery's own
   broker/backend client, does not: it refuses a `rediss://` connection
   outright unless a certificate policy is stated explicitly, and it
   refuses at the first real connection -- `chain.apply_async()` inside
   `complete_upload` -- not at import or at `Celery()` construction. So
   `/ready` reported Redis healthy while every upload still 500'd with
   nothing in the response naming Celery, Kombu, or TLS.

2. The Upstash command-budget setting. `docs/runbooks/deploy-free-tier.md`
   told every reader to pass `--broker-transport-options` on the `celery
   worker` command line. That flag has never existed there -- it is an
   application-config setting -- so the documented command failed at
   argument parsing before the worker so much as tried to connect:
   `Error: No such option '--broker-transport-options'`.

Both found deploying against Upstash.
"""
import importlib
import ssl

from app.tasks.celery_app import BROKER_TRANSPORT_OPTIONS, _tls_options


def test_a_plain_redis_url_gets_no_ssl_options():
    assert _tls_options("redis://localhost:6379/0") is None


def test_a_rediss_url_requires_certificate_verification():
    assert _tls_options("rediss://default:pw@example.upstash.io:6379") == {
        "ssl_cert_reqs": ssl.CERT_REQUIRED,
    }


def test_the_running_celery_app_carries_the_options_its_own_broker_needs(monkeypatch):
    """Not just that the helper computes the right dict -- that the app
    built from `settings.redis_url` actually carries it. A helper that is
    correct but never wired to `celery_app.conf` fails exactly as silently
    as no helper at all.

    Mutates the field on `get_settings()`'s already-cached singleton rather
    than clearing the `lru_cache` -- clearing it makes the next
    `get_settings()` call (including the reloaded module's own) construct a
    brand new `Settings()` from the real environment, which would silently
    discard the patch instead of exercising it.
    """
    import app.tasks.celery_app as celery_app_module
    from app.core.config import get_settings

    monkeypatch.setattr(
        get_settings(), "redis_url", "rediss://default:pw@example.upstash.io:6379", raising=False
    )
    try:
        importlib.reload(celery_app_module)
        assert celery_app_module.celery_app.conf.broker_use_ssl == {"ssl_cert_reqs": ssl.CERT_REQUIRED}
        assert celery_app_module.celery_app.conf.redis_backend_use_ssl == {"ssl_cert_reqs": ssl.CERT_REQUIRED}
    finally:
        # `monkeypatch`'s own teardown only reverts the attribute *after*
        # this test returns, which is too late to matter to a module-level
        # singleton: `celery_app` reloaded here would keep carrying the
        # fake URL's config for every later test that imports it, since
        # nothing re-runs this module just because a setting changed back.
        # `.undo()` reverts immediately, so the reload right after it
        # rebuilds `celery_app` against the real `redis_url` again.
        monkeypatch.undo()
        importlib.reload(celery_app_module)


def test_the_broker_transport_options_actually_reach_celery_conf():
    """Not a CLI flag -- the fix for the invalid `--broker-transport-options`
    argument is to set this in `celery_app.conf` directly, so this checks the
    thing that is actually load-bearing: the running app's own config, not
    just the constant existing somewhere in the module.
    """
    from app.tasks.celery_app import celery_app

    assert celery_app.conf.broker_transport_options == BROKER_TRANSPORT_OPTIONS
    assert BROKER_TRANSPORT_OPTIONS["brpop_timeout"] == 30  # the number the runbook's arithmetic depends on


def test_a_local_dev_broker_carries_no_ssl_options():
    """The default `redis://localhost:6379/0` (docker-compose, local dev)
    must come through this unchanged -- broker_use_ssl=None is Celery's own
    default, not a new behaviour for the path nobody is debugging right now.
    """
    from app.tasks.celery_app import celery_app

    # Whatever this test run's actual settings.redis_url is: if it is a
    # plain redis:// URL (true for every non-Drive test run), the options
    # must be unset.
    from app.core.config import get_settings

    if get_settings().redis_url.startswith("redis://"):
        assert celery_app.conf.broker_use_ssl is None
        assert celery_app.conf.redis_backend_use_ssl is None
