"""Reddit market-research worker.

Pulls posts + FULL nested comment trees from chosen subreddits via the official
Reddit API (PRAW), stores them in Postgres (market_research DB) with threading
preserved (reddit_comments.parent_reddit_id -> recursive CTE rebuilds the tree).

Usage (inside the container):
  python reddit_worker.py seed     # enqueue posts from SUBREDDITS into the Redis queue
  python reddit_worker.py worker    # run an RQ worker that fetches queued posts

ToS: official API only, OAuth, descriptive user-agent, we analyze (don't republish raw).
"""
import os
import sys
import praw
import psycopg2
from psycopg2.extras import execute_values
from redis import Redis
from rq import Queue, Worker
from dotenv import load_dotenv

load_dotenv()

PG_DSN = os.environ["PG_DSN"]
REDIS_URL = os.environ.get("REDIS_URL", "redis://devcore-redis:6379/4")


def reddit_client():
    return praw.Reddit(
        client_id=os.environ["REDDIT_CLIENT_ID"],
        client_secret=os.environ["REDDIT_CLIENT_SECRET"],
        username=os.environ.get("REDDIT_USERNAME"),
        password=os.environ.get("REDDIT_PASSWORD"),
        user_agent=os.environ["REDDIT_USER_AGENT"],
    )


def _strip(fullname):
    # 't1_abc' (comment) / 't3_abc' (post) -> 'abc'
    return fullname.split("_", 1)[1] if fullname and "_" in fullname else fullname


def fetch_post(post_id: str) -> str:
    """RQ job: fetch one submission + all comments, upsert into Postgres."""
    r = reddit_client()
    s = r.submission(id=post_id)
    s.comments.replace_more(limit=None)  # expand the whole tree (uses extra API calls)

    conn = psycopg2.connect(PG_DSN)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO reddit_posts
              (reddit_id, subreddit, title, selftext, author, score, upvote_ratio,
               num_comments, url, flair, created_utc)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (reddit_id) DO UPDATE
              SET score = EXCLUDED.score, num_comments = EXCLUDED.num_comments
            RETURNING id
            """,
            (s.id, str(s.subreddit), s.title, s.selftext or None, str(s.author),
             s.score, getattr(s, "upvote_ratio", None), s.num_comments, s.url,
             s.link_flair_text, int(s.created_utc)),
        )
        post_pk = cur.fetchone()[0]

        rows = [
            (post_pk, c.id, _strip(c.parent_id), c.body, str(c.author), c.score,
             getattr(c, "depth", None), len(getattr(c, "all_awardings", []) or []),
             int(c.created_utc))
            for c in s.comments.list()
        ]
        if rows:
            execute_values(
                cur,
                """
                INSERT INTO reddit_comments
                  (post_id, reddit_id, parent_reddit_id, body, author, score,
                   depth, awards, created_utc)
                VALUES %s
                ON CONFLICT (reddit_id) DO UPDATE SET score = EXCLUDED.score
                """,
                rows,
            )
        conn.commit()
        return f"{post_id}: {len(rows)} comments"
    finally:
        conn.close()


def seed():
    """Enqueue the newest N posts from each subreddit."""
    r = reddit_client()
    q = Queue("reddit", connection=Redis.from_url(REDIS_URL))
    subs = [x.strip() for x in os.environ.get("SUBREDDITS", "ecommerce").split(",") if x.strip()]
    n = int(os.environ.get("POSTS_PER_SUB", "300"))
    count = 0
    for sub in subs:
        for post in r.subreddit(sub).new(limit=n):
            q.enqueue(fetch_post, post.id, job_timeout=600)
            count += 1
    print(f"enqueued {count} posts across {len(subs)} subreddits: {subs}")


def run_worker():
    Worker(["reddit"], connection=Redis.from_url(REDIS_URL)).work(with_scheduler=True)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "worker"
    {"seed": seed, "worker": run_worker}.get(cmd, run_worker)()
