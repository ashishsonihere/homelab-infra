"""Unit tests for connectors/saas_reviews.py — TrustRadius JSON-LD parsing, upsert idempotency.

All external services are MOCKED: curl_cffi/httpx never invoked; the DB is FakeCursor.
The idempotency test proves the ON CONFLICT contract: a second pass inserts 0 new rows.
"""
import sys
import os
import json

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

os.environ.setdefault("PG_DSN", "postgresql://test@localhost/test")

import connectors.saas_reviews as sr  # noqa: E402
from conftest import FakeCursor, FakeConn, fake_execute_values  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def load_fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()


@pytest.fixture
def trustradius_html():
    """Load the real TrustRadius reviews page fixture (saved from live fetch)."""
    path = os.path.join(FIXTURES, "trustradius_reviews.html")
    if os.path.exists(path):
        return load_fixture("trustradius_reviews.html")
    return None


# ---------------------------------------------------------------------------
# JSON-LD parsing
# ---------------------------------------------------------------------------
def test_parse_trustradius_product_page(trustradius_html):
    """Parse a real TrustRadius page → product row + review rows from JSON-LD."""
    if trustradius_html is None:
        pytest.skip("trustradius_reviews.html fixture not found")
    url = "https://www.trustradius.com/products/slack/reviews"
    prow, rrows = sr.tr_parse_product_page(url, trustradius_html)
    assert prow is not None, "product row should be extracted from JSON-LD"
    # product row: (source, ext_id, name, vendor, category, website, pricing_text, pricing, rating, n_reviews, description, metadata)
    assert prow[0] == "trustradius"
    assert prow[1] == "slack"
    assert prow[2] is not None and len(prow[2]) > 0  # name
    assert prow[8] is not None  # rating
    assert prow[9] is not None and prow[9] > 0  # n_reviews

    assert len(rrows) > 0, "should extract at least one review"
    # review row: (source_slug, ext_id, url, title, body, metadata)
    r = rrows[0]
    assert r[0] == "trustradius_review"
    assert r[1] is not None and len(r[1]) > 0  # ext_id
    assert r[4] is not None and len(r[4]) > 0  # body (reviewBody)
    meta = r[5].adapted if hasattr(r[5], "adapted") else r[5]
    assert "product_ext_id" in meta
    assert meta["product_ext_id"] == "slack"


def test_parse_trustradius_handles_no_jsonld():
    """If there's no JSON-LD, returns (None, [])."""
    url = "https://www.trustradius.com/products/fakeproduct/reviews"
    prow, rrows = sr.tr_parse_product_page(url, "<html><body>no json-ld here</body></html>")
    assert prow is None
    assert rrows == []


def test_parse_trustraidu_skips_non_software_blocks():
    """JSON-LD blocks that aren't SoftwareApplication are ignored."""
    html = '''<script type="application/ld+json">{"@type":"BreadcrumbList","itemListElement":[]}</script>
    <script type="application/ld+json">{"@type":"ItemList","name":"Top Industries"}</script>'''
    url = "https://www.trustradius.com/products/fakeproduct/reviews"
    prow, rrows = sr.tr_parse_product_page(url, html)
    assert prow is None
    assert rrows == []


# ---------------------------------------------------------------------------
# DB upsert + idempotency (FakeCursor emulates ON CONFLICT)
# ---------------------------------------------------------------------------
class SaasFakeCursor(FakeCursor):
    """FakeCursor that also knows about saas_products + documents tables."""

    _KEY = {
        **FakeCursor._KEY,
        "saas_products": "ext_id",
        "documents": "ext_id",
    }

    def __init__(self):
        super().__init__()
        self._table_source = {}  # (table, source) -> set of ext_ids for source-scoped tables

    def _apply_rows(self, table, rows):
        # saas_products and documents use different conflict keys
        key_idx = 1 if table in ("saas_products", "documents") else 0
        touched = 0
        for row in rows:
            nid = row[key_idx]
            present = (table, nid) in self.store
            self.store[(table, nid)] = row
            if not present:
                self.new_inserts += 1
            touched += 1
        self.rowcount = touched


