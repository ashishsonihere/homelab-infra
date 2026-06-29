"""Tier 1 — embed survivors, then ANN-dedup (Analysis-Architecture.md section 1, phase P2).

Two phases, both fully RESUMABLE / checkpointed because the box loses power frequently:

  EMBED   stream analysis.pain_signals WHERE embedding IS NULL in batches; call local Ollama
          (qwen3-embedding:0.6b, 1024-dim halfvec) per row; COMMIT PER BATCH. A crash never loses an
          embedded row and never recomputes one (the WHERE embedding IS NULL skips done rows). Each
          batch re-fetches the next NULL window, so it self-resumes from wherever it died.

  DEDUP   for each not-yet-deduped signal, use the HNSW index to find neighbours with cosine distance
          < 0.08, fold them into the seed (dup_count += folded), and mark the folded rows so they are
          skipped downstream. Dedup state is checkpointed in a `deduped` flag column added idempotently,
          so a power loss mid-dedup resumes instead of restarting.

SAMPLE/LIMIT mode:
  AN_LIMIT          cap total rows embedded this run (0/unset = all)
  AN_SOURCE_FILTER  restrict to certain pain_signals.source values (comma list)
  AN_EMBED_BATCH    rows per commit (default 128)
  AN_DEDUP          '0' to skip the dedup phase (embed only)
  AN_DEDUP_DISTANCE cosine-distance threshold (default 0.08, per the doc)

Run:  python -m analysis.tier1_embed
Env:  PG_DSN, OLLAMA_URL (default http://mr-ollama:11434), AN_* knobs above.
"""
import os
import httpx
import psycopg2

PG_DSN = os.environ["PG_DSN"]
OLLAMA = os.environ.get("OLLAMA_URL", "http://mr-ollama:11434")
MODEL = os.environ.get("AN_EMBED_MODEL", "qwen3-embedding:0.6b")
DIM = 1024                                  # halfvec(1024) per schema
BATCH = int(os.environ.get("AN_EMBED_BATCH", "128"))
AN_LIMIT = int(os.environ["AN_LIMIT"]) if os.environ.get("AN_LIMIT") else None
SOURCE_FILTER = [s.strip() for s in os.environ.get("AN_SOURCE_FILTER", "").split(",") if s.strip()]
DO_DEDUP = os.environ.get("AN_DEDUP", "1") != "0"
DEDUP_DISTANCE = float(os.environ.get("AN_DEDUP_DISTANCE", "0.08"))
EMBED_CHARS = int(os.environ.get("AN_EMBED_CHARS", "8000"))   # truncate very long text before embed
HTTP_TIMEOUT = float(os.environ.get("AN_EMBED_TIMEOUT", "120"))


# ---------------------------------------------------------------------------
# Ollama request builder + caller (factored out so the HTTP can be mocked in tests).
# ---------------------------------------------------------------------------
def build_embed_request(text):
    """Return the (url, json_payload) for a single Ollama /api/embeddings call.

    Ollama's /api/embeddings takes one prompt at a time (model-dependent), so a 'batch' here is a
    Python-side loop over rows that share one DB transaction/commit — the checkpoint unit. Text is
    truncated defensively to EMBED_CHARS (the model has 32k ctx; this just bounds pathological rows).
    """
    return (f"{OLLAMA}/api/embeddings",
            {"model": MODEL, "prompt": (text or "")[:EMBED_CHARS]})


