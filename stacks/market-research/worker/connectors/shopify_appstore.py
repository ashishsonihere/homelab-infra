"""Shopify App Store connector — SUPPLY-SIDE competitive map (the layer that measures saturation).

Why: every ecommerce-tool category's saturation is measurable here — # of apps, each app's rating +
review_count (traction/age proxy), price, and category. This is how we replace "I think it's saturated"
with "here are the N apps serving this, their ratings, and the gap."

How: enumerate all apps via the public sitemap (no JS needed), then parse each app page's
JSON-LD `SoftwareApplication` block (clean structured data, no fragile selectors, datacenter-IP OK).
Writes to the universal `documents` table (source_slug='shopify_app'). Resumable (skips collected
handles), idempotent upsert, per-app error isolation, polite delay. httpx + psycopg2 only.

Run:  python -m connectors.shopify_appstore     (env: SHOPIFY_MAX, SHOPIFY_DELAY)
"""
import os
import re
import json
import time
import httpx
import psycopg2
from psycopg2.extras import Json, execute_values

PG_DSN = os.environ["PG_DSN"]
SLUG = "shopify_app"
MAX = int(os.environ.get("SHOPIFY_MAX", "400"))
DELAY = float(os.environ.get("SHOPIFY_DELAY", "0.5"))
SITEMAP = "https://apps.shopify.com/sitemap.xml"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124 Safari/537.36"}
LD_RE = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.S)
LOC_RE = re.compile(r'<loc>([^<]+)</loc>')
APP_URL_RE = re.compile(r'^https://apps\.shopify\.com/[a-z0-9][a-z0-9-]+$')


def app_urls(c):
    idx = c.get(SITEMAP)
    idx.raise_for_status()
    children = [x for x in LOC_RE.findall(idx.text) if "apps_en" in x] or LOC_RE.findall(idx.text)
    urls = []
    for sm in children:
        try:
            r = c.get(sm, timeout=60)
            r.raise_for_status()
            urls += LOC_RE.findall(r.text)
        except Exception as e:
            print("  sitemap err", sm.split('/')[-1], repr(e)[:50])
    return [u for u in urls if APP_URL_RE.match(u)]


def _get(c, url, tries=3):
    """Fetch with retry/backoff — Shopify throttles datacenter IPs, so retry 429/403/503."""
    for i in range(tries):
        try:
            r = c.get(url)
            if r.status_code == 200:
                return r
            if r.status_code in (429, 403, 503):
                time.sleep(2 * (i + 1)); continue
            return r
        except Exception:
            time.sleep(1.5 * (i + 1))
    return None


def parse_app(url, html):
    data = None
    for b in LD_RE.findall(html):
        try:
            j = json.loads(b)
        except Exception:
            continue
        if isinstance(j, dict) and j.get("@type") == "SoftwareApplication":
            data = j
            break
    if not data:
        return None
    handle = url.rstrip('/').split('/')[-1]
    agg = data.get("aggregateRating") or {}
    offers = data.get("offers") or {}
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    name = (data.get("name") or handle)[:300]
    desc = data.get("description") or ""
    rating = agg.get("ratingValue")
    rcount = agg.get("ratingCount") or agg.get("reviewCount")
    cat = data.get("applicationCategory") or data.get("applicationSubCategory")
    price = offers.get("price")
    cur = offers.get("priceCurrency")
    body = f"{name}. {desc} [rating={rating}, reviews={rcount}, price={price} {cur}, category={cat}]"
    return (SLUG, handle, url, name, body,
            Json({"rating": rating, "review_count": rcount, "price": price,
                  "currency": cur, "category": cat}))


def _flush(cur, rows):
    uniq = {}
    for r in rows:
        uniq[(r[0], r[1])] = r
    execute_values(cur, """INSERT INTO documents (source_slug, ext_id, url, title, body, metadata)
        VALUES %s ON CONFLICT (source_slug, ext_id) DO UPDATE
        SET title=EXCLUDED.title, body=EXCLUDED.body, metadata=EXCLUDED.metadata""", list(uniq.values()))


def run():
    conn = psycopg2.connect(PG_DSN)
    cur = conn.cursor()
    cur.execute("SELECT ext_id FROM documents WHERE source_slug=%s", (SLUG,))
    seen = {r[0] for r in cur.fetchall()}
    rows, processed, errs, saved = [], 0, 0, 0
    with httpx.Client(timeout=30, headers=UA, follow_redirects=True) as c:
        urls = app_urls(c)
        todo = [u for u in urls if u.rstrip('/').split('/')[-1] not in seen][:MAX]
        print(f"sitemap: {len(urls)} apps total; {len(seen)} already collected; fetching {len(todo)}")
        for u in todo:
            r = _get(c, u)
            if r is not None and r.status_code == 200:
                row = parse_app(u, r.text)
                if row:
                    rows.append(row)
            else:
                errs += 1
            processed += 1
            if len(rows) >= 100:
                _flush(cur, rows); conn.commit(); saved += len(rows); rows = []
            time.sleep(DELAY)
    if rows:
        _flush(cur, rows); conn.commit(); saved += len(rows)
    cur.close(); conn.close()
    print(f"shopify_app: processed {processed}, saved {saved}, errors {errs}")


if __name__ == "__main__":
    run()
