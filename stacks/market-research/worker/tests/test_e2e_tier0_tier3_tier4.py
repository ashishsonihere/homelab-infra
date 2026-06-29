"""End-to-end funnel test: pain_signals (mocked Tier-0) -> Tier-3 -> Tier-4 against REAL Postgres.

Guarded by RUN_INTEGRATION=1: skipped unless explicitly opted in (needs a live DB + worker.env).
The LLM is MOCKED (httpx.post) -> no OpenRouter calls, no cost. The DB is real, exercising the
actual INSERT ... ON CONFLICT ... RETURNING upserts, FK constraints, and JSONB columns.

All writes happen in ONE uncommitted transaction on a single connection that is rolled back at the
end -> nothing persists, no cleanup needed, no pollution of the live 10.3M-row tables. This is the
gap the FakeCursor unit tests cannot close: they prove the SQL shape, this proves the real DB
accepts it (types, constraints, JSONB adaptation, RETURNING ids).

Funnel exercised:
  Tier-0  MOCKED   - we INSERT a few test pain_signals directly (source='e2e_tbi') to simulate
                     Tier-0's output, skipping the 10M-row lexical filter (heavy + disruptive).
  Tier-3  MOCKED LLM + REAL DB upsert
                   - t3.call_llm(httpx mocked) -> validate_batch -> ProblemStatement[]
                   - t3.upsert_statement(cur, ...) -> real INSERT ... RETURNING id
  Tier-4  MOCKED LLM + REAL DB upsert
                   - t4.validate_opportunity(httpx mocked) -> OpportunitySchema
                   - t4.upsert_opportunity(cur, ...) -> real INSERT + opportunity_competitors

Run (on devcore):
  docker run --rm --network devcore_net -v /opt/market-research/worker:/app -w /app \
    --env-file /opt/market-research/worker.env -e RUN_INTEGRATION=1 \
    mr-worker:scrape python3 -m pytest tests/test_e2e_tier0_tier3_tier4.py -v
"""
import os
import sys
import json

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# tier3/tier4 read these at import time. In the container, worker.env sets the real values;
# setdefault only fills them for local/standalone imports.
os.environ.setdefault("PG_DSN", "postgresql://devcore@devcore-postgres:5432/market_research")
os.environ.setdefault("OPENROUTER_API_KEY", "test-key")

import psycopg2  # noqa: E402
import analysis.tier3_extract as t3  # noqa: E402
import analysis.tier4_validate as t4  # noqa: E402

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_INTEGRATION") != "1",
    reason="set RUN_INTEGRATION=1 to run the real-DB e2e funnel test (needs live Postgres)",
)

TEST_SOURCE = "e2e_tbi"
TEST_MODEL = "e2e_tbi"


def _tier3_llm_content(signal_ids):
    """Raw LLM response text the mocked httpx.post returns: 2 statements referencing the signals."""
    return json.dumps({
        "statements": [
            {
                "icp": "ecom",
                "job_to_be_done": "sync inventory across sales channels",
                "statement": "Manual inventory sync across Shopify, Amazon and TikTok causes "
                             "overselling during flash sales and loses real money",
                "current_workaround": "Excel spreadsheets reconciled by hand every hour",
                "wtp_quotes": ["I would pay $200/mo to never oversell again"],
                "severity": 4,
                "frequency_note": "daily during peak sale seasons",
                "supporting_signal_ids": signal_ids[:2],
            },
            {
                "icp": "saas_operator",
                "job_to_be_done": "generate weekly client performance reports",
                "statement": "Building client reports by hand from five ad platforms eats six hours "
                             "every single Monday and nobody enjoys doing it",
                "current_workaround": "Google Sheets plus manual CSV exports",
                "wtp_quotes": ["Wish I had a tool that did this automatically"],
                "severity": 3,
                "frequency_note": "weekly per client",
                "supporting_signal_ids": signal_ids[2:3],
            },
        ]
    })


def _tier4_llm_content():
    return json.dumps({
        "is_real": True,
        "tam_estimate": "$1.2B",
        "sam_estimate": "$120M",
        "competitors": [
            {"name": "CompA", "funded": True, "stage": "Seed"},
            {"name": "CompB", "funded": False, "stage": ""},
        ],
        "saturation_ok": True,
        "success_evidence": "growing demand across ecom operators",
        "bear_case": "incumbents may bundle this feature",
        "wave": "ai_native_ops",
        "structural_tailwind": "AI ops consolidation",
        "edge_fit_notes": "ecom operator background, India cost base",
        "wedge_score": 4,
        "wave_score": 3,
        "edge_score": 2,
    })


