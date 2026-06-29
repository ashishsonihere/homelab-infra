"""Tier-4 — Opportunity validation (the funnel endgame).

WHY: Take top problem_statements from Tier-3, validate each with live-web evidence,
score on 3 axes (wedge/wave/edge), apply saturation gate (≥3 funded incumbents → skip),
and upsert opportunities + opportunity_competitors.

PATTERN: mirrors youtube_analyze.py (httpx + Pydantic + OpenRouter).
COST: ~$10-15 for ~50 candidates. DeepSeek for bulk web-read, stronger model for judge.

Run:  python -m analysis.tier4_validate
Env:  PG_DSN, OPENROUTER_API_KEY (required),
      AN_TIER4_TOP_N (default 50), AN_TIER4_MODEL (default deepseek/deepseek-chat),
      AN_TIER4_JUDGE (default anthropic/claude-sonnet-4-20250514),
      AN_TIER4_WEB_SEARCH (default 0 — set to 1 to enable live web validation)
"""
import os
import re
import json
import time
import httpx
import psycopg2
from psycopg2.extras import Json
from pydantic import BaseModel, Field, field_validator
from typing import List, Optional

PG_DSN = os.environ["PG_DSN"]
OR_KEY = os.environ["OPENROUTER_API_KEY"]
OR_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = os.environ.get("AN_TIER4_MODEL", "deepseek/deepseek-chat")
JUDGE_MODEL = os.environ.get("AN_TIER4_JUDGE", "anthropic/claude-sonnet-4-20250514")
TOP_N = int(os.environ.get("AN_TIER4_TOP_N", "50"))
WEB_SEARCH = os.environ.get("AN_TIER4_WEB_SEARCH", "0") == "1"

SYSTEM_PROMPT = "You are a venture analyst validating SaaS opportunities. Return strict JSON only."

VALIDATE_INSTRUCT = """Validate this problem statement as a SaaS opportunity:

Statement: {statement}
ICP: {icp}
Severity: {severity}/5
WTP Quotes: {wtp_quotes}

Analyze:
1. Is this pain real and recurring? (cite evidence from the quotes)
2. TAM/SAM estimate (rough range, justify)
3. Funded competitors — list any known companies solving this. For each: name, funded (true/false), stage.
4. Saturation: are there 3+ funded companies on the EXACT same ICP? If yes, this is saturated.
5. Success rate evidence — any public data on companies that tried this and succeeded/failed?
6. Bear case — the strongest argument AGAINST building this. Who already solved it? Why might it fail?

Return JSON:
{{"is_real": true/false, "tam_estimate": "...", "sam_estimate": "...",
  "competitors": [{{"name": "...", "funded": true, "stage": "..."}}],
  "saturation_ok": true/false, "success_evidence": "...", "bear_case": "...",
  "wave": "ai_native_ops|vertical_ai_agents|data_sovereignty|null",
  "structural_tailwind": "...", "edge_fit_notes": "...",
  "wedge_score": 0-5, "wave_score": 0-5, "edge_score": 0-5}}"""


class OpportunitySchema(BaseModel):
    is_real: bool = True
    tam_estimate: str = ""
    sam_estimate: str = ""
    competitors: List[dict] = Field(default_factory=list)
    saturation_ok: bool = True
    success_evidence: str = ""
    bear_case: str = ""
    wave: Optional[str] = None
    structural_tailwind: str = ""
    edge_fit_notes: str = ""
    wedge_score: float = 0
    wave_score: float = 0
    edge_score: float = 0

    @field_validator("competitors", mode="before")
    @classmethod
    def _coerce_comp(cls, v):
        if v is None:
            return []
        if isinstance(v, dict):
            return [v]
        return list(v)

    @field_validator("wedge_score", "wave_score", "edge_score", mode="before")
    @classmethod
    def _clamp_score(cls, v):
        try:
            v = float(v)
        except (TypeError, ValueError):
            return 0
        return min(5.0, max(0.0, v))


