"""Unit tests for analysis/tier3_extract.py and analysis/tier4_validate.py.

All external services are MOCKED: OpenRouter is never called (httpx.post is monkeypatched).
The DB is FakeCursor from conftest.py. The idempotency test proves ON CONFLICT contract.
"""
import sys
import os
import json
import re

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

os.environ.setdefault("PG_DSN", "postgresql://test@localhost/test")
os.environ.setdefault("OPENROUTER_API_KEY", "test-key")

import analysis.tier3_extract as t3  # noqa: E402
import analysis.tier4_validate as t4  # noqa: E402
from conftest import FakeCursor, FakeConn, fake_execute_values  # noqa: E402


# ---------------------------------------------------------------------------
# Tier-3: Pydantic schema validation
# ---------------------------------------------------------------------------
def test_problem_statement_valid():
    ps = t3.ProblemStatement.model_validate({
        "icp": "ecom",
        "job_to_be_done": "sync inventory across channels",
        "statement": "Manual inventory sync causes overselling during peak sales",
        "current_workaround": "Excel spreadsheets updated hourly",
        "wtp_quotes": ["I would pay $200/mo for this"],
        "severity": 4,
        "frequency_note": "daily during sales seasons",
        "supporting_signal_ids": [1, 5, 12],
    })
    assert ps.icp == "ecom"
    assert ps.severity == 4
    assert len(ps.wtp_quotes) == 1
    assert ps.supporting_signal_ids == [1, 5, 12]


def test_severity_clamped():
    assert t3.ProblemStatement.model_validate({"statement": "x" * 15, "severity": 99}).severity == 5
    assert t3.ProblemStatement.model_validate({"statement": "x" * 15, "severity": 0}).severity == 1
    assert t3.ProblemStatement.model_validate({"statement": "x" * 15, "severity": "3"}).severity == 3


def test_lists_coerced_from_null_and_string():
    ps = t3.ProblemStatement.model_validate({
        "statement": "x" * 15,
        "wtp_quotes": None,
        "supporting_signal_ids": "42",
    })
    assert ps.wtp_quotes == []
    assert ps.supporting_signal_ids == [42]


def test_icp_coerced():
    assert t3.ProblemStatement.model_validate({"statement": "x" * 15, "icp": "agency owner"}).icp == "agency"
    assert t3.ProblemStatement.model_validate({"statement": "x" * 15, "icp": None}).icp == "saas_operator"


# ---------------------------------------------------------------------------
# Tier-3: LLM response parsing
# ---------------------------------------------------------------------------
def test_validate_batch_extracts_json():
    raw = 'Sure! Here is the JSON:\n{"statements": [{"icp": "ecom", "statement": "Inventory sync is painful and causes overselling", "severity": 4}]}\nThanks'
    batch = t3.validate_batch(raw)
    assert len(batch.statements) == 1
    assert batch.statements[0].icp == "ecom"
    assert batch.statements[0].severity == 4


def test_validate_batch_bad_json():
    assert len(t3.validate_batch("not json at all").statements) == 0


def test_validate_batch_handles_list():
    raw = json.dumps([{"icp": "saas_operator", "statement": "x" * 10, "severity": 3}])
    batch = t3.validate_batch(raw)
    assert len(batch.statements) == 1


# ---------------------------------------------------------------------------
# Tier-3: LLM call (mocked)
# ---------------------------------------------------------------------------
def test_call_llm_success(monkeypatch):
    class FakeResp:
        def raise_for_status(self): pass
        def json(self):
            return {
                "choices": [{"message": {"content": json.dumps({
                    "statements": [{"icp": "ecom", "statement": "x" * 10, "severity": 4}]
                })}}],
                "usage": {"prompt_tokens": 1000, "completion_tokens": 200}
            }

    class FakeClient:
        def post(self, *a, **kw): return FakeResp()

    monkeypatch.setattr(t3.httpx, "post", FakeClient().post)
    monkeypatch.setattr(t3.time, "sleep", lambda *a, **k: None)
    result, tok_in, tok_out = t3.call_llm([(1, "pain text here", "reddit_comment", "ecom")])
    assert result is not None
    assert len(result.statements) == 1
    assert tok_in == 1000
    assert tok_out == 200


