"""Google Play reviews connector — unofficial internal endpoint via `google-play-scraper`, no API key.

SOLUTION-mapping: find apps by keyword (Play search) -> pull newest reviews. 1-3 star reviews
reveal what existing tools fail at = wedge angles. The library hits Google Play's own
`batchexecute` JSON endpoint over plain HTTP (no Selenium, no key). See Scraping-Playbook.md 2.8.
Writes to the universal `documents` table (source_slug='google_play_reviews'). psycopg2 only.

Library: google-play-scraper (JoMingyu fork), verified against v1.2.7.
  search(query, lang, country, n_hits) -> list[dict]  (each has appId, title, ...)
  reviews(app_id, lang, country, sort, count, continuation_token) -> (list[dict], token)
  review dict keys: reviewId, userName, content, score, thumbsUpCount,
                    reviewCreatedVersion, at (datetime), appVersion, ...
  Sort enum: Sort.NEWEST, Sort.MOST_RELEVANT

Proxies: the library has no built-in proxy arg, but it uses urllib under the hood and honors
the HTTP_PROXY / HTTPS_PROXY env vars. For volume, a Webshare datacenter proxy from
/opt/market-research/proxies.txt (format host:port:user:pass) can be exported as
HTTPS_PROXY=http://user:pass@host:port before running, rotating per run. Try direct first —
the endpoint only throttles per-IP under heavy load, and modest volume here works fine.

Run:  python -m connectors.google_play
"""
import os
import time
import psycopg2
from psycopg2.extras import Json, execute_values
from google_play_scraper import search, reviews, Sort

PG_DSN = os.environ["PG_DSN"]
SLUG = "google_play_reviews"
LANG = os.environ.get("GP_LANG", "en")
COUNTRY = os.environ.get("GP_COUNTRY", "us")
KEYWORDS = os.environ.get(
    "GP_KEYWORDS",
    "shopify,ecommerce,inventory,dropshipping,email marketing,seo,google ads,"
    "facebook ads,invoicing,accounting,crm,helpdesk,analytics"
).split(",")
APPS_PER_KW = int(os.environ.get("GP_APPS_PER_KW", "10"))
REVIEWS_PER_APP = int(os.environ.get("GP_REVIEWS_PER_APP", "150"))
MAX_APPS = int(os.environ.get("GP_MAX_APPS", "120"))   # global cap to keep a run sane
DELAY = float(os.environ.get("GP_DELAY", "2.0"))       # polite per-app pacing (seconds)


def search_apps(term):
    """Return list of (app_id, title) for a keyword; capped at APPS_PER_KW."""
    hits = search(term, lang=LANG, country=COUNTRY, n_hits=APPS_PER_KW)
    return [(a["appId"], a.get("title")) for a in hits if a.get("appId")]


def fetch_reviews(app_id):
    """Return up to REVIEWS_PER_APP newest reviews for an app."""
    result, _token = reviews(
        app_id,
        lang=LANG,
        country=COUNTRY,
        sort=Sort.NEWEST,
        count=REVIEWS_PER_APP,
    )
    return result or []


def run():
    conn = psycopg2.connect(PG_DSN)
    cur = conn.cursor()
    rows = []
    seen_apps = set()  # dedup app ids across keywords

    for kw in KEYWORDS:
        kw = kw.strip()
        if not kw:
            continue
        try:
            apps = search_apps(kw)
        except Exception as e:
            print("  search err", kw, repr(e)[:80]); continue

        for app_id, app_name in apps:
            if app_id in seen_apps:
                continue
            if len(seen_apps) >= MAX_APPS:
                break
            seen_apps.add(app_id)

            url = f"https://play.google.com/store/apps/details?id={app_id}"
            try:
                revs = fetch_reviews(app_id)
            except Exception as e:
                print("  reviews err", app_id, repr(e)[:80]); continue

            for r in revs:
                rid = r.get("reviewId")
                if not rid:
                    continue
                body = r.get("content")
                if not body:
                    continue  # nothing to mine
                title = (r.get("title")  # newer responses may not carry a title
                         or (body[:60].strip() + ("..." if len(body) > 60 else "")))
                at = r.get("at")
                rows.append((
                    SLUG,
                    str(rid),
                    url,
                    title,
                    body,
                    Json({
                        "app": app_name,
                        "app_id": app_id,
                        "score": r.get("score"),
                        "thumbs_up": r.get("thumbsUpCount"),
                        "version": r.get("reviewCreatedVersion") or r.get("appVersion"),
                        "at": str(at) if at is not None else None,
                        "keyword": kw,
                    }),
                ))
            time.sleep(DELAY)  # be polite to the per-IP-throttled endpoint
        if len(seen_apps) >= MAX_APPS:
            break

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
    print(f"google_play_reviews: upserted {n} reviews from {len(seen_apps)} apps")


if __name__ == "__main__":
    run()
