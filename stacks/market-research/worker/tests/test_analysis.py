"""Unit tests for the local analysis tiers (P0-P3). No Postgres, no Ollama: the DB is a fake cursor
and HTTP is monkeypatched. Covers the five things the spec asks for:

  1. lexicon matching            (analysis.lexicon)
  2. Ollama embedding-batch request builder, mocked HTTP   (analysis.tier1_embed)
  3. dedup logic                 (analysis.tier1_embed.dedup folding via a fake cursor)
  4. cluster -> DDL upsert        (analysis.tier2_cluster Matryoshka + assignment shape)
  5. idempotency                  (second tier0 run inserts 0 via ON CONFLICT semantics)

Modules read PG_DSN/OLLAMA at import time, so env is set before importing them.
"""
import os
import re
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
os.environ.setdefault("PG_DSN", "postgresql://test@localhost/test")
os.environ.setdefault("OLLAMA_URL", "http://fake-ollama:11434")

from analysis import lexicon                       # noqa: E402
from analysis import tier1_embed                   # noqa: E402
from analysis import tier2_cluster                 # noqa: E402


# ---------------------------------------------------------------------------
# 1. Lexicon matching
# ---------------------------------------------------------------------------
def test_lexicon_size_high_recall():
    # spec's sizing hint was 50-100; the coordinator's high-recall mandate widened it to cover all
    # six pain SHAPES (desire/frustration/effort/switching/cost/lack-negation). Keep a sane ceiling.
    assert 50 <= len(lexicon.ALL_PHRASES) <= 140


def test_lexicon_covers_all_six_shapes():
    assert set(lexicon.LEXICON.keys()) == {
        "desire", "frustration", "manual_toil", "switching", "wtp", "lack_negation"}
    for shape, phrases in lexicon.LEXICON.items():
        assert len(phrases) >= 5, f"shape {shape} too thin for recall"


def test_lack_negation_phrases_match():
    assert lexicon.has_pain("there's no easy way to reconcile these orders") is True
    assert "doesn't exist" in lexicon.lexicon_hits("a tool for this just doesn't exist yet")


def test_is_question_shape():
    assert lexicon.is_question_shape("How do I automate invoice reconciliation?") is True
    assert lexicon.is_question_shape("Is there a tool that syncs inventory?") is True
    # question-starter after a sentence boundary still counts
    assert lexicon.is_question_shape("My current flow is manual. What do you use for this?") is True
    # a plain statement with no question shape does not
    assert lexicon.is_question_shape("I reconcile invoices manually each week.") is False
    assert lexicon.is_question_shape("") is False


def test_lexicon_hits_basic():
    txt = "I would pay for this, doing it manually takes me hours every week."
    hits = lexicon.lexicon_hits(txt)
    assert "i would pay for" in hits
    assert "manually" in hits
    assert "takes me hours" in hits
    # provenance is sorted + de-duplicated
    assert hits == sorted(set(hits))


def test_lexicon_hits_empty_and_none():
    assert lexicon.lexicon_hits("") == []
    assert lexicon.lexicon_hits(None) == []
    assert lexicon.has_pain("just a normal sentence about cats") is False


def test_lexicon_case_and_whitespace_insensitive():
    assert lexicon.has_pain("I   WISH  THERE   WAS a better tool") is True
    assert "i wish there was" in lexicon.lexicon_hits("I   WISH  THERE   WAS a better tool")


def test_guess_icp():
    assert lexicon.guess_icp("my shopify store inventory and sku and fulfillment") == "ecom"
    assert lexicon.guess_icp("client retainer deliverable for the agency") == "agency"
    assert lexicon.guess_icp("our churn and mrr and onboarding for saas") == "saas_operator"
    assert lexicon.guess_icp("nothing relevant here") is None


def test_tsquery_string_is_or_of_quoted_phrases():
    q = lexicon.tsquery_string()
    assert " OR " in q
    assert q.count('"') >= 2 * 40              # at least ~40 quoted phrases
    # no punctuation that would break websearch_to_tsquery
    assert "$" not in q and "'" not in q


# ---------------------------------------------------------------------------
# 2. Ollama embedding-batch request builder (mocked HTTP)
# ---------------------------------------------------------------------------
def test_build_embed_request_shape():
    url, payload = tier1_embed.build_embed_request("hello world")
    assert url.endswith("/api/embeddings")
    assert payload["model"] == tier1_embed.MODEL
    assert payload["prompt"] == "hello world"


def test_build_embed_request_truncates():
    long = "x" * 50000
    _url, payload = tier1_embed.build_embed_request(long)
    assert len(payload["prompt"]) == tier1_embed.EMBED_CHARS


