"""Arctic Shift Reddit connector — CORRELATED post+comment, MEMORY-SAFE (streams; flushes per page).

Fixes the OOM: instead of loading a whole subreddit's posts+comments into RAM, it STREAMS pages and
flushes each one. Posts upsert per page. Comments stream into a SESSION-LOCAL TEMP table (in Postgres,
private per shard connection — safe for parallel shards), then one set-insert (resolve post_id) +
one parent_id fixup per subreddit. Bounded retries + live logging. Idempotent (ON CONFLICT reddit_id).
Run: python -m connectors.arctic_reddit   Env: AS_SUBREDDITS, AS_AFTER, AS_BEFORE, AS_MAX_PER_SUB, AS_MAX_COMMENTS
"""
import os
import time
import datetime as dt
import httpx
import psycopg2
from psycopg2.extras import execute_values

PG_DSN = os.environ["PG_DSN"]
BASE = "https://arctic-shift.photon-reddit.com/api"
UA = {"User-Agent": "research-correlated-reddit/1.0"}
SUBS = os.environ.get("AS_SUBREDDITS", "shopify,ecommerce").split(",")
AFTER = os.environ.get("AS_AFTER", "2020-01-01")
BEFORE = os.environ.get("AS_BEFORE", dt.date.today().isoformat())
POST_CAP = int(os.environ.get("AS_MAX_PER_SUB", "5000000"))       # effectively uncapped (was 200000 — truncated webdev/Wordpress/SideProject)
COMMENT_CAP = int(os.environ.get("AS_MAX_COMMENTS", "20000000"))  # effectively uncapped
RESUME = os.environ.get("AS_RESUME", "1") != "0"                  # AS_RESUME=0 → full re-walk from AFTER to fill internal gaps (idempotent)
LIMIT = 100


def _epoch(d):
    return int(dt.datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc).timestamp())


def _ts(e):
    try:
        return dt.datetime.fromtimestamp(int(e), tz=dt.timezone.utc)
    except Exception:
        return None


def fetch_pages(c, kind, sub, after, before, cap):
    """Generator yielding successive pages (lists) — never holds the whole sub in RAM. Bounded retries."""
    got, cursor, fails = 0, after, 0
    while got < cap:
        params = {"subreddit": sub, "after": cursor, "before": before, "limit": LIMIT, "sort": "asc"}
        try:
            r = c.get(f"{BASE}/{kind}/search", params=params, timeout=60)
            if r.status_code != 200:
                fails += 1
                if fails >= 6:
                    print(f"  [{sub}/{kind}] STOP http {r.status_code} x6", flush=True); return
                time.sleep(3 * fails); continue
            payload = r.json()
            rows = payload.get("data") if isinstance(payload, dict) else payload
            fails = 0
        except Exception as e:
            fails += 1
            if fails >= 6:
                print(f"  [{sub}/{kind}] STOP {repr(e)[:50]} x6", flush=True); return
            time.sleep(3 * fails); continue
        if not rows:
            return
        yield rows
        got += len(rows)
        last = rows[-1].get("created_utc")
        if last is None:
            return
        nxt = int(last) + 1
        if nxt <= cursor:
            return
        cursor = nxt
        if got % 5000 < LIMIT:
            print(f"  [{sub}/{kind}] {got} up to {dt.datetime.fromtimestamp(cursor, tz=dt.timezone.utc).date()}", flush=True)
        if len(rows) < LIMIT:
            return
        time.sleep(0.4)


