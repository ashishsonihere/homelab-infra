"""YouTube insight extraction — turns video transcripts into structured `youtube_video_insights`.

Mirrors analyze.py: an INCREMENTAL, CHEAP-LLM step that runs SEPARATELY from ingestion. It selects
videos that HAVE a transcript but NO insights row yet, truncates the transcript to bound tokens/cost,
asks a cheap OpenRouter model for a strict-JSON scorecard, validates it with Pydantic, and upserts one
row per video into youtube_video_insights.

KEY PROPERTIES (same contract as analyze.py):
  - INCREMENTAL: only videos lacking an insights row are processed → never analyzed twice (no dup cost).
    Re-runnable; cron chips at new transcripts; a manual run chews the backlog. Idempotent on video_id.
  - CHEAP + ROBUST: small model, per-video call, 3 retries on transient errors, commit every N rows
    (crash-safe — a dropped run resumes where it left off).
  - SCHEMA-ENFORCED: the model's JSON is validated against a Pydantic model before it ever hits the DB,
    so a malformed/hallucinated response is rejected instead of corrupting the table.

Why a thin httpx call and NOT LangChain: analyze.py already talks to OpenRouter with a ~10-line httpx
POST. LangChain would add a heavy dependency tree (and its transitive pins) to a deliberately lean
scrape image for zero benefit here — we need one chat-completion call + JSON parsing, which httpx +
Pydantic already cover. Keeping it consistent with analyze.py also keeps the image small.

Run:  python -m connectors.youtube_analyze
Env:  OPENROUTER_API_KEY, PG_DSN, YT_MODEL (default openai/gpt-4o-mini),
      YT_ANALYSIS_MAX (default 200), YT_ANALYSIS_COMMIT_EVERY (default 20),
      YT_TRANSCRIPT_CHARS (default 12000)
"""
import os
import re
import json
import time
from typing import List

import httpx
import psycopg2
from psycopg2.extras import Json
from pydantic import BaseModel, Field, ValidationError, field_validator

PG_DSN = os.environ["PG_DSN"]
OR_KEY = os.environ["OPENROUTER_API_KEY"]
OR_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = os.environ.get("YT_MODEL", "openai/gpt-4o-mini")
MAX_VIDEOS = int(os.environ.get("YT_ANALYSIS_MAX", "200"))
COMMIT_EVERY = int(os.environ.get("YT_ANALYSIS_COMMIT_EVERY", "20"))
TRANSCRIPT_CHARS = int(os.environ.get("YT_TRANSCRIPT_CHARS", "12000"))


# ---------------------------------------------------------------------------
# Pydantic output schema — the contract the LLM must satisfy
# ---------------------------------------------------------------------------
class VideoInsight(BaseModel):
    scorecard_rating: int = Field(..., ge=1, le=10)
    summary: str
    actionable_insights: List[str] = Field(default_factory=list)
    pain_points: List[str] = Field(default_factory=list)
    ideas: List[str] = Field(default_factory=list)

    @field_validator("scorecard_rating", mode="before")
    @classmethod
    def _clamp_rating(cls, v):
        """Coerce to int and clamp into 1..10 so a model returning 0/11/'7' doesn't fail validation."""
        try:
            v = int(float(v))
        except (TypeError, ValueError):
            v = 1
        return min(10, max(1, v))

    @field_validator("actionable_insights", "pain_points", "ideas", mode="before")
    @classmethod
    def _coerce_list(cls, v):
        """Accept null or a single string where a list is expected (defensive against model drift)."""
        if v is None:
            return []
        if isinstance(v, str):
            return [v] if v.strip() else []
        return list(v)


PROMPT = """You are a market-research analyst extracting MONETIZABLE SaaS opportunities from a YouTube video transcript.
Rate the video's usefulness as a SaaS opportunity signal and extract structured insight.
scorecard_rating (1-10): how rich this video is in actionable, monetizable problems (10 = dense with paid-tool-worthy pain; 1 = no signal).
summary: 1-3 sentences on what the video covers and why it matters for a solo SaaS founder.
actionable_insights: concrete things a founder could DO based on this video.
pain_points: specific problems/frustrations mentioned that someone would PAY to solve.
ideas: product/feature ideas implied by the content.
Return ONLY JSON of exactly this shape:
{"scorecard_rating":1,"summary":"...","actionable_insights":["..."],"pain_points":["..."],"ideas":["..."]}
Video:
"""