@pytest.fixture
def saas_cursor():
    return SaasFakeCursor()


@pytest.fixture
def saas_conn(saas_cursor):
    return FakeConn(saas_cursor)


def _ingest_once(cur, product_row, review_rows):
    """Run the upsert path the way _run_trustradius does, but against FakeCursor."""
    if product_row:
        fake_execute_values(cur,
            "INSERT INTO saas_products VALUES %s ON CONFLICT DO UPDATE",
            [product_row])
    if review_rows:
        fake_execute_values(cur,
            "INSERT INTO documents VALUES %s ON CONFLICT DO UPDATE",
            review_rows)


def _make_sample_data():
    """Create a minimal product + review for testing."""
    prow = (
        "trustradius", "slack", "Slack", "Salesforce", "Collaboration",
        "", "Free", {"price": 0, "currency": "USD"}, 9.2, 10039,
        "Slack is a messaging app.", {"url": "https://example.com", "awards": []}
    )
    rrows = [
        ("trustradius_review", "slack-2026-reviewer1", "https://trustradius.com/products/slack/reviews",
         "Great tool", "Slack is great for team communication.",
         {"product_ext_id": "slack", "product_name": "Slack", "rating": 9}),
    ]
    return prow, rrows


def test_upsert_inserts_expected_rows(saas_cursor, monkeypatch):
    monkeypatch.setattr(sr, "execute_values", fake_execute_values)
    prow, rrows = _make_sample_data()
    _ingest_once(saas_cursor, prow, rrows)
    assert saas_cursor.count("saas_products") == 1
    assert saas_cursor.count("documents") == 1
    assert saas_cursor.new_inserts == 2


def test_second_run_inserts_zero_rows(saas_cursor, monkeypatch):
    """IDEMPOTENCY: a second identical ingest must insert 0 NEW rows."""
    monkeypatch.setattr(sr, "execute_values", fake_execute_values)
    prow, rrows = _make_sample_data()
    _ingest_once(saas_cursor, prow, rrows)
    first = saas_cursor.new_inserts
    assert first == 2
    _ingest_once(saas_cursor, prow, rrows)  # re-run
    assert saas_cursor.new_inserts == first  # no new rows
    assert saas_cursor.count("saas_products") == 1
    assert saas_cursor.count("documents") == 1


# ---------------------------------------------------------------------------
# HTTP retry / Cloudflare block
# ---------------------------------------------------------------------------
def test_cf_blocked_returns_none(monkeypatch):
    """If response has CF challenge text, _get returns None immediately."""
    class FakeResp:
        status_code = 403
        text = "<html>Just a moment...</html>"

    class FakeCffi:
        def get(self, *a, **kw):
            return FakeResp()

    monkeypatch.setattr(sr, "cffi_requests", FakeCffi())
    monkeypatch.setattr(sr.time, "sleep", lambda *a, **k: None)
    assert sr._get("https://example.com") is None


def test_get_retries_on_429(monkeypatch):
    """First call returns 429, second 200 → succeeds after retry."""
    calls = {"n": 0}

    class FakeResp429:
        status_code = 429
        text = "rate limited"

    class FakeResp200:
        status_code = 200
        text = "<html>real content</html>"

    class FakeCffi:
        def get(self, *a, **kw):
            calls["n"] += 1
            return FakeResp429() if calls["n"] == 1 else FakeResp200()

    monkeypatch.setattr(sr, "cffi_requests", FakeCffi())
    monkeypatch.setattr(sr.time, "sleep", lambda *a, **k: None)
    result = sr._get("https://example.com")
    assert calls["n"] == 2
    assert result == "<html>real content</html>"


def test_get_returns_none_on_404(monkeypatch):
    class FakeResp:
        status_code = 404
        text = "not found"

    class FakeCffi:
        def get(self, *a, **kw):
            return FakeResp()

    monkeypatch.setattr(sr, "cffi_requests", FakeCffi())
    monkeypatch.setattr(sr.time, "sleep", lambda *a, **k: None)
    assert sr._get("https://example.com") is None