class _FakeResp:
    """Stand-in for httpx.Response: raises_for_status is a no-op, json() returns a chat payload."""

    def __init__(self, content):
        self._content = content

    def raise_for_status(self):
        pass

    def json(self):
        return {
            "choices": [{"message": {"content": self._content}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50},
        }


def _make_post_mock(content):
    def _post(*args, **kwargs):
        return _FakeResp(content)
    return _post


def test_full_funnel_real_db(monkeypatch):
    """Tier-0(mock) -> Tier-3 -> Tier-4 against live Postgres, LLM mocked, all rolled back."""
    conn = psycopg2.connect(os.environ["PG_DSN"])
    cur = conn.cursor()
    try:
        # ------------------------------------------------------------------
        # Tier-0 (mocked): insert a few test pain_signals directly.
        # ------------------------------------------------------------------
        signals = [
            ("tbi-1", "pain",
             "I am so frustrated doing manual inventory sync across Shopify and Amazon, "
             "I oversell constantly and lose money every week",
             99, 99, "ecom"),
            ("tbi-2", "pain",
             "Why does syncing inventory have to be so manual? I spend hours every day "
             "fixing oversold orders across channels",
             98, 98, "ecom"),
            ("tbi-3", "pain",
             "Building client reports by hand from five ad platforms takes my whole Monday "
             "every single week and it is miserable",
             97, 97, "saas_operator"),
        ]
        signal_ids = []
        for source_id, intent, text, score, dup, icp in signals:
            cur.execute(
                """INSERT INTO analysis.pain_signals
                   (source, source_id, intent, text, score, dup_count, icp_guess)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (source, source_id) DO UPDATE
                     SET text = EXCLUDED.text, score = EXCLUDED.score
                   RETURNING id""",
                (TEST_SOURCE, source_id, intent, text, score, dup, icp))
            signal_ids.append(cur.fetchone()[0])
        assert len(signal_ids) == 3

        # ------------------------------------------------------------------
        # Tier-3: mocked LLM groups the test signals into problem_statements,
        # real DB upsert via upsert_statement (INSERT ... RETURNING id).
        # ------------------------------------------------------------------
        cur.execute(
            "SELECT id, text, source, icp_guess FROM analysis.pain_signals WHERE source = %s ORDER BY id",
            (TEST_SOURCE,))
        test_signals = [(r[0], (r[1] or "")[:600], r[2], r[3]) for r in cur.fetchall()]
        assert len(test_signals) == 3

        monkeypatch.setattr(t3.httpx, "post", _make_post_mock(_tier3_llm_content(signal_ids)))
        monkeypatch.setattr(t3.time, "sleep", lambda *a, **k: None)

        result, tok_in, tok_out = t3.call_llm(test_signals)
        assert result is not None, "call_llm should return a StatementBatch from the mocked response"
        assert len(result.statements) == 2

        ps_ids = []
        for stmt in result.statements:
            sid = t3.upsert_statement(cur, stmt, stmt.supporting_signal_ids, TEST_MODEL)
            assert sid is not None, "upsert_statement must RETURNING id on real DB"
            ps_ids.append(sid)
        assert len(ps_ids) == 2

        # Verify the problem_statements landed (real DB read, same transaction).
        cur.execute(
            "SELECT id, statement, icp, severity, pre_score, extracted_by "
            "FROM analysis.problem_statements WHERE id = ANY(%s)", (ps_ids,))
        rows = cur.fetchall()
        assert len(rows) == 2
        extracted_bys = {r[5] for r in rows}
        assert extracted_bys == {TEST_MODEL}, f"extracted_by should be {TEST_MODEL}, got {extracted_bys}"
        # pre_score = severity * max(1, len(supporting_signal_ids))
        for r in rows:
            assert r[4] is not None and float(r[4]) > 0, "pre_score should be positive"

        # ------------------------------------------------------------------
        # Tier-4: mocked LLM validates one statement, real DB upserts opportunity
        # + opportunity_competitors.
        # ------------------------------------------------------------------
        ps_id = ps_ids[0]
        cur.execute(
            "SELECT statement, icp, severity, wtp_quotes FROM analysis.problem_statements WHERE id = %s",
            (ps_id,))
        statement, icp, severity, wtp_json = cur.fetchone()

        # Mirror t4.run()'s wtp_quotes extraction from the JSONB column.
        wtp_quotes = []
        if wtp_json:
            if isinstance(wtp_json, str):
                wtp_json = json.loads(wtp_json)
            if isinstance(wtp_json, list):
                for q in wtp_json:
                    if isinstance(q, dict) and "quote" in q:
                        wtp_quotes.append(q["quote"])
                    elif isinstance(q, str):
                        wtp_quotes.append(q)

        monkeypatch.setattr(t4.httpx, "post", _make_post_mock(_tier4_llm_content()))
        monkeypatch.setattr(t4.time, "sleep", lambda *a, **k: None)

        opp, _, _ = t4.validate_opportunity(statement, icp, severity, wtp_quotes)
        assert opp is not None, "validate_opportunity should parse the mocked response"
        assert opp.saturation_ok is True

        opp_id = t4.upsert_opportunity(cur, ps_id, statement, icp, opp)
        assert opp_id is not None, "upsert_opportunity must RETURNING id on real DB"

        # Verify the opportunity + competitors landed.
        cur.execute(
            "SELECT id, final_score, saturation_ok, wedge_score, wave_score, edge_score "
            "FROM analysis.opportunities WHERE id = %s", (opp_id,))
        opp_row = cur.fetchone()
        assert opp_row is not None
        # final_score = 0.5*4 + 0.3*3 + 0.2*2 = 3.3 (saturation_ok=True)
        assert abs(float(opp_row[1]) - 3.3) < 0.01, f"final_score expected 3.3, got {opp_row[1]}"
        assert opp_row[2] is True  # saturation_ok
        assert float(opp_row[3]) == 4.0  # wedge_score

        cur.execute(
            "SELECT count(*) FROM analysis.opportunity_competitors WHERE opportunity_id = %s",
            (opp_id,))
        assert cur.fetchone()[0] == 2, "should have 2 competitors (CompA + CompB)"

        # Idempotency of the opportunity upsert (ON CONFLICT problem_statement_id DO UPDATE).
        opp_id_again = t4.upsert_opportunity(cur, ps_id, statement, icp, opp)
        assert opp_id_again == opp_id, "re-upserting the same ps_id should return the same opp id"
        cur.execute(
            "SELECT count(*) FROM analysis.opportunities WHERE problem_statement_id = %s", (ps_id,))
        assert cur.fetchone()[0] == 1, "re-upsert must not duplicate the opportunity"
    finally:
        # Nothing was committed -> rollback discards all test writes. No cleanup needed.
        conn.rollback()
        cur.close()
        conn.close()
