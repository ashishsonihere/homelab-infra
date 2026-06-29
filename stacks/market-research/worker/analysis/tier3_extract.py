"""Tier-3 — Problem statement extraction from pain_signals (DIRECT MODE, no embeddings).

WHY: CPU embedding on this no-GPU box is ~7 vectors/min → Tier-1/Tier-2 (embed+cluster)
are INFEASIBLE. Instead, Tier-3 works DIRECTLY on the highest-signal pain_signals: top-N
by (GREATEST(score,0)+1) × dup_count DESC, batched. DeepSeek-V3 groups each batch into
recurring problem themes AND emits Pydantic-validated problem_statements.

PATTERN: mirrors connectors/youtube_analyze.py (httpx + Pydantic + OpenRouter).
COST: ~20K signals ≈ $1-2. Budget-metered: logs tokens + $ per call.

Run:  python -m analysis.tier3_extract
Env:  PG_DSN, OPENROUTER_API_KEY (required),
      AN_TOP_N (default 20000), AN_BATCH (default 200),
      AN_TIER3_MODEL (default deepseek/deepseek-chat),
      AN_COMMIT_EVERY (default 10)
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
MODEL = os.environ.get("AN_TIER3_MODEL", "deepseek/deepseek-chat")
TOP_N = int(os.environ.get("AN_TOP_N", "20000"))
BATCH = int(os.environ.get("AN_BATCH", "200"))
COMMIT_EVERY = int(os.environ.get("AN_COMMIT_EVERY", "10"))

SYSTEM_PROMPT = "You are a product strategist analyzing real customer pain signals. Return strict JSON only."
INSTRUCT = """Analyze these {n} customer pain signals (from Reddit, app reviews, YouTube comments).

Group them into recurring problem themes. For each theme, emit ONE problem statement:

1. Read ALL signals carefully — they are raw quotes from real users.
2. Identify 3-8 DISTINCT recurring problem themes (not one-per-signal — group them).
3. For each theme, provide:
   - icp: which buyer persona feels this pain (agency|ecom|saas_operator)
   - job_to_be_done: what the user is trying to accomplish
   - statement: the core pain in ONE sentence (the problem, not the solution)
   - current_workaround: how they cope today (spreadsheet, manual, ignored, etc.)
   - wtp_quotes: 1-3 actual quotes from the signals that show willingness-to-pay
   - severity: 1-5 (5 = business-threatening pain)
   - frequency_note: how often this pain recurs (daily, weekly, per-project, etc.)
   - supporting_signal_ids: the signal IDs that support this theme

