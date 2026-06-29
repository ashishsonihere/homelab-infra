"""Atlassian Marketplace connector — FREE, no auth (public listing REST API).

SUPPLY-side map of the Jira/Confluence app ecosystem: who builds B2B add-ons, their
ratings + review counts (= category saturation + competitor signal). 1-3 star review
bodies reveal what existing paid tools fail at = wedge angles. httpx + psycopg2 only.

Endpoints (verified live 2026-06):
  - Listing:  GET https://marketplace.atlassian.com/rest/2/addons?offset=&limit=
              -> _embedded.addons[], paginate via _links.next.href (offset/limit)
              each addon embeds _embedded.reviews.{averageStars,count},
              _embedded.distribution.downloads, _links.vendor / _links.self.
  - Detail:   GET /rest/2/addons/{key}
              -> _embedded.categories[].name, _embedded.vendor.name, tags, summary.
  - Reviews:  GET /rest/2/addons/{key}/reviews?offset=&limit=
              -> _embedded.reviews[].{stars,review,date,_embedded.author.name}

Writes to the universal `documents` table (source_slug='atlassian_app').
Run:  python -m connectors.atlassian_marketplace
"""
import os
import time

import httpx
import psycopg2
from psycopg2.extras import Json, execute_values

PG_DSN = os.environ["PG_DSN"]
SLUG = "atlassian_app"
BASE = "https://marketplace.atlassian.com"

PAGE_LIMIT = int(os.environ.get("ATLASSIAN_PAGE_LIMIT", "50"))      # addons per listing page
MAX_ADDONS = int(os.environ.get("ATLASSIAN_MAX_ADDONS", "500"))     # hard cap on total addons
FETCH_DETAIL = os.environ.get("ATLASSIAN_FETCH_DETAIL", "1") == "1"  # 1 = pull categories/vendor per addon
REVIEW_BODIES = int(os.environ.get("ATLASSIAN_REVIEW_BODIES", "5"))  # # of recent review bodies to keep (0 = none)
DELAY = float(os.environ.get("ATLASSIAN_DELAY", "0.4"))            # politeness delay between requests (s)
APP_FILTER = os.environ.get("ATLASSIAN_APP_FILTER", "")            # optional: 'jira' or 'confluence' application filter
UA = {"User-Agent": "Mozilla/5.0 (market-research bot)", "Accept": "application/json"}


def _get(c, url):
    """GET an absolute or BASE-relative URL, return parsed JSON or None on failure."""
    if url.startswith("/"):
        url = BASE + url
    try:
        r = c.get(url)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print("  GET err", url[:90], repr(e)[:60])
        return None


def list_addons(c):
    """Paginate /rest/2/addons via _links.next; yield addon summary dicts up to MAX_ADDONS."""
    params = {"limit": PAGE_LIMIT, "offset": 0}
    if APP_FILTER:
        params["application"] = APP_FILTER
    url = BASE + "/rest/2/addons?" + "&".join(f"{k}={v}" for k, v in params.items())
    pulled = 0
    while url and pulled < MAX_ADDONS:
        data = _get(c, url)
        if not data:
            break
        addons = (data.get("_embedded", {}) or {}).get("addons", []) or []
        if not addons:
            break
        for a in addons:
            if pulled >= MAX_ADDONS:
                break
            yield a
            pulled += 1
        nxt = ((data.get("_links", {}) or {}).get("next") or [])
        # _links.next is a list (json + html variants); pick the JSON (rest/) href
        href = None
        if isinstance(nxt, dict):
            nxt = [nxt]
        for link in nxt:
            h = (link or {}).get("href", "")
            if "/rest/" in h:
                href = h
                break
        url = href
        time.sleep(DELAY)


def fetch_detail(c, key):
    """GET /rest/2/addons/{key}; return (categories list, vendor name, tags list, summary)."""
    data = _get(c, f"/rest/2/addons/{key}")
    if not data:
        return [], None, [], None
    emb = data.get("_embedded", {}) or {}
    cats = [cat.get("name") for cat in (emb.get("categories", []) or []) if cat.get("name")]
    vendor = (emb.get("vendor", {}) or {}).get("name")
    tags = [t.get("name") if isinstance(t, dict) else t for t in (data.get("tags", []) or [])]
    return cats, vendor, [t for t in tags if t], data.get("summary")


def fetch_reviews(c, key, n):
    """GET /rest/2/addons/{key}/reviews; return up to n {stars,text,author,date} dicts."""
    if n <= 0:
        return []
    data = _get(c, f"/rest/2/addons/{key}/reviews?offset=0&limit={n}")
    if not data:
        return []
    out = []
    for rv in ((data.get("_embedded", {}) or {}).get("reviews", []) or [])[:n]:
        author = ((rv.get("_embedded", {}) or {}).get("author", {}) or {}).get("name")
        out.append({
            "stars": rv.get("stars"),
            "text": rv.get("review"),
            "author": author,
            "date": rv.get("date"),
        })
    return out


def run():
    conn = psycopg2.connect(PG_DSN)
    cur = conn.cursor()
    rows = []
    seen = set()
    with httpx.Client(timeout=30, headers=UA, follow_redirects=True) as c:
        for a in list_addons(c):
            key = a.get("key")
            if not key or key in seen:
                continue
            seen.add(key)

            name = a.get("name") or key
            tagline = a.get("tagLine") or a.get("summary") or ""
            emb = a.get("_embedded", {}) or {}
            reviews_sum = emb.get("reviews", {}) or {}
            rating = reviews_sum.get("averageStars")
            review_count = reviews_sum.get("count")
            downloads = (emb.get("distribution", {}) or {}).get("downloads")
            links = a.get("_links", {}) or {}
            vendor_href = (links.get("vendor", {}) or {}).get("href")

            categories, vendor, tags, summary = [], None, [], None
            if FETCH_DETAIL:
                categories, vendor, tags, summary = fetch_detail(c, key)
                time.sleep(DELAY)

            review_samples = fetch_reviews(c, key, REVIEW_BODIES)
            if REVIEW_BODIES > 0:
                time.sleep(DELAY)

            body_parts = [name]
            if tagline:
                body_parts.append(tagline)
            if summary and summary != tagline:
                body_parts.append(summary)
            body = "\n\n".join(p for p in body_parts if p)

            url = f"{BASE}/apps/{key}"  # public app page

            rows.append((
                SLUG, key, url, name, body,
                Json({
                    "rating": rating,
                    "review_count": review_count,
                    "downloads": downloads,
                    "categories": categories,
                    "tags": tags,
                    "vendor": vendor,
                    "vendor_link": (BASE + vendor_href) if vendor_href else None,
                    "tagline": tagline,
                    "reviews_sample": review_samples,  # recent (incl. low-star) bodies = weakness signal
                }),
            ))

    # dedup on (source_slug, ext_id) within this batch before execute_values
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
    print(f"atlassian_app: upserted {n} addons")


if __name__ == "__main__":
    run()
