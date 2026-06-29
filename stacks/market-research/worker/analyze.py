"""Analysis funnel — turns collected `documents` into WTP-tiered `signals` + ranked `insights`.

WHAT THIS IS (plain English):
  This is the "reasoning" step of the pipeline. Collection just gathers raw posts/reviews into the
  `documents` table. analyze.py reads those, sends them in small batches to a CHEAP LLM, and asks it
  to extract MONETIZABLE problems — each ranked by how likely someone is to PAY to solve it
  (willingness-to-pay tier 1-5). Each extracted problem becomes a row in `signals`. Then it rolls the
  most common high-WTP topics up into `insights` (the ranked opportunity list you see in Metabase).

KEY PROPERTIES:
  - INCREMENTAL: marks each document `analyzed_at` so it is NEVER analyzed twice (no duplicate work/cost).
    Re-runnable; cron chips away at new docs daily; a big manual run chews the whole backlog.
  - FOCUSED: prioritizes problems suited to a RECURRING subscription SaaS, and flags AI-enabled angles.
    Deprioritizes one-off / lifetime-deal-only / pure platform-policy gripes.
  - CHEAP + ROBUST: small model, batched, retries on transient network errors, commits every ~80 docs
    (crash-safe — a dropped run resumes where it left off).

Run: python analyze.py
Env: OPENROUTER_API_KEY, PG_DSN, ANALYSIS_MAX_DOCS (default 500), ANALYSIS_MODEL, ANALYSIS_SOURCES
"""
import os
import re
import json
import time
import httpx
import psycopg2
from psycopg2.extras import Json, execute_values

PG_DSN = os.environ["PG_DSN"]
OR_KEY = os.environ["OPENROUTER_API_KEY"]
OR_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = os.environ.get("ANALYSIS_MODEL", "openai/gpt-4o-mini")
BATCH = int(os.environ.get("ANALYSIS_BATCH", "8"))
MAX_DOCS = int(os.environ.get("ANALYSIS_MAX_DOCS", "500"))
COMMIT_EVERY = int(os.environ.get("ANALYSIS_COMMIT_EVERY", "80"))
# pain-rich sources only (skip ycombinator company blurbs + news headlines = low pain signal)
SOURCES = os.environ.get(
    "ANALYSIS_SOURCES",
    "reddit,reddit_comment,appstore_reviews,google_play_reviews,wordpress_plugins,appsumo,"
    "hackernews,stackexchange,producthunt"
).split(",")

PROMPT = """You are a market-research analyst hunting MONETIZABLE, RECURRING-revenue SaaS opportunities for a solo founder.
From each item extract 0-3 signals. A signal = a specific problem/desire someone would PAY (ideally monthly) to solve.
PRIORITIZE problems that fit a RECURRING subscription SaaS (ongoing need); FLAG when AI makes a problem newly solvable.
DEPRIORITIZE one-off / lifetime-deal-only / pure platform-policy complaints (e.g. "Amazon suspended me") unless software could genuinely help.
wtp_tier (1-5): 5=already paying for a tool / explicit spend; 4=hacked workaround or hired help; 3=actively asking for a tool; 2=complaint about a paid tool; 1=general gripe.
Use SHORT lowercase snake_case topics (e.g. attribution, inventory_sync, ppc_reporting, return_fraud, review_management).
Return ONLY JSON: {"signals":[{"type":"pain_point|feature_request|pricing|workaround|existing_spend","topic":"...","summary":"<1 sentence>","wtp_tier":1,"recurring_fit":true,"ai_angle":"<short or empty>","sentiment":-1.0,"evidence_url":"<item url>"}]}
Items:
"""


def call_llm(items):
    payload = {"model": MODEL, "temperature": 0.2,
               "messages": [{"role": "system", "content": "Return strict JSON only."},
                            {"role": "user", "content": PROMPT + json.dumps(items)[:14000]}]}
    last = None
    for attempt in range(3):
        try:
            r = httpx.post(OR_URL, headers={"Authorization": f"Bearer {OR_KEY}",
                                            "HTTP-Referer": "https://proximity.laenec.in", "X-Title": "market-rs"},
                           json=payload, timeout=120)
            r.raise_for_status()
            txt = r.json()["choices"][0]["message"]["content"]
            m = re.search(r'\{.*\}', txt, re.S)
            return json.loads(m.group(0)) if m else {"signals": []}
        except Exception as e:
            last = e
            time.sleep(2 * (attempt + 1))
    print("  batch failed:", repr(last)[:100])
    return {"signals": []}


