"""Tier 0 — SQL + lexical pre-filter (Analysis-Architecture.md section 1, phase P1).

Knocks ~70M raw rows down to ~3-5M survivors deterministically, for $0, BEFORE any embedding.
The filtering is done SET-BASED IN POSTGRES — never row-by-row in Python. Each source's survivors are
INSERTed into analysis.pain_signals in one INSERT...SELECT per source, so a multi-million-row pass
never materializes in worker RAM.

ZERO DDL ON SOURCE TABLES. Tier 0 issues no CREATE INDEX (or any DDL) against reddit_posts /
reddit_comments / documents / youtube_*: the app role does not own them, and a write-lock on tables
that the scraper shards are actively inserting into would stall the live pipeline. The FTS match is
done INLINE with `to_tsvector(...) @@ websearch_to_tsquery(...)` — a one-pass seq scan per source,
which is correct for a once-per-run batch filter. The ONLY indexes this pipeline creates live on the
`analysis` schema (owned by the app role) and are created by analysis_schema.sql / tier1.

KEEP-PATH (high recall, engagement is NOT a keep-gate):
    keep = (lexicon hit) OR (question-shape) OR (long-detailed post)
  - lexicon hit:    any pain-shape phrase from analysis.lexicon (desire/frustration/effort/
                    switching/cost/lack-negation).
  - question-shape: the text reads like a help-seeking question/demand (regex over QUESTION_STARTERS).
  - long-detailed:  char_length in [LONG_MIN, MAX_LEN] — long posts are usually real problem write-ups.
  Engagement (score/upvotes/likes) is NEVER a survival gate — the top-upvoted threads are mostly
  jokes/drama/scams, not pain. Engagement is carried through only as a RANKING signal (Tier 1 embeds
  in engagement*dup order; Tier 2 ranks medoids by it).

Sources covered (source label in pain_signals.source):
  reddit_comments        -> reddit_comment       (inline to_tsvector on body; no body_tsv dependency)
  reddit_posts           -> reddit_post
  documents (reviews)    -> per-slug label
  youtube_comments       -> youtube_comment
  youtube_videos.transcript -> youtube_transcript (jsonb flattened to text)

Per surviving row we store: source, source_id, text, score (ranking only), created_at_src,
lexicon_hits[] (set-based in SQL), icp_guess (coarse SQL keyword vote), intent='pain' (placeholder).

Idempotent: UNIQUE(source, source_id) + ON CONFLICT DO NOTHING. Resumable: each source commits
independently; an interrupted run re-runs harmlessly.

SAMPLE/LIMIT mode:
  AN_LIMIT          cap rows scanned PER SOURCE (LIMIT in the SELECT) for fast funnel validation
  AN_SOURCE_FILTER  comma list to restrict which sources run, e.g. "reddit_comment,youtube_comment"
  AN_SUBREDDIT      restrict reddit_* to one subreddit (e.g. "shopify")
  AN_MIN_LEN/AN_MAX_LEN  length gate in chars (defaults 80 / 4000)
  AN_LONG_MIN       structural keep threshold for a "long detailed" post (default 300 chars)

Run:  python -m analysis.tier0_filter
Env:  PG_DSN (required), plus the AN_* knobs above.
"""
import os
import re

import psycopg2

from analysis.lexicon import ALL_PHRASES, ICP_KEYWORDS, QUESTION_STARTERS

PG_DSN = os.environ["PG_DSN"]

# --- length gates. MIN_LEN keeps trivially-short rows out; MAX_LEN bounds the embed cost. The
#     structural long-detailed keep-path fires for rows >= LONG_MIN (and <= MAX_LEN). ---
MIN_LEN = int(os.environ.get("AN_MIN_LEN", "80"))
MAX_LEN = int(os.environ.get("AN_MAX_LEN", "4000"))
LONG_MIN = int(os.environ.get("AN_LONG_MIN", "300"))

# --- sample / limit knobs ---
AN_LIMIT = int(os.environ["AN_LIMIT"]) if os.environ.get("AN_LIMIT") else None
SOURCE_FILTER = {s.strip() for s in os.environ.get("AN_SOURCE_FILTER", "").split(",") if s.strip()}
SUBREDDIT = os.environ.get("AN_SUBREDDIT", "").strip()