def call_llm(prompt, model=None, timeout=120):
    """Call OpenRouter with a prompt. Returns (text, tok_in, tok_out) or (None, 0, 0)."""
    m = model or MODEL
    payload = {"model": m, "temperature": 0.2,
               "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                            {"role": "user", "content": prompt}]}
    last = None
    for attempt in range(3):
        try:
            r = httpx.post(OR_URL, headers={"Authorization": f"Bearer {OR_KEY}",
                            "HTTP-Referer": "https://proximity.laenec.in", "X-Title": "market-rs"},
                           json=payload, timeout=timeout)
            r.raise_for_status()
            data = r.json()
            txt = data["choices"][0]["message"]["content"]
            tok_in = data.get("usage", {}).get("prompt_tokens", 0)
            tok_out = data.get("usage", {}).get("completion_tokens", 0)
            return txt, tok_in, tok_out
        except Exception as e:
            last = e
            time.sleep(2 * (attempt + 1))
    print(f"  LLM failed: {repr(last)[:120]}", flush=True)
    return None, 0, 0


def validate_opportunity(statement, icp, severity, wtp_quotes):
    """Validate a single problem statement. Returns (OpportunitySchema, tok_in, tok_out)."""
    prompt = VALIDATE_INSTRUCT.format(
        statement=statement, icp=icp, severity=severity,
        wtp_quotes=json.dumps(wtp_quotes)[:2000])
    txt, tok_in, tok_out = call_llm(prompt)
    if not txt:
        return None, tok_in, tok_out
    m = re.search(r'\{.*\}', txt, re.S)
    if not m:
        return None, tok_in, tok_out
    try:
        obj = json.loads(m.group(0))
        return OpportunitySchema.model_validate(obj), tok_in, tok_out
    except Exception as e:
        print(f"  parse error: {repr(e)[:80]}", flush=True)
        return None, tok_in, tok_out


def upsert_opportunity(cur, ps_id, stmt, icp, opp):
    """Insert opportunity + competitors. Returns opportunity id."""
    n_funded = sum(1 for c in opp.competitors if c.get("funded"))
    final_score = 0.0
    if opp.saturation_ok:
        final_score = 0.5 * opp.wedge_score + 0.3 * opp.wave_score + 0.2 * opp.edge_score

    competitors_json = [{"name": c.get("name", ""), "funded": c.get("funded", False),
                         "stage": c.get("stage", ""), "exact_icp_match": c.get("exact_icp_match", False),
                         "url": c.get("url", "")} for c in opp.competitors]

    evidence = {"pain_signal_ids": [], "web_sources": [], "studies": opp.success_evidence}

    cur.execute(
        """INSERT INTO analysis.opportunities
           (problem_statement_id, title, statement, icp, evidence_refs,
            pain_severity, pain_frequency, wtp_signal, icp_reachability, build_complexity,
            saturation_ok, wedge_score, wave, trend_acceleration, capital_inflow,
            structural_tailwind, wave_score, edge_fit_notes, edge_score,
            competitors, n_funded_incumbents, tam_estimate, sam_estimate,
            success_rate_evidence, rebuttal, expansion_vector, final_score,
            validated_by, status)
           VALUES (%s, %s, %s, %s, %s,
                   NULL, NULL, NULL, NULL, NULL,
                   %s, %s, %s, NULL, NULL,
                   %s, %s, %s, %s,
                   %s, %s, %s, %s,
                   %s, %s, %s, %s,
                   %s, 'scored')
           ON CONFLICT (problem_statement_id) DO UPDATE SET
             title=EXCLUDED.title, final_score=EXCLUDED.final_score,
             saturation_ok=EXCLUDED.saturation_ok, competitors=EXCLUDED.competitors,
             updated_at=now()
           RETURNING id""",
        (ps_id, stmt[:200], stmt, icp, Json(evidence),
         opp.saturation_ok, opp.wedge_score, opp.wave,
         opp.structural_tailwind, opp.wave_score, opp.edge_fit_notes, opp.edge_score,
         Json(competitors_json), n_funded,
         opp.tam_estimate, opp.sam_estimate,
         Json({"evidence": opp.success_evidence}), opp.bear_case,
         f"{opp.wave or 'unknown'}_wedge", final_score,
         JUDGE_MODEL if MODEL != JUDGE_MODEL else MODEL))
    row = cur.fetchone()
    opp_id = row[0] if row else None

    # Insert competitors into sidecar table
    if opp_id:
        for comp in opp.competitors:
            name = comp.get("name", "")
            if not name:
                continue
            cur.execute(
                """INSERT INTO analysis.opportunity_competitors
                   (opportunity_id, funded_company_id, name, exact_icp_match, notes)
                   VALUES (%s, NULL, %s, %s, %s)
                   ON CONFLICT (opportunity_id, name) DO NOTHING""",
                (opp_id, name, comp.get("exact_icp_match", False), comp.get("stage", "")))

    return opp_id


def run():
    conn = psycopg2.connect(PG_DSN)
    cur = conn.cursor()

    # Select top-N problem_statements by pre_score
    cur.execute("""
        SELECT id, statement, icp, job_to_be_done, current_workaround,
               wtp_quotes, severity, frequency_note, pre_score
        FROM analysis.problem_statements
        WHERE statement IS NOT NULL
        ORDER BY COALESCE(pre_score, 0) DESC
        LIMIT %s
    """, (TOP_N,))

    statements = cur.fetchall()
    print(f"tier4: selected {len(statements)} problem_statements to validate", flush=True)

    # DeepSeek pricing
    PRICE_IN = 0.2288 / 1_000_000
    PRICE_OUT = 0.3432 / 1_000_000
    total_tok_in = total_tok_out = 0
    cost_in = cost_out = 0.0
    validated = 0
    skipped = 0

    for i, (ps_id, statement, icp, jtbd, workaround, wtp_json, severity,
            freq_note, pre_score) in enumerate(statements):
        # Extract wtp_quotes from jsonb
        wtp_quotes = []
        if wtp_json:
            if isinstance(wtp_json, str):
                try:
                    wtp_json = json.loads(wtp_json)
                except Exception:
                    wtp_json = []
            if isinstance(wtp_json, list):
                for q in wtp_json:
                    if isinstance(q, dict) and "quote" in q:
                        wtp_quotes.append(q["quote"])
                    elif isinstance(q, str):
                        wtp_quotes.append(q)

        opp, tok_in, tok_out = validate_opportunity(statement, icp, severity, wtp_quotes)

        total_tok_in += tok_in
        total_tok_out += tok_out
        cost_in += tok_in * PRICE_IN
        cost_out += tok_out * PRICE_OUT

        if opp:
            opp_id = upsert_opportunity(cur, ps_id, statement, icp, opp)
            validated += 1
            cost = cost_in + cost_out
            print(f"  [{i+1}/{len(statements)}] validated: saturation_ok={opp.saturation_ok} "
                  f"final={0.5*opp.wedge_score + 0.3*opp.wave_score + 0.2*opp.edge_score:.1f} "
                  f"(cost ${cost:.4f})", flush=True)
        else:
            skipped += 1
            print(f"  [{i+1}/{len(statements)}] FAILED to validate", flush=True)

        conn.commit()

    cur.close()
    conn.close()

    total_cost = cost_in + cost_out
    print(f"\ntier4: DONE — {validated} validated, {skipped} failed", flush=True)
    print(f"  tokens: in={total_tok_in:,} out={total_tok_out:,}", flush=True)
    print(f"  cost: ${total_cost:.4f}", flush=True)
    print(f"  models: bulk={MODEL}, judge={JUDGE_MODEL}", flush=True)


if __name__ == "__main__":
    run()
