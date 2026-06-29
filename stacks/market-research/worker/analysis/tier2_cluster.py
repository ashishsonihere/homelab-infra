"""Tier 2 — cluster pain signals into candidate problem clusters (Analysis-Architecture.md s.1, P3).

No-GPU path (the doc): full CPU HDBSCAN on millions of vectors is >24h, so:
  1. COARSE  MiniBatchKMeans over the full deduped set, embeddings truncated to 256-dim (Matryoshka)
             — over-cluster on purpose into k buckets. Fitted with partial_fit over streamed pages so
             memory stays bounded (we never hold all vectors at once).
  2. FINE    HDBSCAN inside each coarse bucket (hundreds-to-low-thousands of vectors each) finds real
             sub-clusters and rejects noise. Falls back to the KMeans bucket if hdbscan isn't installed.
  3. WRITE   each fine cluster -> analysis.problem_clusters with label, medoid_signal_id, member_count,
             total_dup_weight (sum dup_count), source_spread (# distinct sources), sources[], centroid.
             trend_acceleration (linear slope of quarterly member counts 2020->2026) and yoy_ratio are
             computed IN SQL from pain_signals.created_at_src.

Operates only on non-duplicate, embedded rows (Tier 1 output). Idempotent within a run: it TRUNCATEs
the cluster assignment for the scoped rows and rewrites clusters, so re-running reproduces a clean set
(it does NOT append phantom clusters). Resumable at the run level (cheap minutes-scale step; re-run on
power loss). cluster_id on pain_signals is the join key Tier 3 uses.

Matryoshka note: qwen3-embedding:0.6b is a Matryoshka model — the first 256 dims are a valid (lower-res)
embedding after L2 re-normalization. We truncate+renormalize for the coarse pass (fast, low memory);
the stored 1024-dim halfvec is untouched and remains the source of truth.

SAMPLE/LIMIT mode:
  AN_LIMIT          cap signals loaded (0/unset = all non-duplicate embedded rows)
  AN_SOURCE_FILTER  restrict to certain sources (comma list)
  AN_KMEANS_K       coarse bucket count (default: ~sqrt(n/2), low-thousands at scale)
  AN_HDBSCAN_MIN    HDBSCAN min_cluster_size (default 5)
  AN_TRUNC_DIM      Matryoshka truncation dim (default 256)

Run:  python -m analysis.tier2_cluster
Env:  PG_DSN, AN_* knobs above.  Requires numpy + scikit-learn; hdbscan optional (graceful fallback).
"""
import math
import os

import numpy as np
import psycopg2
from psycopg2.extras import execute_values
from sklearn.cluster import MiniBatchKMeans

try:
    import hdbscan  # optional; fine-clustering falls back to coarse buckets if missing
    _HAS_HDBSCAN = True
except Exception:
    _HAS_HDBSCAN = False

PG_DSN = os.environ["PG_DSN"]
AN_LIMIT = int(os.environ["AN_LIMIT"]) if os.environ.get("AN_LIMIT") else None
SOURCE_FILTER = [s.strip() for s in os.environ.get("AN_SOURCE_FILTER", "").split(",") if s.strip()]
KMEANS_K = int(os.environ["AN_KMEANS_K"]) if os.environ.get("AN_KMEANS_K") else None
HDBSCAN_MIN = int(os.environ.get("AN_HDBSCAN_MIN", "5"))
TRUNC_DIM = int(os.environ.get("AN_TRUNC_DIM", "256"))
LOAD_BATCH = int(os.environ.get("AN_LOAD_BATCH", "10000"))


def _source_clause(alias="ps"):
    if not SOURCE_FILTER:
        return "", {}
    return f" AND {alias}.source = ANY(%(sources)s)", {"sources": SOURCE_FILTER}


def parse_vector(text):
    """Parse a pgvector/halfvec text literal '[a,b,c]' to a float32 numpy array."""
    return np.fromstring(text.strip()[1:-1], sep=",", dtype=np.float32)


def truncate_matryoshka(vec, dim):
    """First `dim` components, L2-renormalized — a valid lower-res Matryoshka embedding."""
    v = vec[:dim]
    n = np.linalg.norm(v)
    return v / n if n > 0 else v


def count_signals(cur):
    src_sql, params = _source_clause()
    cur.execute(f"SELECT count(*) FROM analysis.pain_signals ps "
                f"WHERE embedding IS NOT NULL AND NOT COALESCE(is_duplicate,false){src_sql}", params)
    return cur.fetchone()[0]


