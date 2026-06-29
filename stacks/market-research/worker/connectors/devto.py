"""Dev.to (Forem) public-API connector — FREE, no key for public reads.

PROBLEM/PAIN-mapping: developer-community posts on dev-tool / SaaS topics surface what
builders struggle with, ask for, and complain about = wedge angles for dev tools & SaaS.
We page the public Articles list across pain-relevant tags, then (optionally, cheaply)
fetch each article's body_markdown for a richer signal.

Endpoint (public, no key):
  GET https://dev.to/api/articles?per_page=100&page=N&tag=<tag>
  GET https://dev.to/api/articles/{id}              -> full object incl. body_markdown

Notes verified against the live Forem API (June 2026):
  * per_page accepts 1..1000 (we use 100 by default — plenty, and keeps single-fetch cheap).
  * Public reads need no API key. Forem rate-limits per-IP; the documented public ceiling is
    generous (well above what this run needs). We pace with a small per-request delay and
    wrap every tag/page/detail call in try/except + backoff-on-429 to stay polite.
  * The list endpoint already returns title, description, tag_list, public_reactions_count,
    comments_count, reading_time_minutes, published_at and a nested user object — enough to
    build a useful document without the per-id detail call. body_markdown is detail-only.

Writes to the universal `documents` table (source_slug='devto'). httpx + psycopg2 only.
Run:  python -m connectors.devto
"""
import os
import time
import httpx
import psycopg2
from psycopg2.extras import Json, execute_values

PG_DSN = os.environ["PG_DSN"]
SLUG = "devto"

API = "https://dev.to/api/articles"
TAGS = os.environ.get(
    "DEVTO_TAGS",
    "saas,startup,webdev,ecommerce,marketing,indiehackers,productivity,api"
).split(",")
PER_PAGE = int(os.environ.get("DEVTO_PER_PAGE", "100"))     # Forem max is 1000; 100 is plenty
MAX_PAGES = int(os.environ.get("DEVTO_MAX_PAGES", "5"))     # pages per tag
FETCH_BODY = os.environ.get("DEVTO_FETCH_BODY", "1") == "1"  # fetch body_markdown per article
DELAY = float(os.environ.get("DEVTO_DELAY", "0.5"))         # polite per-request pacing (seconds)
DETAIL_DELAY = float(os.environ.get("DEVTO_DETAIL_DELAY", "0.25"))  # pacing for per-id detail calls
UA = {"User-Agent": "market-research bot (homelab)", "Accept": "application/json"}


def fetch_page(c, tag, page):
    """Return the list of article dicts for one (tag, page); [] on error or empty."""
    r = c.get(API, params={"per_page": PER_PAGE, "page": page, "tag": tag})
    if r.status_code == 429:  # rate-limited — back off and retry once
        time.sleep(5)
        r = c.get(API, params={"per_page": PER_PAGE, "page": page, "tag": tag})
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else []


def fetch_body(c, article_id):
    """Return body_markdown for a single article, or None if unavailable."""
    try:
        r = c.get(f"{API}/{article_id}")
        if r.status_code == 429:
            time.sleep(5)
            r = c.get(f"{API}/{article_id}")
        r.raise_for_status()
        return (r.json() or {}).get("body_markdown")
    except Exception as e:
        print("  detail err", article_id, repr(e)[:80])
        return None


def build_row(a, tag_seed, body_markdown=None):
    """Map a Dev.to article dict to a documents row tuple, or None if unusable."""
    aid = a.get("id")
    if not aid:
        return None
    title = a.get("title") or ""
    desc = a.get("description") or ""
    # body = title + description (or body_markdown when we fetched it)
    if body_markdown:
        body = f"{title}\n\n{body_markdown}".strip()
    else:
        body = f"{title}\n\n{desc}".strip()
    user = a.get("user") or {}
    meta = {
        "tags": a.get("tag_list") or a.get("tags"),
        "reactions": a.get("public_reactions_count", a.get("positive_reactions_count")),
        "comments_count": a.get("comments_count"),
        "reading_time": a.get("reading_time_minutes"),
        "published_at": a.get("published_at"),
        "user": {"username": user.get("username"), "name": user.get("name")},
        "tag_seed": tag_seed,
    }
    return (SLUG, str(aid), a.get("url"), title, body, Json(meta))


def run():
    conn = psycopg2.connect(PG_DSN)
    cur = conn.cursor()
    rows = []
    seen_ids = set()  # dedup article ids across tags/pages

    with httpx.Client(timeout=30, headers=UA, follow_redirects=True) as c:
        for tag in TAGS:
            tag = tag.strip()
            if not tag:
                continue
            for page in range(1, MAX_PAGES + 1):
                try:
                    articles = fetch_page(c, tag, page)
                except Exception as e:
                    print("  page err", tag, page, repr(e)[:80])
                    break  # stop paging this tag on error
                if not articles:
                    break  # no more pages for this tag
                for a in articles:
                    aid = a.get("id")
                    if not aid or aid in seen_ids:
                        continue
                    seen_ids.add(aid)
                    body_md = None
                    if FETCH_BODY:
                        body_md = fetch_body(c, aid)
                        time.sleep(DETAIL_DELAY)
                    row = build_row(a, tag, body_md)
                    if row:
                        rows.append(row)
                time.sleep(DELAY)  # be polite between page requests

    # Dedup rows on (source_slug, ext_id) in Python to avoid CardinalityViolation on upsert.
    uniq = {}
    for row in rows:
        uniq[(row[0], row[1])] = row
    rows = list(uniq.values())

    if rows:
        execute_values(cur, """INSERT INTO documents (source_slug, ext_id, url, title, body, metadata)
            VALUES %s ON CONFLICT (source_slug, ext_id) DO UPDATE
            SET title=EXCLUDED.title, body=EXCLUDED.body, metadata=EXCLUDED.metadata""", rows)
    conn.commit()
    n = len(rows)
    cur.close(); conn.close()
    print(f"devto: upserted {n} articles across {len([t for t in TAGS if t.strip()])} tags")


if __name__ == "__main__":
    run()