Return JSON: {{"statements": [{{...}}, ...]}}
Only include themes supported by 2+ signals. Do not invent problems not present in the data."""


class ProblemStatement(BaseModel):
    icp: str = Field(default="saas_operator", description="agency|ecom|saas_operator")
    job_to_be_done: str = ""
    statement: str = Field(..., min_length=10)
    current_workaround: str = ""
    wtp_quotes: List[str] = Field(default_factory=list)
    severity: int = Field(default=3, ge=1, le=5)
    frequency_note: str = ""
    supporting_signal_ids: List[int] = Field(default_factory=list)

    @field_validator("severity", mode="before")
    @classmethod
    def _clamp_severity(cls, v):
        try:
            v = int(float(v))
        except (TypeError, ValueError):
            v = 3
        return min(5, max(1, v))

    @field_validator("wtp_quotes", mode="before")
    @classmethod
    def _coerce_wtp(cls, v):
        if v is None:
            return []
        if isinstance(v, str):
            return [v] if v.strip() else []
        return list(v)

    @field_validator("supporting_signal_ids", mode="before")
    @classmethod
    def _coerce_signal_ids(cls, v):
        if v is None:
            return []
        if isinstance(v, (int, float)):
            return [int(v)]
        if isinstance(v, str):
            parts = v.split(",")
            out = []
            for p in parts:
                p = p.strip()
                try:
                    out.append(int(float(p)))
                except ValueError:
                    continue
            return out
        if isinstance(v, list):
            out = []
            for item in v:
                try:
                    out.append(int(float(item)) if item is not None else None)
                except (TypeError, ValueError):
                    continue
            return [x for x in out if x is not None]
        return []

    @field_validator("icp", mode="before")
    @classmethod
    def _coerce_icp(cls, v):
        if not v or not isinstance(v, str):
            return "saas_operator"
        v = v.lower().strip()
        if "agency" in v:
            return "agency"
        if "ecom" in v:
            return "ecom"
        return "saas_operator"


class StatementBatch(BaseModel):
    statements: List[ProblemStatement] = Field(default_factory=list)

    @field_validator("statements", mode="before")
    @classmethod
    def _coerce(cls, v):
        if v is None:
            return []
        return list(v)


def call_llm(signals_batch, prior_themes=None):
    """Call DeepSeek to group a batch of signals into problem statements.
    Returns (StatementBatch, tokens_in, tokens_out) or (None, 0, 0).
    """
    items = []
    for s in signals_batch:
        items.append({"id": s[0], "text": s[1][:500], "source": s[2], "icp": s[3] or "unknown"})
    user = INSTRUCT.format(n=len(items))
    if prior_themes:
        user += "\n\nAlready-identified themes (avoid duplicates):\n"
        user += "\n".join(f"- {t}" for t in prior_themes[:20])
    user += "\n\nSignals:\n" + json.dumps(items)[:14000]

    payload = {"model": MODEL, "temperature": 0.2,
               "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                            {"role": "user", "content": user}]}

    last = None
    for attempt in range(3):
        try:
            r = httpx.post(OR_URL, headers={"Authorization": f"Bearer {OR_KEY}",
                            "HTTP-Referer": "https://proximity.laenec.in", "X-Title": "market-rs"},
                           json=payload, timeout=120)
            r.raise_for_status()
            data = r.json()
            txt = data["choices"][0]["message"]["content"]
            tok_in = data.get("usage", {}).get("prompt_tokens", 0)
            tok_out = data.get("usage", {}).get("completion_tokens", 0)
            result = validate_batch(txt)
            return result, tok_in, tok_out
        except Exception as e:
            last = e
            time.sleep(2 * (attempt + 1))
    print(f"  batch failed: {repr(last)[:120]}", flush=True)
    return None, 0, 0


def validate_batch(txt):
    """Extract JSON from LLM response and validate with Pydantic."""
    # Try to find a JSON object first, then a JSON array
    m = re.search(r'\{.*\}', txt, re.S)
    if not m:
        m = re.search(r'\[.*\]', txt, re.S)
    if not m:
        return StatementBatch(statements=[])
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return StatementBatch(statements=[])
    if isinstance(obj, list):
        obj = {"statements": obj}
    elif isinstance(obj, dict) and "statements" not in obj and "themes" not in obj:
        # Might be a single statement dict (not wrapped in {"statements": [...]})
        if "statement" in obj or "icp" in obj:
            obj = {"statements": [obj]}
    try:
        return StatementBatch.model_validate(obj)
    except Exception:
        statements = []
        for raw in obj.get("statements", obj.get("themes", [])):
            try:
                statements.append(ProblemStatement.model_validate(raw))
            except Exception:
                continue
        return StatementBatch(statements=statements)


def upsert_statement(cur, stmt, batch_signal_ids, model):
    """Insert a problem_statement. cluster_id=NULL (direct mode)."""
    pre_score = stmt.severity * max(1, len(stmt.supporting_signal_ids))
    cur.execute(
        """INSERT INTO analysis.problem_statements
           (cluster_id, statement, icp, job_to_be_done, current_workaround,
            wtp_quotes, severity, frequency_note, pre_score, extracted_by)
           VALUES (NULL, %s, %s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (cluster_id) WHERE cluster_id IS NULL DO NOTHING
           RETURNING id""",
        (stmt.statement, stmt.icp, stmt.job_to_be_done, stmt.current_workaround,
         Json([{"quote": q} for q in stmt.wtp_quotes]), stmt.severity,
         stmt.frequency_note, pre_score, model))
    row = cur.fetchone()
    return row[0] if row else None


def run():
    conn = psycopg2.connect(PG_DSN)
    cur = conn.cursor()

    # Select top-N pain_signals by (score+1)*dup_count DESC
    cur.execute("""
        SELECT id, text, source, icp_guess
        FROM analysis.pain_signals
        WHERE intent = 'pain'
          AND text IS NOT NULL AND LENGTH(text) > 20
        ORDER BY (GREATEST(COALESCE(score,0),0) + 1) * dup_count DESC
        LIMIT %s
    """, (TOP_N,))

    all_signals = cur.fetchall()
    # Truncate to 600 chars in Python (avoids server-side LEFT() UTF-8 issues)
    all_signals = [(s[0], (s[1] or "")[:600], s[2], s[3]) for s in all_signals]
    print(f"tier3: selected {len(all_signals)} top pain_signals", flush=True)

    # Batch them
    batches = [all_signals[i:i + BATCH] for i in range(0, len(all_signals), BATCH)]
    print(f"tier3: {len(batches)} batches of ~{BATCH} signals each", flush=True)

    all_themes = []
    total_tok_in = total_tok_out = 0
    cost_in = cost_out = 0.0
    pending = 0
    statements_saved = 0

    # DeepSeek pricing: $0.2288/M in, $0.3432/M out (June 2026)
    PRICE_IN = 0.2288 / 1_000_000
    PRICE_OUT = 0.3432 / 1_000_000

    for i, batch in enumerate(batches):
        prior = [s.statement for s in all_themes]
        result, tok_in, tok_out = call_llm(batch, prior_themes=prior)

        total_tok_in += tok_in
        total_tok_out += tok_out
        cost_in += tok_in * PRICE_IN
        cost_out += tok_out * PRICE_OUT

        if result and result.statements:
            for stmt in result.statements:
                all_themes.append(stmt)
                sid = upsert_statement(cur, stmt, stmt.supporting_signal_ids, MODEL)
                if sid:
                    statements_saved += 1
                    pending += 1

        if pending >= COMMIT_EVERY:
            conn.commit()
            pending = 0

        cost_so_far = cost_in + cost_out
        print(f"  batch {i+1}/{len(batches)}: +{len(result.statements) if result else 0} statements "
              f"(total {statements_saved}), tokens in={tok_in} out={tok_out}, "
              f"cost ${cost_so_far:.4f}", flush=True)

    conn.commit()
    cur.close()
    conn.close()

    total_cost = cost_in + cost_out
    print(f"\ntier3: DONE — {statements_saved} statements from {len(all_signals)} signals", flush=True)
    print(f"  tokens: in={total_tok_in:,} out={total_tok_out:,}", flush=True)
    print(f"  cost: ${total_cost:.4f} (in ${cost_in:.4f} + out ${cost_out:.4f})", flush=True)
    print(f"  model: {MODEL}", flush=True)


if __name__ == "__main__":
    run()
