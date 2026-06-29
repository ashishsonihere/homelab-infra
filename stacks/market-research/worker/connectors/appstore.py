"""Apple App Store reviews — COMPREHENSIVE for relevant categories (not a keyword sample).

Seeds app IDs from (a) genre TOP-CHARTS (Business/Productivity/Finance/Shopping/Utilities × free/paid/grossing
× several countries) AND (b) keyword search (catches niche tools), dedups, then pulls recent reviews per app.
FREE, no key, no proxy. Writes documents(source_slug='appstore_reviews'). Run: python -m connectors.appstore
"""
import os
import httpx
import psycopg2
from psycopg2.extras import Json, execute_values

PG_DSN = os.environ["PG_DSN"]
COUNTRY = os.environ.get("APPSTORE_COUNTRY", "us")
COUNTRIES = os.environ.get("APPSTORE_COUNTRIES", "us,gb,in,ca,au").split(",")
GENRES = os.environ.get("APPSTORE_GENRES", "6000,6007,6015,6024,6002").split(",")  # Business,Productivity,Finance,Shopping,Utilities
CHARTS = os.environ.get("APPSTORE_CHARTS", "topfreeapplications,topgrossingapplications,toppaidapplications").split(",")
KEYWORDS = os.environ.get("APPSTORE_KEYWORDS",
    "shopify,ecommerce,inventory management,amazon seller,dropshipping,email marketing,seo,crm,"
    "helpdesk,analytics,invoicing,accounting,social media,subscriptions").split(",")
APPS_PER_KW = int(os.environ.get("APPSTORE_APPS_PER_KW", "25"))
REVIEW_PAGES = int(os.environ.get("APPSTORE_REVIEW_PAGES", "3"))
SLUG = "appstore_reviews"
UA = {"User-Agent": "Mozilla/5.0 (market-research bot)"}


def search_apps(c, term):
    r = c.get("https://itunes.apple.com/search",
              params={"term": term, "country": COUNTRY, "entity": "software", "limit": APPS_PER_KW})
    r.raise_for_status()
    return [(str(a["trackId"]), a.get("trackName"), a.get("primaryGenreName"))
            for a in r.json().get("results", []) if a.get("trackId")]


def genre_app_ids(c, country, genre, chart):
    url = f"https://itunes.apple.com/{country}/rss/{chart}/limit=200/genre={genre}/json"
    try:
        r = c.get(url); r.raise_for_status()
        entries = r.json().get("feed", {}).get("entry", [])
    except Exception:
        return []
    if isinstance(entries, dict):
        entries = [entries]
    out = []
    for e in entries:
        aid = ((e.get("id", {}) or {}).get("attributes", {}) or {}).get("im:id")
        name = (e.get("im:name", {}) or {}).get("label")
        cat = (((e.get("category", {}) or {}).get("attributes", {}) or {}).get("label"))
        if aid:
            out.append((str(aid), name, cat))
    return out


def fetch_reviews(c, app_id):
    out = []
    for page in range(1, REVIEW_PAGES + 1):
        url = f"https://itunes.apple.com/{COUNTRY}/rss/customerreviews/page={page}/id={app_id}/sortby=mostrecent/json"
        try:
            r = c.get(url); r.raise_for_status()
            entries = r.json().get("feed", {}).get("entry", [])
        except Exception:
            break
        if isinstance(entries, dict):
            entries = [entries]
        if not entries:
            break
        for e in entries:
            if isinstance(e, dict) and "im:rating" in e:
                out.append(e)
    return out


def _flush(cur, rows):
    uniq = {}
    for r in rows:
        uniq[(r[0], r[1])] = r
    execute_values(cur, """INSERT INTO documents (source_slug,ext_id,url,title,body,metadata)
        VALUES %s ON CONFLICT (source_slug,ext_id) DO UPDATE
        SET title=EXCLUDED.title, body=EXCLUDED.body, metadata=EXCLUDED.metadata""", list(uniq.values()))


def run():
    conn = psycopg2.connect(PG_DSN)
    cur = conn.cursor()
    apps = {}  # app_id -> (name, genre)
    rows = []
    with httpx.Client(timeout=30, headers=UA, follow_redirects=True) as c:
        for country in [x.strip() for x in COUNTRIES if x.strip()]:
            for genre in [x.strip() for x in GENRES if x.strip()]:
                for chart in [x.strip() for x in CHARTS if x.strip()]:
                    for aid, name, cat in genre_app_ids(c, country, genre, chart):
                        apps.setdefault(aid, (name, cat))
        for kw in [x.strip() for x in KEYWORDS if x.strip()]:
            try:
                for aid, name, cat in search_apps(c, kw):
                    apps.setdefault(aid, (name, cat))
            except Exception as e:
                print("  search err", kw, repr(e)[:50])
        print(f"appstore: {len(apps)} unique apps discovered; fetching reviews", flush=True)
        for i, (aid, (name, cat)) in enumerate(apps.items()):
            for e in fetch_reviews(c, aid):
                rid = (e.get("id", {}) or {}).get("label")
                if not rid:
                    continue
                rows.append((SLUG, str(rid), f"https://apps.apple.com/{COUNTRY}/app/id{aid}",
                             (e.get("title", {}) or {}).get("label"), (e.get("content", {}) or {}).get("label"),
                             Json({"app": name, "app_id": aid, "genre": cat,
                                   "rating": (e.get("im:rating", {}) or {}).get("label"),
                                   "version": (e.get("im:version", {}) or {}).get("label"),
                                   "author": ((e.get("author", {}) or {}).get("name", {}) or {}).get("label")})))
            if i % 200 == 0 and rows:
                _flush(cur, rows); conn.commit(); rows = []
    if rows:
        _flush(cur, rows); conn.commit()
    cur.close(); conn.close()
    print(f"appstore_reviews: done, {len(apps)} apps", flush=True)


if __name__ == "__main__":
    run()
