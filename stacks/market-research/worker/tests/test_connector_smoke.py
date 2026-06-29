"""Smoke tests for every connector module: import succeeds, run() exists, PG_DSN is wired.

These are SMOKE tests only — they prove each connector is importable (no missing deps, no
import-time crashes) and structurally complete (a runnable entrypoint + a Postgres DSN). They
do NOT hit the network or the DB. Two connector shapes are supported:
  - module-level `def run()` (appstore, google_play, saas_reviews, saashub, funding, ...)
  - a `class X(Connector)` subclass whose `run()` is inherited from connectors.base.Connector
    (hackernews, news_rss, producthunt, stackexchange, ycombinator, ...)

DRIFT NOTE: connectors/saashub.py, shopify_reviews.py, and funding.py exist on the devcore
deploy dir (/opt/market-research/worker) but are NOT yet committed to this git repo (see
AGENTS.md Known issues — schema/code drift). They are real module-level connectors. This test
detects module presence dynamically: on the server all 17 are smoke-tested; from a pure git
checkout the 3 drifted modules are skipped with a clear reason (and will be covered
automatically once TB-A rescues them into the repo).
"""
import importlib
import importlib.util
import inspect
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Several connectors read PG_DSN at import time (module-level `PG_DSN = os.environ["PG_DSN"]`).
# Set a placeholder so import doesn't KeyError; the smoke test never connects.
os.environ.setdefault("PG_DSN", "postgresql://test@localhost/test")
os.environ.setdefault("OPENROUTER_API_KEY", "test-key")

from connectors.base import Connector  # noqa: E402

# The TB-I connector list. Names map 1:1 to connectors/<name>.py modules.
CONNECTORS = [
    "arctic_reddit",
    "appstore",
    "google_play",
    "shopify_appstore",
    "wordpress",
    "atlassian_marketplace",
    "devto",
    "chrome_webstore",
    "hackernews",
    "ycombinator",
    "stackexchange",
    "producthunt",
    "news_rss",
    "saas_reviews",
    "saashub",
    "shopify_reviews",
    "funding",
]


def _is_connector_subclass(obj):
    return inspect.isclass(obj) and issubclass(obj, Connector) and obj is not Connector


def _module_has_run(mod):
    """True if the module has a module-level run() OR a Connector subclass (inherits run)."""
    if hasattr(mod, "run") and callable(mod.run):
        return True
    return any(_is_connector_subclass(v) for v in vars(mod).values())


def _module_reads_pg_dsn(mod):
    """True if the module references PG_DSN directly, or via a base.Connector subclass.

    Module-level connectors do `PG_DSN = os.environ["PG_DSN"]`; class-based connectors
    inherit db() from connectors.base.Connector, which reads base.PG_DSN.
    """
    src = inspect.getsource(mod)
    if "PG_DSN" in src:
        return True
    # class-based: inherits db() from base.Connector, which uses base.PG_DSN
    return any(_is_connector_subclass(v) for v in vars(mod).values())


@pytest.mark.parametrize("name", CONNECTORS)
def test_connector_smoke(name):
    """Each connector imports cleanly, has a runnable entrypoint, and reads PG_DSN."""
    # Detect module presence rather than hardcoding: the 3 drifted modules exist on the
    # server but not in a pure git checkout. Missing module -> skip (drift, not a failure).
    # A present-but-broken import (real regression) -> ImportError -> test fails, as intended.
    if importlib.util.find_spec(f"connectors.{name}") is None:
        pytest.skip(
            f"connectors/{name}.py not present in this environment "
            f"(drift: module is on devcore but not committed to git — see AGENTS.md)"
        )

    mod = importlib.import_module(f"connectors.{name}")
    assert mod is not None, f"connectors.{name} did not import"

    assert _module_has_run(mod), (
        f"{name} has no module-level run() and no Connector subclass — missing entrypoint"
    )

    assert _module_reads_pg_dsn(mod), (
        f"{name} does not reference PG_DSN (directly or via base.Connector)"
    )