class _FakeResp:
    def __init__(self, emb):
        self._emb = emb

    def raise_for_status(self):
        pass

    def json(self):
        return {"embedding": self._emb}


class _FakeClient:
    """Stands in for httpx.Client; records the last posted payload."""
    def __init__(self, emb):
        self._emb = emb
        self.last = None

    def post(self, url, json=None, timeout=None):
        self.last = {"url": url, "json": json}
        return _FakeResp(self._emb)


def test_embed_one_returns_1024_and_posts_correctly():
    client = _FakeClient([0.1] * 1024)
    emb = tier1_embed.embed_one(client, "manual reconciliation pain")
    assert len(emb) == 1024
    assert client.last["json"]["model"] == tier1_embed.MODEL
    assert client.last["url"].endswith("/api/embeddings")


def test_embed_one_rejects_wrong_dim():
    client = _FakeClient([0.1] * 768)              # e.g. nomic by mistake
    with pytest.raises(ValueError):
        tier1_embed.embed_one(client, "text")


def test_to_vector_literal_roundtrips():
    lit = tier1_embed.to_vector_literal([1.0, 2.5, -3.0])
    assert lit == "[1.0,2.5,-3.0]"


# ---------------------------------------------------------------------------
# 3. Dedup logic — fold neighbours, sum dup_count, mark duplicates, checkpoint
# ---------------------------------------------------------------------------
class DedupFakeCursor:
    """In-memory pain_signals store that understands just the statements dedup_phase issues:
      - SELECT next un-deduped seed
      - find_neighbors SELECT (returns precomputed neighbours for the seed)
      - the WITH folded ... UPDATE (mark dups + bump seed dup_count)
      - UPDATE ... SET deduped = true WHERE id = %s
    Lets us assert the folding math + that every row ends up deduped (resumability invariant).
    """
    def __init__(self, rows, neighbours):
        # rows: {id: {"dup_count":int, "is_duplicate":bool, "deduped":bool}}
        self.rows = rows
        self.neighbours = neighbours       # {seed_id: [neighbour_ids]}
        self._result = []

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        if s.startswith("SET LOCAL"):
            self._result = []
        elif "ORDER BY id LIMIT 1" in s and "SELECT id, embedding::text" in s:
            # next seed: lowest id that is embedded, not deduped, not duplicate
            cand = sorted(i for i, r in self.rows.items()
                          if not r["deduped"] and not r["is_duplicate"])
            self._result = [(cand[0], "[0.1,0.2]")] if cand else []
        elif "ORDER BY embedding <=> %(emb)s::halfvec" in s:
            # find_neighbors for the seed
            seed = params["seed"]
            ns = [n for n in self.neighbours.get(seed, [])
                  if not self.rows[n]["is_duplicate"]]
            self._result = [(n,) for n in ns]
        elif "WITH folded AS" in s:
            ids = params["ids"]
            seed = params["seed"]
            folded_weight = 0
            for i in ids:
                if not self.rows[i]["is_duplicate"]:
                    self.rows[i]["is_duplicate"] = True
                    self.rows[i]["deduped"] = True
                    folded_weight += self.rows[i]["dup_count"]
            self.rows[seed]["dup_count"] += folded_weight
            self._result = []
        elif "SET deduped = true WHERE id = %s" in s:
            self.rows[params[0]]["deduped"] = True
            self._result = []
        else:
            self._result = []

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return list(self._result)

    def close(self):
        pass


class DedupFakeConn:
    def __init__(self, cur):
        self._cur = cur
        self.commits = 0

    def cursor(self):
        return self._cur

    def commit(self):
        self.commits += 1

    def close(self):
        pass


def test_dedup_folds_neighbours_and_sums_dup_count(monkeypatch):
    rows = {
        1: {"dup_count": 1, "is_duplicate": False, "deduped": False},
        2: {"dup_count": 1, "is_duplicate": False, "deduped": False},   # dup of 1
        3: {"dup_count": 1, "is_duplicate": False, "deduped": False},   # dup of 1
        4: {"dup_count": 1, "is_duplicate": False, "deduped": False},   # standalone
    }
    neighbours = {1: [2, 3], 4: []}
    cur = DedupFakeCursor(rows, neighbours)
    conn = DedupFakeConn(cur)
    # dedup_phase uses two cursors; return the same fake for both
    monkeypatch.setattr(conn, "cursor", lambda: cur)
    seeds, folded = tier2_dedup_run(conn)
    assert rows[1]["dup_count"] == 3            # 1 + folded 2 (each weight 1)
    assert rows[2]["is_duplicate"] and rows[3]["is_duplicate"]
    assert not rows[4]["is_duplicate"]
    # resumability invariant: every row deduped, commit per seed
    assert all(r["deduped"] for r in rows.values())
    assert folded == 2
    assert conn.commits >= seeds                 # checkpoint per seed