# Review feeds in `documents` whose bodies are user pain (reviews / marketplace listings).
# Each maps to a coarse pain_signals.source label. Add slugs here as new review connectors land.
REVIEW_SLUGS = {
    "appstore_reviews": "app_store",
    "google_play_reviews": "google_play",
    "shopify_app": "shopify",
    "alternativeto": "alternativeto",
    "appsumo": "appsumo",
    "atlassian_app": "atlassian",
    "chrome_extension": "chrome_extension",
    "wordpress_plugins": "wordpress",
    "g2": "g2",
    "capterra": "capterra",
    "trustpilot": "trustpilot",
}


def _limit_clause():
    return f" LIMIT {AN_LIMIT}" if AN_LIMIT else ""


def _wanted(source):
    return (not SOURCE_FILTER) or (source in SOURCE_FILTER)


# Shared SQL fragments -------------------------------------------------------
# lexicon_hits[] computed SET-BASED: intersect the row text with the phrase array param.
LEXHITS_SQL = ("ARRAY(SELECT p FROM unnest(%(phrases)s::text[]) p "
               "WHERE position(p in lower({textexpr})) > 0)")


def _icp_case(textexpr):
    """icp_guess: coarse keyword vote in SQL — pick the ICP whose keywords appear most."""
    counts = []
    for icp in ("agency", "ecom", "saas_operator"):
        counts.append(
            f"(SELECT count(*) FROM unnest(%(icp_{icp})s::text[]) k "
            f"WHERE position(k in lower({textexpr})) > 0)"
        )
    a, e, s = counts
    return (
        f"CASE WHEN GREATEST({a},{e},{s}) = 0 THEN NULL "
        f"WHEN {a} >= {e} AND {a} >= {s} THEN 'agency' "
        f"WHEN {e} >= {s} THEN 'ecom' ELSE 'saas_operator' END"
    )


def _keep_clause(textexpr, tsv_expr, length_expr):
    """The KEEP predicate, set-based: lexicon-FTS OR question-shape OR long-detailed.

      tsv_expr     a precomputed `to_tsvector(...)` expression for the row's text
      textexpr     the raw text expression (for the question-shape regex)
      length_expr  char_length expression for the long-detailed path

    Engagement is deliberately absent — it is never a keep-gate.
    """
    fts = f"{tsv_expr} @@ websearch_to_tsquery('english', %(q)s)"
    question = f"lower({textexpr}) ~* %(qre)s"
    long_detailed = f"{length_expr} BETWEEN %(long_min)s AND %(max_len)s"
    return f"(({fts}) OR ({question}) OR ({long_detailed}))"


def _params(extra=None):
    p = {
        "phrases": ALL_PHRASES,
        "icp_agency": ICP_KEYWORDS["agency"],
        "icp_ecom": ICP_KEYWORDS["ecom"],
        "icp_saas_operator": ICP_KEYWORDS["saas_operator"],
        "q": _tsquery(),
        "qre": _question_regex(),
        "min_len": MIN_LEN,
        "max_len": MAX_LEN,
        "long_min": LONG_MIN,
    }
    if extra:
        p.update(extra)
    return p


def filter_reddit_comments(cur):
    src = "reddit_comment"
    if not _wanted(src):
        return 0
    sub = " AND p.subreddit = %(subreddit)s" if SUBREDDIT else ""
    textexpr = "c.body"
    tsv = "to_tsvector('english', coalesce(c.body,''))"      # inline; no dependency on body_tsv
    length_expr = "char_length(c.body)"
    sql = f"""
    INSERT INTO analysis.pain_signals
        (source, source_id, text, score, created_at_src, lexicon_hits, icp_guess, intent)
    SELECT '{src}', c.reddit_id, c.body, c.score,
           c.created_utc,
           {LEXHITS_SQL.format(textexpr=textexpr)},
           {_icp_case(textexpr)},
           'pain'
    FROM reddit_comments c
    JOIN reddit_posts p ON p.id = c.post_id
    WHERE char_length(c.body) BETWEEN %(min_len)s AND %(max_len)s
      AND {_keep_clause(textexpr, tsv, length_expr)}
      {sub}
    {_limit_clause()}
    ON CONFLICT (source, source_id) DO NOTHING
    """
    cur.execute(sql, _params({"subreddit": SUBREDDIT}))
    return cur.rowcount