def embed_one(client, text):
    """Call Ollama for one row; return a 1024-float list. Raises on a wrong-dim response so a
    misconfigured model (e.g. nomic 768-dim) fails loudly instead of corrupting the column."""
    url, payload = build_embed_request(text)
    r = client.post(url, json=payload, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    emb = r.json()["embedding"]
    if len(emb) != DIM:
        raise ValueError(f"embedding dim {len(emb)} != expected {DIM} (wrong model? got {MODEL})")
    return emb


def to_vector_literal(emb):
    """pgvector text literal '[a,b,c]' — cast to ::halfvec on insert."""
    return "[" + ",".join(repr(float(x)) for x in emb) + "]"


def ensure_dedup_column(cur):
    """Idempotent checkpoint state for the dedup phase: a 'deduped' flag so a power-loss mid-dedup
    resumes. Folded duplicates get is_duplicate=true and are excluded from Tier 2."""
    cur.execute("ALTER TABLE analysis.pain_signals ADD COLUMN IF NOT EXISTS deduped boolean NOT NULL DEFAULT false")
    cur.execute("ALTER TABLE analysis.pain_signals ADD COLUMN IF NOT EXISTS is_duplicate boolean NOT NULL DEFAULT false")
    cur.execute("CREATE INDEX IF NOT EXISTS pain_signals_deduped ON analysis.pain_signals (deduped) WHERE NOT deduped")


def _source_clause(alias="", params=None):
    if not SOURCE_FILTER:
        return "", params or {}
    col = f"{alias}." if alias else ""
    p = dict(params or {})
    p["sources"] = SOURCE_FILTER
    return f" AND {col}source = ANY(%(sources)s)", p


# ---------------------------------------------------------------------------
# Phase 1: embed
# ---------------------------------------------------------------------------
def embed_phase(conn):
    cur = conn.cursor()
    src_sql, _ = _source_clause()
    done = 0
    with httpx.Client() as client:
        while True:
            remaining = (AN_LIMIT - done) if AN_LIMIT else None
            if remaining is not None and remaining <= 0:
                break
            page = BATCH if remaining is None else min(BATCH, remaining)
            params = {"page": page}
            # PRIORITY ORDER: embed high-signal rows first (engagement * dup_count desc) so high-value
            # clusters are usable before the full backlog finishes. Each batch flips its rows to
            # embedding IS NOT NULL, so they leave the window — the next page is always the next-most-
            # important un-embedded rows, and a crash resumes from exactly there (fully checkpointed).
            sql = (f"SELECT id, text FROM analysis.pain_signals "
                   f"WHERE embedding IS NULL{src_sql} "
                   f"ORDER BY (GREATEST(COALESCE(score,0),0) + 1) * dup_count DESC, id "
                   f"LIMIT %(page)s")
            if SOURCE_FILTER:
                params["sources"] = SOURCE_FILTER
            cur.execute(sql, params)
            rows = cur.fetchall()
            if not rows:
                break
            for sig_id, text in rows:
                try:
                    emb = embed_one(client, text)
                except Exception as e:
                    print(f"  embed err id={sig_id}: {repr(e)[:100]}", flush=True)
                    continue
                cur.execute(
                    "UPDATE analysis.pain_signals SET embedding = %s::halfvec WHERE id = %s",
                    (to_vector_literal(emb), sig_id),
                )
                done += 1
            conn.commit()                       # checkpoint per batch -> resumable
            print(f"  embedded {done} (batch of {len(rows)})", flush=True)
    cur.close()
    print(f"tier1 embed: {done} rows embedded", flush=True)
    return done


# ---------------------------------------------------------------------------
# Phase 2: ANN-dedup (uses the HNSW index)
# ---------------------------------------------------------------------------
def find_neighbors(cur, seed_id, embedding_literal, distance):
    """Return ids (excluding the seed) within cosine distance < `distance`, ordered nearest-first.
    Uses the HNSW index via the <=> cosine-distance operator. Caps at 200 neighbours per seed to
    bound a pathological dense cluster."""
    cur.execute(
        """
        SELECT id FROM analysis.pain_signals
        WHERE embedding IS NOT NULL
          AND NOT is_duplicate
          AND id <> %(seed)s
          AND (embedding <=> %(emb)s::halfvec) < %(dist)s
        ORDER BY embedding <=> %(emb)s::halfvec
        LIMIT 200
        """,
        {"seed": seed_id, "emb": embedding_literal, "dist": distance},
    )
    return [r[0] for r in cur.fetchall()]


def dedup_phase(conn, distance):
    cur = conn.cursor()
    rcur = conn.cursor()                         # separate cursor for neighbour reads
    src_sql, _ = _source_clause()
    folded_total = 0
    seeds = 0
    # bump ef_search for better recall during dedup (per the doc's "bump ef_search at query time")
    cur.execute("SET LOCAL hnsw.ef_search = 100")
    while True:
        # next un-deduped, embedded, non-duplicate seed
        sql = (f"SELECT id, embedding::text FROM analysis.pain_signals "
               f"WHERE embedding IS NOT NULL AND NOT deduped AND NOT is_duplicate{src_sql} "
               f"ORDER BY id LIMIT 1")
        params = {"sources": SOURCE_FILTER} if SOURCE_FILTER else {}
        cur.execute(sql, params)
        row = cur.fetchone()
        if not row:
            break
        seed_id, emb_lit = row
        neigh = find_neighbors(rcur, seed_id, emb_lit, distance)
        if neigh:
            # fold neighbours into the seed: seed.dup_count += sum(neighbour dup_count); mark dups
            rcur.execute(
                """
                WITH folded AS (
                    UPDATE analysis.pain_signals
                    SET is_duplicate = true, deduped = true
                    WHERE id = ANY(%(ids)s) AND NOT is_duplicate
                    RETURNING dup_count
                )
                UPDATE analysis.pain_signals seed
                SET dup_count = seed.dup_count + COALESCE((SELECT sum(dup_count) FROM folded), 0)
                WHERE seed.id = %(seed)s
                """,
                {"ids": neigh, "seed": seed_id},
            )
            folded_total += len(neigh)
        cur.execute("UPDATE analysis.pain_signals SET deduped = true WHERE id = %s", (seed_id,))
        conn.commit()                            # checkpoint per seed -> resumable
        seeds += 1
        if seeds % 500 == 0:
            print(f"  dedup: {seeds} seeds processed, {folded_total} folded", flush=True)
    cur.close()
    rcur.close()
    print(f"tier1 dedup: {seeds} seeds, {folded_total} duplicates folded", flush=True)
    return seeds, folded_total


def run():
    conn = psycopg2.connect(PG_DSN)
    print(f"tier1: model={MODEL} dim={DIM} batch={BATCH} limit={AN_LIMIT} "
          f"sources={SOURCE_FILTER or 'all'} dedup={DO_DEDUP}@{DEDUP_DISTANCE}", flush=True)
    embed_phase(conn)
    if DO_DEDUP:
        cur = conn.cursor()
        ensure_dedup_column(cur)
        conn.commit()
        cur.close()
        dedup_phase(conn, DEDUP_DISTANCE)
    conn.close()
    print("tier1: done", flush=True)


if __name__ == "__main__":
    run()