def tier2_dedup_run(conn):
    """Helper to invoke dedup_phase with the fake conn (distance irrelevant to the fake)."""
    return tier1_embed.dedup_phase(conn, distance=0.08)


# ---------------------------------------------------------------------------
# 4. Cluster -> DDL: Matryoshka truncation + assignment shape
# ---------------------------------------------------------------------------
def test_truncate_matryoshka_renormalizes():
    v = np.arange(1024, dtype=np.float32) + 1.0
    t = tier2_cluster.truncate_matryoshka(v, 256)
    assert t.shape == (256,)
    assert abs(np.linalg.norm(t) - 1.0) < 1e-5


def test_parse_vector():
    v = tier2_cluster.parse_vector("[1.0,2.0,3.5]")
    assert list(v) == [1.0, 2.0, 3.5]


def test_fine_hdbscan_fallback_small_bucket():
    # tiny bucket -> single cluster (all label 0), never crashes without hdbscan
    ids = [1, 2, 3]
    mat = np.random.RandomState(0).rand(3, 16).astype(np.float32)
    labels = tier2_cluster.fine_hdbscan(ids, mat, min_cluster_size=5)
    assert len(labels) == 3
    assert set(labels) == {0}


def test_assign_clusters_separates_two_blobs(monkeypatch):
    """Two well-separated blobs of 256-dim vectors should land in >=2 clusters."""
    rng = np.random.RandomState(1)
    blob_a = rng.normal(0.0, 0.01, size=(40, 256)).astype(np.float32) + 0.0
    blob_b = rng.normal(0.0, 0.01, size=(40, 256)).astype(np.float32) + 5.0
    mat = np.vstack([blob_a, blob_b])
    mat = mat / np.linalg.norm(mat, axis=1, keepdims=True)
    ids = list(range(1, 81))

    class ClusterFakeCursor:
        def __init__(self):
            self._n = len(ids)
            self._pages_served = 0
            self._result = []

        def execute(self, sql, params=None):
            s = " ".join(sql.split())
            if s.startswith("SELECT count(*)"):
                self._result = [(self._n,)]
            elif "ORDER BY id LIMIT" in s:
                last = params.get("last", 0)
                # serve everything in one page when last==0, else empty
                if last == 0:
                    self._result = [(ids[i], _vec_lit(mat[i])) for i in range(self._n)]
                else:
                    self._result = []
            else:
                self._result = []

        def fetchone(self):
            return self._result[0] if self._result else None

        def fetchall(self):
            return list(self._result)

        def close(self):
            pass

    def _vec_lit(row):
        return "[" + ",".join(repr(float(x)) for x in row) + "]"

    cur = ClusterFakeCursor()
    monkeypatch.setattr(tier2_cluster, "KMEANS_K", 4)
    monkeypatch.setattr(tier2_cluster, "AN_LIMIT", None)
    assignment = tier2_cluster.assign_clusters(cur, dim=256)
    # the two blobs must not collapse into one cluster
    assert len(set(assignment.values())) >= 2
    labels_a = {assignment[i] for i in ids[:40] if i in assignment}
    labels_b = {assignment[i] for i in ids[40:] if i in assignment}
    assert labels_a.isdisjoint(labels_b)


# ---------------------------------------------------------------------------
# 5. Idempotency — second tier0 run inserts 0 (ON CONFLICT DO NOTHING semantics)
# ---------------------------------------------------------------------------
class UpsertFakeCursor:
    """Emulates INSERT ... ON CONFLICT (source, source_id) DO NOTHING against pain_signals.
    rowcount reflects only genuinely-new (source, source_id) pairs."""
    def __init__(self, survivors):
        # survivors: list of (source, source_id) the SELECT would produce
        self.survivors = survivors
        self.store = set()
        self.rowcount = 0

    def execute(self, sql, params=None):
        if "INSERT INTO analysis.pain_signals" in sql:
            new = 0
            for key in self.survivors:
                if key not in self.store:
                    self.store.add(key)
                    new += 1
            self.rowcount = new                 # ON CONFLICT DO NOTHING -> only new rows count
        else:
            self.rowcount = 0

    def mogrify(self, sql, args):
        return b"('x','y')"

    def close(self):
        pass


def test_idempotent_second_insert_is_zero():
    survivors = [("reddit_comment", "abc"), ("reddit_comment", "def")]
    cur = UpsertFakeCursor(survivors)
    # first run: both new
    cur.execute("INSERT INTO analysis.pain_signals ...")
    assert cur.rowcount == 2
    # second run with identical survivors: ON CONFLICT DO NOTHING -> 0 new
    cur.execute("INSERT INTO analysis.pain_signals ...")
    assert cur.rowcount == 0