def transcript_to_text(transcript):
    """jsonb transcript (list of {text,start,dur}) → a single bounded string for the prompt."""
    if not transcript:
        return ""
    if isinstance(transcript, str):
        try:
            transcript = json.loads(transcript)
        except Exception:
            return transcript[:TRANSCRIPT_CHARS]
    parts = [seg.get("text", "") for seg in transcript if isinstance(seg, dict)]
    return " ".join(p for p in parts if p)[:TRANSCRIPT_CHARS]


def call_llm(title, transcript_text):
    """One OpenRouter chat-completion → validated VideoInsight, or None on hard failure. 3 retries on
    transient network/parse errors (same retry shape as analyze.py)."""
    user = PROMPT + json.dumps({"title": title, "transcript": transcript_text})[:TRANSCRIPT_CHARS + 2000]
    payload = {"model": MODEL, "temperature": 0.2,
               "messages": [{"role": "system", "content": "Return strict JSON only."},
                            {"role": "user", "content": user}]}
    last = None
    for attempt in range(3):
        try:
            r = httpx.post(OR_URL, headers={"Authorization": f"Bearer {OR_KEY}",
                                            "HTTP-Referer": "https://proximity.laenec.in", "X-Title": "market-rs"},
                           json=payload, timeout=120)
            r.raise_for_status()
            txt = r.json()["choices"][0]["message"]["content"]
            return validate_insight(txt)
        except ValidationError as e:
            last = e  # bad shape — retry once or twice, the model may correct itself
        except Exception as e:
            last = e
        time.sleep(2 * (attempt + 1))
    print("  video failed:", repr(last)[:120])
    return None


def validate_insight(txt):
    """Extract the JSON object from a model response and validate it against VideoInsight. Raises
    ValidationError/json errors on failure (caller treats those as a retryable miss)."""
    m = re.search(r'\{.*\}', txt, re.S)
    obj = json.loads(m.group(0)) if m else {}
    return VideoInsight.model_validate(obj)


def upsert_insight(cur, video_id, ins: VideoInsight, model):
    """Idempotent insight upsert (UNIQUE video_id). Re-running refreshes the row + analyzed_at."""
    cur.execute(
        """INSERT INTO youtube_video_insights
           (video_id,scorecard_rating,summary,actionable_insights,pain_points,ideas,model,analyzed_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s,now())
           ON CONFLICT (video_id) DO UPDATE SET
             scorecard_rating=EXCLUDED.scorecard_rating, summary=EXCLUDED.summary,
             actionable_insights=EXCLUDED.actionable_insights, pain_points=EXCLUDED.pain_points,
             ideas=EXCLUDED.ideas, model=EXCLUDED.model, analyzed_at=now()""",
        (video_id, ins.scorecard_rating, ins.summary, Json(ins.actionable_insights),
         Json(ins.pain_points), Json(ins.ideas), model),
    )


def run():
    conn = psycopg2.connect(PG_DSN)
    cur = conn.cursor()
    # incremental: transcript present, no insights row yet (LEFT JOIN … IS NULL → never re-analyze)
    cur.execute(
        """SELECT v.video_id, v.title, v.transcript
           FROM youtube_videos v
           LEFT JOIN youtube_video_insights i ON i.video_id = v.video_id
           WHERE v.transcript IS NOT NULL AND i.video_id IS NULL
           ORDER BY v.published_at DESC NULLS LAST
           LIMIT %s""",
        (MAX_VIDEOS,),
    )
    videos = cur.fetchall()
    done = 0
    pending = 0
    for video_id, title, transcript in videos:
        text = transcript_to_text(transcript)
        if len(text) < 40:
            continue  # transcript too short to be worth a call
        ins = call_llm(title, text)
        if ins is None:
            continue
        upsert_insight(cur, video_id, ins, MODEL)
        done += 1
        pending += 1
        if pending >= COMMIT_EVERY:
            conn.commit()
            pending = 0
            print(f"  …{done} insights", flush=True)
    conn.commit()
    cur.close()
    conn.close()
    msg = f"youtube_analyze: {done} insights upserted (of {len(videos)} candidates)"
    print(msg, flush=True)
    return msg


if __name__ == "__main__":
    run()
