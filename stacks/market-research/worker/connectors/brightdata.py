"""Bright Data connector — async dataset/scraper ingestion into `documents`.

Why: Bright Data gives us (a) 5-year HISTORICAL backfill and (b) reliable scraping of
gated sources (Reddit, G2, Amazon, marketplaces) that free APIs/datacenter proxies can't.

Two-phase async design (matches Bright Data's snapshot model):
  trigger : POST jobs from brightdata_jobs.json  -> save snapshot_ids to bd_pending.jsonl
  collect : poll each pending snapshot; when ready -> download JSON -> map -> upsert documents

Run:
  python -m connectors.brightdata trigger          # fire all due jobs
  python -m connectors.brightdata trigger daily     # fire only mode==daily jobs (date=yesterday)
  python -m connectors.brightdata collect          # download+ingest any ready snapshots

Env: BRIGHTDATA_API_TOKEN, PG_DSN.  Config: /opt/market-research/brightdata_jobs.json
Cost guard: each job carries a "limit" (max records) and we refuse to trigger without one.
"""
import os
import sys
import json
import time
import datetime as dt
import httpx
import psycopg2
from psycopg2.extras import Json, execute_values

PG_DSN = os.environ["PG_DSN"]
TOKEN = os.environ["BRIGHTDATA_API_TOKEN"]
API = "https://api.brightdata.com/datasets/v3"
JOBS_FILE = os.environ.get("BD_JOBS_FILE", "/opt/market-research/brightdata_jobs.json")
PENDING_FILE = os.environ.get("BD_PENDING_FILE", "/opt/market-research/bd_pending.jsonl")
HDR = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}


# ---------- field maps: Bright Data row -> documents row ----------
def map_reddit(r):
    ext = r.get("post_id") or r.get("id") or r.get("url")
    body = r.get("description") or r.get("markdown") or r.get("title") or ""
    return {
        "ext_id": str(ext),
        "url": r.get("url"),
        "title": r.get("title"),
        "body": body,
        "metadata": {
            "subreddit": r.get("community_name") or r.get("subreddit"),
            "community_url": r.get("community_url"),
            "user": r.get("user_posted"),
            "date_posted": r.get("date_posted"),
            "num_comments": r.get("num_comments"),
            "num_upvotes": r.get("num_upvotes") or r.get("upvotes"),
            "photos": r.get("photos"), "videos": r.get("videos"),
            "related_queries": r.get("related_queries"),
            "comments": r.get("comments"),  # comment tree if present
        },
    }


def map_generic(r):
    ext = r.get("id") or r.get("url") or r.get("product_id") or json.dumps(r)[:120]
    return {
        "ext_id": str(ext),
        "url": r.get("url"),
        "title": r.get("title") or r.get("name"),
        "body": r.get("description") or r.get("review") or r.get("text") or r.get("content") or "",
        "metadata": r,
    }


def map_reddit_comment(r):
    ext = r.get("comment_id") or r.get("id") or r.get("url")
    return {
        "ext_id": str(ext),
        "url": r.get("url") or r.get("comment_url"),
        "title": (r.get("post_title") or "comment")[:200],
        "body": r.get("comment") or r.get("body") or r.get("text") or "",
        "metadata": {  # NOTE: verify field names against a real Reddit Comments snapshot
            "post_url": r.get("post_url") or r.get("url"),
            "parent": r.get("parent_comment_id") or r.get("parent_id"),
            "user": r.get("user_posted") or r.get("author"),
            "upvotes": r.get("num_upvotes") or r.get("score"),
            "date": r.get("date_posted") or r.get("created_at"),
            "subreddit": r.get("community_name") or r.get("subreddit"),
        },
    }


MAPS = {"reddit": map_reddit, "reddit_comment": map_reddit_comment, "generic": map_generic}


# ---------- phase 1: trigger ----------
def _resolve_inputs(job):
    """Build the input list. Supports keyword-scoped expansion (subreddit x keyword = high-signal,
    relevance over engagement) and daily date-stamping."""
    if job.get("expand_keywords"):  # cartesian of subreddits x keywords -> on-topic posts of ANY upvote count
        return [{"url": s, "keyword": k, "sort_by": job.get("sort_by", "New"),
                 "sort_by_time": job.get("sort_by_time", "All Time"),
                 "num_of_posts": job.get("num_of_posts", 10)}
                for s in job["subreddits"] for k in job["keywords"]]
    if job.get("mode") == "daily":
        y = (dt.date.today() - dt.timedelta(days=1)).isoformat()
        for inp in job["inputs"]:
            inp.setdefault("date", y)
            inp["start_date"] = inp.get("start_date", y)
            inp["end_date"] = inp.get("end_date", y)
    return job["inputs"]