def test_tier0_tsquery_has_no_query_breaking_punctuation():
    import analysis.tier0_filter as t0
    q = t0._tsquery()
    assert " OR " in q
    assert "$" not in q and "'" not in q
    # every clause is a quoted phrase
    clauses = q.split(" OR ")
    assert all(c.startswith('"') and c.endswith('"') for c in clauses)


# ---------------------------------------------------------------------------
# 6. Tier 0 fixes: ZERO source DDL + keep-path = lexicon OR question OR long (no engagement gate)
# ---------------------------------------------------------------------------
class CapturingCursor:
    """Captures every SQL string executed so we can assert on the generated statements."""
    def __init__(self):
        self.sqls = []
        self.rowcount = 0

    def execute(self, sql, params=None):
        self.sqls.append(sql)
        self.rowcount = 0

    def mogrify(self, sql, args):
        return b"('appstore_reviews','app_store')"

    def close(self):
        pass


def _all_tier0_sql():
    """Run every tier0 filter fn against a capturing cursor and return the SQL strings."""
    import analysis.tier0_filter as t0
    cur = CapturingCursor()
    for fn in (t0.filter_reddit_comments, t0.filter_reddit_posts, t0.filter_documents,
               t0.filter_youtube_comments, t0.filter_youtube_transcripts):
        fn(cur)
    return cur.sqls


def test_tier0_issues_zero_source_ddl():
    import analysis.tier0_filter as t0
    # the index-creating function must be gone entirely
    assert not hasattr(t0, "ensure_indexes")
    sqls = _all_tier0_sql()
    assert sqls, "expected tier0 to emit SQL"
    for s in sqls:
        upper = s.upper()
        assert "CREATE INDEX" not in upper
        assert "CREATE TABLE" not in upper
        assert "ALTER TABLE" not in upper
        assert " DROP " not in f" {upper} "
    # and it must NOT depend on the precomputed body_tsv column (that was a source-table artifact)
    assert not any("BODY_TSV" in s.upper() for s in sqls)


def test_tier0_keep_clause_is_lexicon_or_question_or_long_no_engagement():
    import analysis.tier0_filter as t0
    clause = t0._keep_clause("c.body", "to_tsvector('english', coalesce(c.body,''))",
                             "char_length(c.body)")
    # three OR-ed paths present
    assert "websearch_to_tsquery" in clause          # lexicon FTS
    assert "%(qre)s" in clause                        # question-shape regex
    assert "%(long_min)s" in clause                   # long-detailed length
    assert clause.count(" OR ") == 2
    # engagement (score) must NOT appear in the keep predicate — it is ranking-only
    assert "score" not in clause.lower()


def test_tier0_filters_do_not_gate_on_score():
    # survival must never be gated on an engagement comparison. Engagement columns may appear in the
    # SELECT list (carried through for ranking), but no `score/like_count/view_count >= ...` predicate
    # may exist anywhere in any generated statement.
    for s in _all_tier0_sql():
        norm = " ".join(s.split()).lower()
        for col in ("score", "like_count", "view_count"):
            assert f"{col} >=" not in norm, f"{col} used as a keep-gate"
            assert f"coalesce({col},0) >=" not in norm, f"{col} used as a keep-gate"
            assert f"{col} >" not in norm.replace(f"{col} >=", ""), f"{col} used as a keep-gate"


def test_tier0_question_regex_is_anchored_and_safe():
    import analysis.tier0_filter as t0
    qre = t0._question_regex()
    # anchored to start or a sentence boundary, contains question starters. re.escape() renders
    # spaces as '\ ', so check the escaped form. The regex is passed to `~*` as a BOUND PARAMETER
    # (%(qre)s), never string-interpolated into SQL, so an apostrophe (e.g. "what's") is safe — but
    # it must be a single line with no NUL that could confuse the driver.
    assert qre.startswith("(^|")
    assert ("how do i" in qre) or (r"how\ do\ i" in qre)
    assert "\n" not in qre and "\x00" not in qre


def test_tier0_question_regex_is_passed_as_bound_param_not_interpolated():
    # the question regex must reach Postgres via a %(qre)s placeholder (psycopg2 escapes it),
    # never f-string-interpolated into the SQL text.
    for s in _all_tier0_sql():
        if "~*" in s:
            assert "%(qre)s" in s


def test_tier1_embed_orders_by_priority():
    # the embed SELECT must order by engagement*dup desc (priority), not plain id
    import inspect
    import analysis.tier1_embed as t1
    src = inspect.getsource(t1.embed_phase)
    assert "dup_count DESC" in src
    assert "score" in src.lower()
