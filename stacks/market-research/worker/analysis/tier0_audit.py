"""Tier 0 audit — sample rows that did NOT pass the Tier 0 keep-path, so the lexicon can be expanded
empirically (Analysis-Architecture.md: "hand-validate hits").

For each source it pulls N rows from the live table that are absent from analysis.pain_signals
(i.e. Tier 0 dropped them), optionally restricted to high-engagement rows (--min-score / AN_AUDIT_MIN_SCORE)
so you eyeball the MISSES THAT MATTER — a high-upvote complaint the lexicon failed to catch is the
signal that the lexicon needs a new phrase. (High engagement is used HERE only to surface candidate
misses for review; it is never a keep-gate in tier0 itself.)

For each missed row it prints whether it WOULD be caught by the structural paths (question-shape,
long-detailed) using the same lexicon helpers tier0 uses, so you can tell "lexicon needs a phrase"
from "already covered by a structural path but below the length threshold", etc.

ZERO DDL. Read-only: SELECT only, no writes, no index creation.

Usage:
  python -m analysis.tier0_audit                         # 20 misses per source
  AN_AUDIT_N=50 AN_AUDIT_MIN_SCORE=20 \
    AN_SOURCE_FILTER=reddit_comment AN_SUBREDDIT=shopify python -m analysis.tier0_audit

Env:
  PG_DSN              required
  AN_AUDIT_N          rows to sample per source (default 20)
  AN_AUDIT_MIN_SCORE  only sample rows with score/upvotes/likes >= this (default 0 = no filter)
  AN_SOURCE_FILTER    comma list of sources to audit (default all)
  AN_SUBREDDIT        restrict reddit_* to one subreddit
  AN_AUDIT_TEXT_CHARS truncate printed text (default 280)
"""
import os
import textwrap

import psycopg2

from analysis.lexicon import lexicon_hits, is_question_shape

PG_DSN = os.environ["PG_DSN"]
N = int(os.environ.get("AN_AUDIT_N", "20"))
MIN_SCORE = int(os.environ.get("AN_AUDIT_MIN_SCORE", "0"))
SOURCE_FILTER = {s.strip() for s in os.environ.get("AN_SOURCE_FILTER", "").split(",") if s.strip()}
SUBREDDIT = os.environ.get("AN_SUBREDDIT", "").strip()
TEXT_CHARS = int(os.environ.get("AN_AUDIT_TEXT_CHARS", "280"))
LONG_MIN = int(os.environ.get("AN_LONG_MIN", "300"))


def _wanted(src):
    return (not SOURCE_FILTER) or (src in SOURCE_FILTER)


# Each query returns (source_id, text, score) for rows NOT already in pain_signals for that source.
# Anti-join via NOT EXISTS on (source, source_id) — the same natural key tier0 inserts.
def q_reddit_comments():
    sub = " AND p.subreddit = %(subreddit)s" if SUBREDDIT else ""
    return f"""
        SELECT c.reddit_id, c.body, c.score
        FROM reddit_comments c JOIN reddit_posts p ON p.id = c.post_id
        WHERE coalesce(c.score,0) >= %(min_score)s {sub}
          AND char_length(coalesce(c.body,'')) >= 40
          AND NOT EXISTS (SELECT 1 FROM analysis.pain_signals ps
                          WHERE ps.source='reddit_comment' AND ps.source_id = c.reddit_id)
        ORDER BY coalesce(c.score,0) DESC
        LIMIT %(n)s
    """


def q_reddit_posts():
    sub = " AND subreddit = %(subreddit)s" if SUBREDDIT else ""
    return f"""
        SELECT reddit_id, coalesce(title,'')||' '||coalesce(selftext,''), score
        FROM reddit_posts
        WHERE coalesce(score,0) >= %(min_score)s {sub}
          AND char_length(coalesce(title,'')||' '||coalesce(selftext,'')) >= 40
          AND NOT EXISTS (SELECT 1 FROM analysis.pain_signals ps
                          WHERE ps.source='reddit_post' AND ps.source_id = reddit_id)
        ORDER BY coalesce(score,0) DESC
        LIMIT %(n)s
    """


def q_youtube_comments():
    return """
        SELECT yc.comment_id, yc.text, yc.like_count
        FROM youtube_comments yc
        WHERE coalesce(yc.like_count,0) >= %(min_score)s
          AND char_length(coalesce(yc.text,'')) >= 40
          AND NOT EXISTS (SELECT 1 FROM analysis.pain_signals ps
                          WHERE ps.source='youtube_comment' AND ps.source_id = yc.comment_id)
        ORDER BY coalesce(yc.like_count,0) DESC
        LIMIT %(n)s
    """


# source label -> (query, applies?)  — documents covers the review feeds collectively.
SOURCES = [
    ("reddit_comment", q_reddit_comments),
    ("reddit_post", q_reddit_posts),
    ("youtube_comment", q_youtube_comments),
]


def _diagnose(text):
    """Why might tier0 have missed this? Report which structural paths WOULD/would-not catch it."""
    hits = lexicon_hits(text)
    flags = []
    if hits:
        flags.append("lexicon:" + ",".join(hits[:3]))
    if is_question_shape(text):
        flags.append("question-shape")
    if len(text or "") >= LONG_MIN:
        flags.append(f"long({len(text)})")
    if not flags:
        flags.append("NO-PATH (true miss — candidate for new lexicon phrase)")
    return flags


def run():
    conn = psycopg2.connect(PG_DSN)
    cur = conn.cursor()
    params = {"n": N, "min_score": MIN_SCORE, "subreddit": SUBREDDIT}
    print(f"tier0_audit: {N}/source, min_score={MIN_SCORE}, "
          f"sources={SOURCE_FILTER or 'all'}, subreddit={SUBREDDIT or 'all'}\n", flush=True)
    for label, qfn in SOURCES:
        if not _wanted(label):
            continue
        try:
            cur.execute(qfn(), params)
            rows = cur.fetchall()
        except Exception as e:
            print(f"## {label}: ERROR {repr(e)[:140]}\n", flush=True)
            conn.rollback()
            continue
        print(f"## {label}: {len(rows)} sampled misses (score-desc)\n", flush=True)
        for sid, text, score in rows:
            text = text or ""
            flags = _diagnose(text)
            snippet = textwrap.shorten(" ".join(text.split()), width=TEXT_CHARS, placeholder=" …")
            print(f"[{label} {sid} score={score}] {' | '.join(flags)}", flush=True)
            print(f"    {snippet}\n", flush=True)
    cur.close()
    conn.close()
    print("tier0_audit: done", flush=True)


if __name__ == "__main__":
    run()