def filter_reddit_posts(cur):
    src = "reddit_post"
    if not _wanted(src):
        return 0
    sub = " AND subreddit = %(subreddit)s" if SUBREDDIT else ""
    textexpr = "(coalesce(title,'')||' '||coalesce(selftext,''))"
    tsv = f"to_tsvector('english', {textexpr})"
    length_expr = f"char_length({textexpr})"
    sql = f"""
    INSERT INTO analysis.pain_signals
        (source, source_id, text, score, created_at_src, lexicon_hits, icp_guess, intent)
    SELECT '{src}', reddit_id, {textexpr}, score,
           created_utc,
           {LEXHITS_SQL.format(textexpr=textexpr)},
           {_icp_case(textexpr)},
           'pain'
    FROM reddit_posts
    WHERE char_length({textexpr}) BETWEEN %(min_len)s AND %(max_len)s
      AND {_keep_clause(textexpr, tsv, length_expr)}
      {sub}
    {_limit_clause()}
    ON CONFLICT (source, source_id) DO NOTHING
    """
    cur.execute(sql, _params({"subreddit": SUBREDDIT}))
    return cur.rowcount


def filter_documents(cur):
    """One INSERT...SELECT over the review feeds in `documents`, mapping slug -> source label.

    Reviews are inherently complaint/praise text, so for them the keep-path is lexicon OR length
    only (question-shape is rare in a review). Engagement still never gates.
    """
    wanted_slugs = {slug: label for slug, label in REVIEW_SLUGS.items() if _wanted(label)}
    if not wanted_slugs:
        return 0
    textexpr = "(coalesce(d.title,'')||' '||coalesce(d.body,''))"
    tsv = f"to_tsvector('english', {textexpr})"
    length_expr = f"char_length({textexpr})"
    label_values = ",".join(cur.mogrify("(%s,%s)", (s, l)).decode() for s, l in wanted_slugs.items())
    sql = f"""
    INSERT INTO analysis.pain_signals
        (source, source_id, text, score, created_at_src, lexicon_hits, icp_guess, intent)
    SELECT m.label, d.ext_id, {textexpr},
           NULLIF(regexp_replace(coalesce(d.metadata->>'rating', d.metadata->>'score',''),
                                 '[^0-9].*$', ''), '')::int,
           coalesce(
               CASE WHEN (d.metadata->>'at') ~ '^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}'
                    THEN (d.metadata->>'at')::timestamptz END,
               d.fetched_at),
           {LEXHITS_SQL.format(textexpr=textexpr)},
           {_icp_case(textexpr)},
           'pain'
    FROM documents d
    JOIN (VALUES {label_values}) AS m(slug, label) ON m.slug = d.source_slug
    WHERE char_length({textexpr}) BETWEEN %(min_len)s AND %(max_len)s
      AND ( ({tsv} @@ websearch_to_tsquery('english', %(q)s))
            OR ({length_expr} BETWEEN %(long_min)s AND %(max_len)s) )
    {_limit_clause()}
    ON CONFLICT (source, source_id) DO NOTHING
    """
    cur.execute(sql, _params())
    return cur.rowcount


def filter_youtube_comments(cur):
    src = "youtube_comment"
    if not _wanted(src):
        return 0
    textexpr = "yc.text"
    tsv = "to_tsvector('english', coalesce(yc.text,''))"
    length_expr = "char_length(coalesce(yc.text,''))"
    sql = f"""
    INSERT INTO analysis.pain_signals
        (source, source_id, text, score, created_at_src, lexicon_hits, icp_guess, intent)
    SELECT '{src}', yc.comment_id, yc.text, yc.like_count,
           yc.published_at,
           {LEXHITS_SQL.format(textexpr=textexpr)},
           {_icp_case(textexpr)},
           'pain'
    FROM youtube_comments yc
    WHERE char_length(coalesce(yc.text,'')) BETWEEN %(min_len)s AND %(max_len)s
      AND {_keep_clause(textexpr, tsv, length_expr)}
    {_limit_clause()}
    ON CONFLICT (source, source_id) DO NOTHING
    """
    cur.execute(sql, _params())
    return cur.rowcount