def run():
    conn = psycopg2.connect(PG_DSN)
    cur = conn.cursor()
    # session-local temp staging table — private per shard connection, persists across commits
    cur.execute("""CREATE TEMP TABLE IF NOT EXISTS reddit_stage_c (reddit_id text, link_id text,
        parent_reddit_id text, body text, author text, score int, created_utc timestamptz)""")
    conn.commit()
    af, bf = _epoch(AFTER), _epoch(BEFORE)
    with httpx.Client(headers=UA, follow_redirects=True) as c:
        for sub in [s.strip() for s in SUBS if s.strip()]:
            # --- RESUME: the DB is the cursor. Start each stream from this sub's last saved record
            # so an interrupted run (power loss, OOM, kill) continues FORWARD instead of re-walking
            # from AFTER. The one-page overlap at the resume point is absorbed by ON CONFLICT, so
            # there are never duplicate rows — only a tiny, harmless re-fetch of the last page. ---
            if RESUME:
                cur.execute("SELECT COALESCE(EXTRACT(epoch FROM MAX(created_utc))::bigint, 0) "
                            "FROM reddit_posts WHERE subreddit=%s", (sub,))
                post_after = max(af, int(cur.fetchone()[0] or 0))
            else:
                post_after = af   # full re-walk from AFTER → fills internal gaps; ON CONFLICT dedups
            print(f"r/{sub}: {'resume' if RESUME else 'REWALK'} posts from {dt.datetime.fromtimestamp(post_after, tz=dt.timezone.utc).date()}", flush=True)
            # --- POSTS: stream + upsert per page (no accumulation) ---
            for page in fetch_pages(c, "posts", sub, post_after, bf, POST_CAP):
                prows = []
                for p in page:
                    cu = _ts(p.get("created_utc"))
                    if not p.get("id") or cu is None:
                        continue
                    prows.append((p["id"], p.get("subreddit") or sub, (p.get("title") or "")[:2000],
                                  p.get("selftext") or "", p.get("author"), int(p.get("score") or 0),
                                  p.get("upvote_ratio"), int(p.get("num_comments") or 0),
                                  p.get("url") or p.get("permalink"), cu))
                if prows:
                    execute_values(cur, """INSERT INTO reddit_posts
                        (reddit_id,subreddit,title,selftext,author,score,upvote_ratio,num_comments,url,created_utc)
                        VALUES %s ON CONFLICT (reddit_id) DO UPDATE SET
                        score=EXCLUDED.score, num_comments=EXCLUDED.num_comments, updated_at=now()""", prows)
                    conn.commit()
            # --- COMMENTS: stream into temp stage (Postgres), then set-insert + parent fixup ---
            # Comments commit all-or-nothing per sub, so MAX(comment date) is either ~end (this sub
            # is done) or 0 (its comments were never inserted) → resume from the last saved comment,
            # else from AFTER. Posts for the whole window already exist above, so the JOIN resolves.
            if RESUME:
                cur.execute("SELECT COALESCE(EXTRACT(epoch FROM MAX(c.created_utc))::bigint, 0) "
                            "FROM reddit_comments c JOIN reddit_posts p ON p.id = c.post_id WHERE p.subreddit=%s", (sub,))
                comm_after = max(af, int(cur.fetchone()[0] or 0))
            else:
                comm_after = af   # full re-walk
            cur.execute("TRUNCATE reddit_stage_c"); conn.commit()
            staged = 0
            for page in fetch_pages(c, "comments", sub, comm_after, bf, COMMENT_CAP):
                srows = []
                for cm in page:
                    cu = _ts(cm.get("created_utc"))
                    if not cm.get("id") or cu is None:
                        continue
                    par = cm.get("parent_id") or ""
                    srows.append((cm["id"], (cm.get("link_id") or "").replace("t3_", ""),
                                  par.replace("t1_", "") if par.startswith("t1_") else None,
                                  cm.get("body") or "", cm.get("author"), int(cm.get("score") or 0), cu))
                if srows:
                    execute_values(cur, "INSERT INTO reddit_stage_c VALUES %s", srows)
                    conn.commit()
                    staged += len(srows)
            cur.execute("""INSERT INTO reddit_comments (reddit_id,post_id,parent_id,body,author,score,created_utc)
                SELECT s.reddit_id, p.id, NULL, s.body, s.author, s.score, s.created_utc
                FROM reddit_stage_c s JOIN reddit_posts p ON p.reddit_id = s.link_id
                ON CONFLICT (reddit_id) DO UPDATE SET score=EXCLUDED.score, body=EXCLUDED.body""")
            ins = cur.rowcount
            cur.execute("""UPDATE reddit_comments ch SET parent_id = par.id
                FROM reddit_stage_c s JOIN reddit_comments par ON par.reddit_id = s.parent_reddit_id
                WHERE ch.reddit_id = s.reddit_id AND s.parent_reddit_id IS NOT NULL""")
            cur.execute("TRUNCATE reddit_stage_c"); conn.commit()
            print(f"r/{sub}: comments staged {staged}, inserted {ins}", flush=True)
    cur.close(); conn.close()
    print("arctic_reddit: done", flush=True)


if __name__ == "__main__":
    run()