def iter_signal_pages(cur, dim):
    """Stream (ids, matrix) pages of truncated embeddings — never holds the whole corpus in RAM.
    Honors AN_LIMIT and AN_SOURCE_FILTER. Yields (id_list, np.ndarray[n, dim])."""
    src_sql, params = _source_clause()
    last_id = 0
    fetched = 0
    while True:
        if AN_LIMIT and fetched >= AN_LIMIT:
            return
        page = LOAD_BATCH if not AN_LIMIT else min(LOAD_BATCH, AN_LIMIT - fetched)
        q = dict(params)
        q.update({"last": last_id, "page": page})
        cur.execute(
            f"SELECT id, embedding::text FROM analysis.pain_signals ps "
            f"WHERE embedding IS NOT NULL AND NOT COALESCE(is_duplicate,false) "
            f"AND id > %(last)s{src_sql} ORDER BY id LIMIT %(page)s",
            q,
        )
        rows = cur.fetchall()
        if not rows:
            return
        ids = [r[0] for r in rows]
        mat = np.vstack([truncate_matryoshka(parse_vector(r[1]), dim) for r in rows])
        last_id = ids[-1]
        fetched += len(ids)
        yield ids, mat


def coarse_kmeans(cur, k, dim):
    """Two streaming passes: partial_fit to learn centroids, then predict to assign coarse buckets.
    Returns dict id -> coarse_label. Memory-bounded (one page resident at a time)."""
    km = MiniBatchKMeans(n_clusters=k, batch_size=max(256, LOAD_BATCH // 4),
                         n_init=3, random_state=42)
    # pass 1: fit
    for _ids, mat in iter_signal_pages(cur, dim):
        if mat.shape[0] >= 1:
            km.partial_fit(mat)
    # pass 2: assign
    assign = {}
    for ids, mat in iter_signal_pages(cur, dim):
        labels = km.predict(mat)
        for sid, lab in zip(ids, labels):
            assign[sid] = int(lab)
    return assign


def fine_hdbscan(ids, mat, min_cluster_size):
    """HDBSCAN within one coarse bucket. Returns array of local labels (-1 = noise). Falls back to
    a single cluster (all label 0) if hdbscan is unavailable or the bucket is tiny."""
    if not _HAS_HDBSCAN or len(ids) < max(min_cluster_size * 2, 6):
        return np.zeros(len(ids), dtype=int)
    clusterer = hdbscan.HDBSCAN(min_cluster_size=min_cluster_size, metric="euclidean",
                                core_dist_n_jobs=1)
    return clusterer.fit_predict(mat)


def assign_clusters(cur, dim):
    """Run coarse then fine; return dict id -> global_cluster_label (ints, noise dropped)."""
    n = count_signals(cur)
    if n == 0:
        return {}
    k = KMEANS_K or max(2, min(n, int(math.sqrt(n / 2)) + 1))
    k = min(k, n)                                 # k can't exceed sample count
    print(f"tier2: {n} signals, coarse k={k}, trunc_dim={dim}, hdbscan={_HAS_HDBSCAN}", flush=True)
    coarse = coarse_kmeans(cur, k, dim)

    # group ids by coarse bucket, then HDBSCAN each bucket. Re-load each bucket's vectors on demand.
    by_bucket = {}
    for sid, lab in coarse.items():
        by_bucket.setdefault(lab, []).append(sid)

    # build an id->vector lookup by streaming once (bounded by holding only the trunc matrix once).
    vec_by_id = {}
    for ids, mat in iter_signal_pages(cur, dim):
        for sid, row in zip(ids, mat):
            vec_by_id[sid] = row

    global_label = 0
    assignment = {}
    for bucket, sids in sorted(by_bucket.items()):
        mat = np.vstack([vec_by_id[s] for s in sids])
        local = fine_hdbscan(sids, mat, HDBSCAN_MIN)
        # map each non-noise local label to a fresh global cluster id
        local_to_global = {}
        for sid, ll in zip(sids, local):
            if ll == -1:
                continue                           # noise rejected
            key = (bucket, int(ll))
            if key not in local_to_global:
                local_to_global[key] = global_label
                global_label += 1
            assignment[sid] = local_to_global[key]
    print(f"tier2: {global_label} fine clusters (noise dropped)", flush=True)
    return assignment


def write_assignments(conn, assignment):
    """Reset cluster_id for the scoped rows, then write the new assignments. Set-based, idempotent."""
    cur = conn.cursor()
    src_sql, params = _source_clause()
    cur.execute(f"UPDATE analysis.pain_signals ps SET cluster_id = NULL "
                f"WHERE NOT COALESCE(is_duplicate,false){src_sql}", params)
    rows = [(sid, lab) for sid, lab in assignment.items()]
    if rows:
        execute_values(
            cur,
            "UPDATE analysis.pain_signals AS p SET cluster_id = v.lab "
            "FROM (VALUES %s) AS v(id, lab) WHERE p.id = v.id",
            rows,
        )
    conn.commit()
    cur.close()


def build_clusters_sql(conn):
    """Materialize analysis.problem_clusters from the cluster_id assignments — all aggregates and the
    trend metrics computed IN SQL. Rewrites the table for the scoped clusters (idempotent run)."""
    cur = conn.cursor()
    # Clear clusters whose ids are about to be rewritten. We rebuild the full set each run, so a clean
    # slate is correct: delete clusters with no surviving members, then upsert by recomputed label.
    # Since problem_clusters.id is identity (not the kmeans label), we key idempotency on `label`.
    cur.execute("ALTER TABLE analysis.problem_clusters ADD COLUMN IF NOT EXISTS cluster_key bigint")
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS problem_clusters_key ON analysis.problem_clusters (cluster_key)")

    # Aggregate per cluster_id. medoid = highest (dup_count*score) member. centroid = avg embedding.
    cur.execute(
        """
        WITH members AS (
            SELECT cluster_id, id, source, dup_count, COALESCE(score,0) AS score,
                   created_at_src, embedding
            FROM analysis.pain_signals
            WHERE cluster_id IS NOT NULL AND NOT COALESCE(is_duplicate,false)
        ),
        agg AS (
            SELECT cluster_id,
                   count(*)                              AS member_count,
                   sum(dup_count)                        AS total_dup_weight,
                   count(DISTINCT source)                AS source_spread,
                   array_agg(DISTINCT source)            AS sources,
                   -- medoid: member with max dup_count*score
                   (array_agg(id ORDER BY dup_count*GREATEST(score,1) DESC))[1] AS medoid_signal_id,
                   -- centroid: average of the 1024-dim halfvecs (cast to vector for avg, back to halfvec)
                   avg(embedding::vector)::halfvec       AS centroid
            FROM members GROUP BY cluster_id
        ),
        -- quarterly member counts for the trend slope (2020Q1 .. now)
        quarters AS (
            SELECT cluster_id,
                   date_trunc('quarter', created_at_src) AS q,
                   count(*) AS c
            FROM members
            WHERE created_at_src IS NOT NULL
              AND created_at_src >= TIMESTAMPTZ '2020-01-01'
            GROUP BY cluster_id, date_trunc('quarter', created_at_src)
        ),
        slope AS (
            -- linear regression slope of count over quarter-index (regr_slope(y, x))
            SELECT cluster_id,
                   regr_slope(c, EXTRACT(epoch FROM q) / (90*86400.0)) AS trend_acceleration
            FROM quarters GROUP BY cluster_id
        ),
        yoy AS (
            SELECT cluster_id,
                   sum(CASE WHEN created_at_src >= now() - INTERVAL '12 months' THEN 1 ELSE 0 END)::numeric
                   / NULLIF(sum(CASE WHEN created_at_src <  now() - INTERVAL '12 months'
                                      AND created_at_src >= now() - INTERVAL '24 months' THEN 1 ELSE 0 END), 0)
                   AS yoy_ratio
            FROM members WHERE created_at_src IS NOT NULL GROUP BY cluster_id
        )
        INSERT INTO analysis.problem_clusters
            (cluster_key, label, medoid_signal_id, member_count, total_dup_weight,
             source_spread, sources, trend_acceleration, yoy_ratio, centroid)
        SELECT a.cluster_id,
               'cluster_' || a.cluster_id::text,
               a.medoid_signal_id, a.member_count, a.total_dup_weight,
               a.source_spread, a.sources,
               s.trend_acceleration, y.yoy_ratio, a.centroid
        FROM agg a
        LEFT JOIN slope s ON s.cluster_id = a.cluster_id
        LEFT JOIN yoy   y ON y.cluster_id = a.cluster_id
        ON CONFLICT (cluster_key) DO UPDATE SET
            label = EXCLUDED.label,
            medoid_signal_id = EXCLUDED.medoid_signal_id,
            member_count = EXCLUDED.member_count,
            total_dup_weight = EXCLUDED.total_dup_weight,
            source_spread = EXCLUDED.source_spread,
            sources = EXCLUDED.sources,
            trend_acceleration = EXCLUDED.trend_acceleration,
            yoy_ratio = EXCLUDED.yoy_ratio,
            centroid = EXCLUDED.centroid
        """
    )
    written = cur.rowcount
    # drop stale clusters that no longer have members this run
    cur.execute(
        """DELETE FROM analysis.problem_clusters
           WHERE cluster_key IS NOT NULL
             AND cluster_key NOT IN (SELECT DISTINCT cluster_id FROM analysis.pain_signals
                                     WHERE cluster_id IS NOT NULL AND NOT COALESCE(is_duplicate,false))"""
    )
    conn.commit()
    cur.close()
    print(f"tier2: wrote/updated {written} problem_clusters", flush=True)
    return written


def run():
    conn = psycopg2.connect(PG_DSN)
    cur = conn.cursor()
    assignment = assign_clusters(cur, TRUNC_DIM)
    cur.close()
    if not assignment:
        print("tier2: no embedded signals to cluster", flush=True)
        conn.close()
        return
    write_assignments(conn, assignment)
    build_clusters_sql(conn)
    conn.close()
    print("tier2: done", flush=True)


if __name__ == "__main__":
    run()