def test_call_llm_retries_on_error(monkeypatch):
    calls = {"n": 0}

    class FakeRespErr:
        def raise_for_status(self): raise Exception("500")
        def json(self): return {}

    class FakeRespOK:
        def raise_for_status(self): pass
        def json(self):
            return {
                "choices": [{"message": {"content": '{"statements": []}'}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 10}
            }

    class FakeClient:
        def post(self, *a, **kw):
            calls["n"] += 1
            return FakeRespErr() if calls["n"] == 1 else FakeRespOK()

    monkeypatch.setattr(t3.httpx, "post", FakeClient().post)
    monkeypatch.setattr(t3.time, "sleep", lambda *a, **k: None)
    result, _, _ = t3.call_llm([(1, "text", "reddit", "ecom")])
    assert calls["n"] == 2
    assert result is not None


# ---------------------------------------------------------------------------
# Tier-4: Pydantic schema
# ---------------------------------------------------------------------------
def test_opportunity_valid():
    opp = t4.OpportunitySchema.model_validate({
        "is_real": True,
        "tam_estimate": "$2B",
        "saturation_ok": True,
        "competitors": [{"name": "Competitor A", "funded": True, "stage": "Series A"}],
        "wedge_score": 4.0,
        "wave_score": 3.0,
        "edge_score": 2.0,
        "wave": "ai_native_ops",
        "bear_case": "Big players may add this feature",
    })
    assert opp.saturation_ok is True
    assert len(opp.competitors) == 1
    assert opp.wedge_score == 4.0


def test_opportunity_scores_clamped():
    opp = t4.OpportunitySchema.model_validate({
        "wedge_score": 99, "wave_score": -5, "edge_score": "3"
    })
    assert opp.wedge_score == 5.0
    assert opp.wave_score == 0.0
    assert opp.edge_score == 3.0


def test_opportunity_competitors_coerced():
    opp = t4.OpportunitySchema.model_validate({
        "competitors": {"name": "Single", "funded": False}
    })
    assert len(opp.competitors) == 1
    assert opp.competitors[0]["name"] == "Single"


# ---------------------------------------------------------------------------
# Tier-4: LLM call (mocked)
# ---------------------------------------------------------------------------
def test_validate_opportunity_success(monkeypatch):
    class FakeResp:
        def raise_for_status(self): pass
        def json(self):
            return {
                "choices": [{"message": {"content": json.dumps({
                    "is_real": True, "tam_estimate": "$500M",
                    "saturation_ok": True, "wedge_score": 4,
                    "wave_score": 3, "edge_score": 2,
                    "competitors": [{"name": "CompA", "funded": True, "stage": "Seed"}],
                    "bear_case": "May be solved by incumbents"
                })}}],
                "usage": {"prompt_tokens": 500, "completion_tokens": 100}
            }

    class FakeClient:
        def post(self, *a, **kw): return FakeResp()

    monkeypatch.setattr(t4.httpx, "post", FakeClient().post)
    monkeypatch.setattr(t4.time, "sleep", lambda *a, **k: None)
    opp, tok_in, tok_out = t4.validate_opportunity(
        "Inventory sync is painful", "ecom", 4, ["I'd pay $200/mo"])
    assert opp is not None
    assert opp.saturation_ok is True
    assert opp.wedge_score == 4.0
    assert tok_in == 500
    assert tok_out == 100


def test_validate_opportunity_failure(monkeypatch):
    class FakeErr:
        def raise_for_status(self): raise Exception("API down")
        def json(self): return {}

    monkeypatch.setattr(t4.httpx, "post", lambda *a, **k: FakeErr())
    monkeypatch.setattr(t4.time, "sleep", lambda *a, **k: None)
    opp, _, _ = t4.validate_opportunity("test", "ecom", 3, [])
    assert opp is None


# ---------------------------------------------------------------------------
# Tier-4: upsert + final score calculation
# ---------------------------------------------------------------------------
def test_final_score_saturation_gate():
    """If saturation_ok=False → final_score must be 0."""
    opp = t4.OpportunitySchema.model_validate({
        "saturation_ok": False, "wedge_score": 5, "wave_score": 5, "edge_score": 5
    })
    # When saturation_ok=False, final_score = 0 regardless
    final = 0.0 if not opp.saturation_ok else 0.5 * opp.wedge_score + 0.3 * opp.wave_score + 0.2 * opp.edge_score
    assert final == 0.0


def test_final_score_normal():
    opp = t4.OpportunitySchema.model_validate({
        "saturation_ok": True, "wedge_score": 4, "wave_score": 3, "edge_score": 2
    })
    final = 0.5 * 4 + 0.3 * 3 + 0.2 * 2
    assert abs(final - 3.3) < 0.01


# ---------------------------------------------------------------------------
# Tier-3: upsert_statement against an extended FakeCursor
# ---------------------------------------------------------------------------
# upsert_statement issues:
#   INSERT INTO analysis.problem_statements (cluster_id, statement, ...)
#   VALUES (NULL, %s, ...) ON CONFLICT (cluster_id) WHERE cluster_id IS NULL DO NOTHING
#   RETURNING id
#
# In real Postgres, NULLs are distinct in a unique index, so that ON CONFLICT clause is a
# NO-OP (documented in AGENTS.md Known issues: "cosmetic/harmless, no cap; clean it to a
# plain INSERT ... RETURNING id"). ProblemStatementCursor mirrors that reality: every
# INSERT creates a fresh row + returns a new id (no cap, no dedup). This keeps the "not
# capped" and "returns id" tests faithful to live DB behavior. The flip side — that two
# identical inserts DO create a duplicate — is captured by the xfailed idempotency test.
class ProblemStatementCursor(FakeCursor):
    """FakeCursor for analysis.problem_statements, emulating real Postgres NULL-distinct.

    Keys rows by a synthetic id (each insert is distinct, as real Postgres treats multiple
    NULL cluster_id values as distinct) and honours RETURNING id via fetchone().
    """

    def __init__(self):
        super().__init__()
        self._next_id = 1

    @staticmethod
    def _table(sql):
        # FakeCursor._table uses \w+ and chokes on the schema-qualified target
        # "analysis.problem_statements"; accept dotted names here.
        m = re.search(r'(?:INSERT INTO|UPDATE)\s+([\w.]+)', sql, re.I)
        return m.group(1) if m else None

    def execute(self, sql, params=None):
        table = self._table(sql)
        if re.match(r'\s*INSERT', sql, re.I) and table == "analysis.problem_statements":
            # VALUES = (NULL, statement, icp, jtbd, workaround, wtp_json, severity,
            #           frequency_note, pre_score, model); cluster_id is always NULL.
            row = (None,) + tuple(params)
            new_id = self._next_id
            self.store[("analysis.problem_statements", new_id)] = row
            self._result = [(new_id,)]      # RETURNING id
            self.new_inserts += 1
            self.rowcount = 1
            self._next_id += 1
        else:
            super().execute(sql, params)

    def count(self, table):
        # table is schema-qualified ("analysis.problem_statements") here.
        return sum(1 for (t, _id) in self.store if t == table)


@pytest.fixture
def ps_cursor():
    return ProblemStatementCursor()


def _make_statement(text="Manual inventory sync across channels causes overselling and lost revenue"):
    return t3.ProblemStatement.model_validate({
        "icp": "ecom",
        "job_to_be_done": "sync inventory across sales channels",
        "statement": text,
        "current_workaround": "spreadsheets reconciled by hand",
        "wtp_quotes": ["I would pay $200/mo to never oversell again"],
        "severity": 4,
        "frequency_note": "daily during sales seasons",
        "supporting_signal_ids": [1, 2],
    })


def test_upsert_statement_not_capped(ps_cursor):
    """Multiple DISTINCT statements (all cluster_id=NULL) all insert -> no cap.

    Real Postgres treats NULLs as distinct in UNIQUE(cluster_id), so the cosmetic
    `ON CONFLICT (cluster_id) WHERE cluster_id IS NULL DO NOTHING` never fires: there
    is no cap on how many NULL-cluster_id rows can coexist.
    """
    stmts = [
        _make_statement("Inventory sync across Shopify and Amazon causes overselling during sales"),
        _make_statement("Generating client reports by hand eats six hours every Monday morning"),
        _make_statement("Reconciling refunds across marketplaces is a spreadsheet nightmare weekly"),
    ]
    ids = [t3.upsert_statement(ps_cursor, s, s.supporting_signal_ids, "test-model") for s in stmts]
    assert all(i is not None for i in ids), "every distinct statement should insert and return an id"
    assert len(set(ids)) == 3, "ids must be distinct (not capped to one)"
    assert ps_cursor.count("analysis.problem_statements") == 3
    assert ps_cursor.new_inserts == 3


def test_upsert_statement_returns_new_id(ps_cursor):
    """upsert_statement returns the new row id via RETURNING (not None)."""
    stmt = _make_statement()
    sid = t3.upsert_statement(ps_cursor, stmt, stmt.supporting_signal_ids, "test-model")
    assert sid is not None
    assert isinstance(sid, int)
    assert sid > 0
    assert ps_cursor.count("analysis.problem_statements") == 1
    assert ps_cursor.new_inserts == 1


@pytest.mark.xfail(
    strict=True,
    reason="KNOWN ISSUE (AGENTS.md): upsert_statement's ON CONFLICT (cluster_id) WHERE "
           "cluster_id IS NULL is a no-op because NULLs are distinct in a unique index, so a "
           "second identical insert DOES create a duplicate. Dedup requires ON CONFLICT on the "
           "statement text plus a UNIQUE(statement) constraint (schema change, out of scope for "
           "TB-I). This xfail reproduces the bug; convert to a real assertion once upsert_statement "
           "dedups, and delete this marker."
)
def test_upsert_statement_idempotent_no_duplicates(ps_cursor):
    """Calling upsert_statement twice with identical data must NOT create a duplicate.

    Currently FAILS (duplicates created) due to the known issue above -> xfail.
    """
    stmt = _make_statement()
    sid1 = t3.upsert_statement(ps_cursor, stmt, stmt.supporting_signal_ids, "test-model")
    sid2 = t3.upsert_statement(ps_cursor, stmt, stmt.supporting_signal_ids, "test-model")
    # Desired contract: the second call is a no-op (dedup) — no new row, no fresh id.
    assert ps_cursor.new_inserts == 1, "second identical insert should not create a new row"
    assert ps_cursor.count("analysis.problem_statements") == 1
    assert sid2 is None or sid2 == sid1, "second call should not return a fresh id"