def trigger(only_mode=None):
    jobs = json.load(open(JOBS_FILE))
    fired = []
    with httpx.Client(timeout=60, headers=HDR) as c:
        for job in jobs:
            if only_mode and job.get("mode") != only_mode:
                continue
            if not job.get("limit"):
                print(f"  SKIP {job['name']}: no record 'limit' set (cost guard)"); continue
            params = {"dataset_id": job["dataset_id"], "include_errors": "true"}
            if job.get("discover_by"):
                params["type"] = "discover_new"
                params["discover_by"] = job["discover_by"]
            params["limit_per_input"] = str(job["limit"])
            mapper = MAPS.get(job.get("map", "generic"), map_generic)
            if job.get("sync"):  # synchronous: rows returned directly (small jobs, near-instant)
                r = c.post(f"{API}/scrape", params=params, json=_resolve_inputs(job), timeout=600)
                r.raise_for_status()
                rows = r.json()
                if isinstance(rows, dict):
                    rows = rows.get("data") or rows.get("results") or []
                n = _ingest(rows, job["source_slug"], mapper)
                print(f"  [sync] {job['name']}: ingested {n} docs")
                fired.append({"name": job["name"], "sync": True, "rows": n})
                continue
            r = c.post(f"{API}/trigger", params=params, json=_resolve_inputs(job))
            r.raise_for_status()
            snap = r.json().get("snapshot_id")
            rec = {"snapshot_id": snap, "name": job["name"], "source_slug": job["source_slug"],
                   "map": job.get("map", "generic"), "fired_at": dt.datetime.utcnow().isoformat()}
            with open(PENDING_FILE, "a") as f:
                f.write(json.dumps(rec) + "\n")
            fired.append(rec)
            print(f"  triggered {job['name']} -> {snap} (cap {job['limit']} recs)")
    print(f"triggered {len(fired)} job(s)")
    return fired


# ---------- phase 2: collect ----------
def _ingest(rows, source_slug, mapper):
    if not rows:
        return 0
    vals = []
    for r in rows:
        d = mapper(r)
        if not d.get("ext_id"):
            continue
        vals.append((source_slug, d["ext_id"], d.get("url"), d.get("title"),
                     d.get("body"), Json(d.get("metadata") or {})))
    conn = psycopg2.connect(PG_DSN); cur = conn.cursor()
    execute_values(cur, """
        INSERT INTO documents (source_slug, ext_id, url, title, body, metadata)
        VALUES %s
        ON CONFLICT (source_slug, ext_id) DO UPDATE
          SET title=EXCLUDED.title, body=EXCLUDED.body, metadata=EXCLUDED.metadata
    """, vals)
    conn.commit(); n = cur.rowcount; cur.close(); conn.close()
    return len(vals)


def collect():
    if not os.path.exists(PENDING_FILE):
        print("no pending snapshots"); return
    pending = [json.loads(x) for x in open(PENDING_FILE) if x.strip()]
    still = []
    with httpx.Client(timeout=300, headers=HDR) as c:
        for p in pending:
            snap = p["snapshot_id"]
            prog = c.get(f"{API}/progress/{snap}").json()
            st = prog.get("status")
            if st != "ready":
                print(f"  {p['name']} [{snap}]: {st}"); still.append(p); continue
            data = c.get(f"{API}/snapshot/{snap}", params={"format": "json"})
            try:
                rows = data.json()
            except Exception:
                rows = [json.loads(l) for l in data.text.splitlines() if l.strip()]
            if isinstance(rows, dict):
                rows = rows.get("data") or []
            n = _ingest(rows, p["source_slug"], MAPS.get(p["map"], map_generic))
            print(f"  {p['name']} [{snap}]: ingested {n} docs (records={prog.get('records')})")
            if n == 0 and (prog.get("records") or 0) > 0:
                print("  -> keeping for retry (records exist but download was empty)"); still.append(p)
    with open(PENDING_FILE, "w") as f:
        for p in still:
            f.write(json.dumps(p) + "\n")
    print(f"collect done; {len(still)} still pending")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "collect"
    if cmd == "trigger":
        trigger(only_mode=sys.argv[2] if len(sys.argv) > 2 else None)
    else:
        collect()