def norm_topic(t):
    return re.sub(r'[^a-z0-9_]', '', (t or "").lower().replace('-', '_').replace(' ', '_'))[:60]


def run():
    conn = psycopg2.connect(PG_DSN)
    cur = conn.cursor()
    try:
        cur.execute("ALTER TABLE documents ADD COLUMN IF NOT EXISTS analyzed_at timestamptz")
        conn.commit()
    except Exception:
        conn.rollback()  # column already added by admin / no DDL privilege for mr_worker — fine
    cur.execute("""SELECT id, url, title, body FROM documents
                   WHERE analyzed_at IS NULL AND source_slug = ANY(%s) AND length(coalesce(body,'')) > 40
                   ORDER BY fetched_at DESC LIMIT %s""", (SOURCES, MAX_DOCS))
    docs = cur.fetchall()
    buf_rows, buf_ids = [], []
    totals = {"sig": 0, "done": 0}

    def flush():
        if buf_rows:
            execute_values(cur, """INSERT INTO signals
                (source_slug,type,topic,summary,sentiment,severity,evidence_url) VALUES %s""", buf_rows)
        if buf_ids:
            cur.execute("UPDATE documents SET analyzed_at=now() WHERE id::text = ANY(%s)",
                        ([str(x) for x in buf_ids],))
        conn.commit()
        totals["sig"] += len(buf_rows)
        totals["done"] += len(buf_ids)
        buf_rows.clear()
        buf_ids.clear()

    for i in range(0, len(docs), BATCH):
        chunk = docs[i:i + BATCH]
        items = [{"url": u, "title": t, "text": (b or "")[:800]} for (_id, u, t, b) in chunk]
        out = call_llm(items)
        for s in out.get("signals", []):
            try:
                sev = int(s.get("wtp_tier") or 0)
            except Exception:
                sev = 0
            summ = (s.get("summary") or "")[:360]
            if s.get("ai_angle"):
                summ += f" [AI: {s['ai_angle']}]"
            if s.get("recurring_fit") is False:
                summ = "[one-off] " + summ
            buf_rows.append(("analysis", s.get("type"), norm_topic(s.get("topic")),
                             summ, s.get("sentiment"), sev, s.get("evidence_url")))
        buf_ids.extend([c[0] for c in chunk])
        if len(buf_ids) >= COMMIT_EVERY:
            flush()
    flush()

    # rebuild insights from ALL accumulated signals (normalize topic so old/new merge)
    cur.execute("TRUNCATE insights")
    cur.execute("""SELECT lower(regexp_replace(topic,'[^a-zA-Z0-9]+','_','g')) AS ntopic,
                          count(*) c, round(avg(severity)::numeric,1) wtp, round(avg(sentiment)::numeric,2) sent
                   FROM signals WHERE coalesce(topic,'')<>''
                   GROUP BY 1 ORDER BY c DESC, wtp DESC LIMIT 30""")
    for topic, c, wtp, sent in cur.fetchall():
        cur.execute("""INSERT INTO insights (title, topic, quant_summary, qual_summary, confidence)
                       VALUES (%s,%s,%s,%s,%s)""",
                    (f"Opportunity: {topic}", topic,
                     Json({"signal_count": c, "avg_wtp_tier": float(wtp or 0), "avg_sentiment": float(sent or 0)}),
                     f"{c} signals on '{topic}'; avg willingness-to-pay tier {wtp}/5; sentiment {sent}.",
                     min(0.95, (c or 0) / 30.0)))
    conn.commit()
    cur.close(); conn.close()
    return f"analyzed {totals['done']} docs · {totals['sig']} signals inserted"


if __name__ == "__main__":
    print(run())