def filter_youtube_transcripts(cur):
    """Flatten youtube_videos.transcript (jsonb array of {text,start,dur}) to one text per video,
    then apply the same keep-path (lexicon OR question OR long). Transcripts are long; the long-keep
    upper bound is relaxed to 20k here so a real transcript isn't excluded by MAX_LEN."""
    src = "youtube_transcript"
    if not _wanted(src):
        return 0
    flat = "(SELECT string_agg(seg->>'text', ' ') FROM jsonb_array_elements(v.transcript) seg)"
    textexpr = "t.flat"
    tsv = "to_tsvector('english', t.flat)"
    sql = f"""
    INSERT INTO analysis.pain_signals
        (source, source_id, text, score, created_at_src, lexicon_hits, icp_guess, intent)
    SELECT '{src}', t.video_id, t.flat, t.view_count,
           t.published_at,
           {LEXHITS_SQL.format(textexpr=textexpr)},
           {_icp_case(textexpr)},
           'pain'
    FROM (
        SELECT v.video_id, v.published_at, v.view_count, {flat} AS flat
        FROM youtube_videos v
        WHERE v.transcript IS NOT NULL AND jsonb_typeof(v.transcript) = 'array'
    ) t
    WHERE t.flat IS NOT NULL
      AND char_length(t.flat) BETWEEN %(min_len)s AND 20000
      AND ( ({tsv} @@ websearch_to_tsquery('english', %(q)s))
            OR (lower(t.flat) ~* %(qre)s)
            OR (char_length(t.flat) BETWEEN %(long_min)s AND 20000) )
    {_limit_clause()}
    ON CONFLICT (source, source_id) DO NOTHING
    """
    cur.execute(sql, _params())
    return cur.rowcount


_TSQUERY_CACHE = None
_QRE_CACHE = None


def _tsquery():
    """websearch_to_tsquery string: OR of every lexicon phrase (quoted = adjacency match).
    Built from the same lexicon as lexicon_hits() so the SQL gate and the provenance agree."""
    global _TSQUERY_CACHE
    if _TSQUERY_CACHE is None:
        parts, seen = [], set()
        for phrase in ALL_PHRASES:
            cleaned = re.sub(r"[^a-z0-9 ]", " ", phrase)
            cleaned = re.sub(r"\s+", " ", cleaned).strip()
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                parts.append('"%s"' % cleaned)
        _TSQUERY_CACHE = " OR ".join(parts)
    return _TSQUERY_CACHE


def _question_regex():
    """POSIX-ERE for the structural question-shape keep-path: a question-starter at the start of the
    text or right after a sentence boundary. Mirrors lexicon.is_question_shape() so the SQL gate and
    the Python audit agree on what 'question-shape' means."""
    global _QRE_CACHE
    if _QRE_CACHE is None:
        alts = "|".join(re.escape(s) for s in QUESTION_STARTERS)
        # (^|sentence boundary) starter  — POSIX ERE; ~* makes it case-insensitive in Postgres
        _QRE_CACHE = r"(^|[.!?]\s+|\n\s*)(" + alts + r")"
    return _QRE_CACHE


STAGES = [
    ("reddit_comments", filter_reddit_comments),
    ("reddit_posts", filter_reddit_posts),
    ("documents(reviews)", filter_documents),
    ("youtube_comments", filter_youtube_comments),
    ("youtube_transcripts", filter_youtube_transcripts),
]


def run():
    conn = psycopg2.connect(PG_DSN)
    cur = conn.cursor()
    print(f"tier0: ZERO source DDL; keep = lexicon|question|long(>={LONG_MIN}); "
          f"len {MIN_LEN}-{MAX_LEN} limit={AN_LIMIT} "
          f"sources={SOURCE_FILTER or 'all'} subreddit={SUBREDDIT or 'all'}", flush=True)
    total = 0
    for name, fn in STAGES:
        try:
            n = fn(cur)
            conn.commit()           # commit per source -> resumable
            total += n
            print(f"tier0: {name} -> {n} new pain_signals", flush=True)
        except Exception as e:
            conn.rollback()
            print(f"tier0: {name} ERROR {repr(e)[:160]}", flush=True)
    cur.close()
    conn.close()
    print(f"tier0: done, {total} new pain_signals inserted", flush=True)


if __name__ == "__main__":
    run()
