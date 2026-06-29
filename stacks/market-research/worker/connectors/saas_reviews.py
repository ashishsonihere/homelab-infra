"""SaaS review scraper — TrustRadius (primary), SaaSHub, FeaturedCustomers.

WHY: G2 and Capterra are behind Cloudflare managed challenge (Turnstile) that curl_cffi
cannot pass. TrustRadius is the closest G2 competitor with comparable review depth
(pros/cons, 8-10 ratings, reviewer role/company, ROI) and is fully accessible.
SaaSHub gives alternative-product mapping. FeaturedCustomers gives case studies.

DATA PATH:
  TrustRadius: sitemap_productreview.xml -> /products/{slug}/reviews pages
               Each page has clean JSON-LD (schema.org SoftwareApplication + Review[])
               with aggregateRating, reviewBody, author, datePublished, ratingValue.
  SaaSHub:     /{slug} pages — HTML parsing for alternatives + ratings.
  FeaturedCustomers: sitemap_index.xml -> /vendor/{Name} pages — testimonials + ratings.

WRITES:
  Products -> saas_products (source='trustradius', UNIQUE(source, ext_id))
  Reviews  -> documents (source_slug='trustradius_review', metadata.product_ext_id links back)

Conventions: psycopg2 + execute_values, idempotent ON CONFLICT, env-driven, PG_DSN from env,
memory-safe streaming (flush per page, never hold whole site in RAM). curl_cffi with
impersonate='chrome' THROUGH SCRAPE_PROXY (defaults to YT_PROXY).

Run:  python -m connectors.saas_reviews
Env:  PG_DSN (required), SCRAPE_PROXY (defaults to YT_PROXY),
      SAAS_SOURCE (trustradius|saashub|featuredcustomers, default trustradius),
      SAAS_CATEGORIES (comma-sep, default: crm,marketing,ecommerce,analytics,project-management),
      SAAS_MAX_PRODUCTS, SAAS_MAX_REVIEWS_PER_PRODUCT, SAAS_DELAY
"""
import os
import re
import json
import time

import psycopg2
from psycopg2.extras import Json, execute_values

try:
    from curl_cffi import requests as cffi_requests
    _HAS_CFFI = True
except Exception:
    _HAS_CFFI = False
    import httpx

PG_DSN = os.environ["PG_DSN"]
PROXY = os.environ.get("SCRAPE_PROXY") or os.environ.get("YT_PROXY", "")
PROXIES = {"http": PROXY, "https": PROXY} if PROXY else None
SOURCE = os.environ.get("SAAS_SOURCE", "trustradius")
CATEGORIES = os.environ.get("SAAS_CATEGORIES", "crm,marketing,ecommerce,analytics,project-management,ai-tools").split(",")
MAX_PRODUCTS = int(os.environ.get("SAAS_MAX_PRODUCTS", "50"))
MAX_REVIEWS = int(os.environ.get("SAAS_MAX_REVIEWS_PER_PRODUCT", "50"))
DELAY = float(os.environ.get("SAAS_DELAY", "1.5"))
IMPERSONATE = os.environ.get("SAAS_IMPERSONATE", "chrome")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9",
           "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}

LD_RE = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.S)
LOC_RE = re.compile(r'<loc>([^<]+)</loc>')


# --------------------------------------------------------------------------- HTTP

def _get(url, tries=4):
    """Fetch HTML with retry/backoff. Returns response text on 200, None on failure."""
    for i in range(tries):
        try:
            if _HAS_CFFI:
                r = cffi_requests.get(url, headers=HEADERS, impersonate=IMPERSONATE,
                                      proxies=PROXIES, timeout=45)
                status, text = r.status_code, r.text
            else:
                with httpx.Client(timeout=45, headers=HEADERS, follow_redirects=True,
                                  proxy=PROXY) as c:
                    resp = c.get(url)
                    status, text = resp.status_code, resp.text
            if status == 200:
                return text
            if status in (429, 403, 503):
                if "Just a moment" in text or "Please enable JS" in text:
                    print(f"  CF_BLOCKED: {url} (status {status})", flush=True)
                    return None
                time.sleep(3 * (i + 1))
                continue
            return None
        except Exception:
            time.sleep(2 * (i + 1))
    return None


# --------------------------------------------------------------------------- TrustRadius

TR_BASE = "https://www.trustradius.com"
TR_SITEMAP = f"{TR_BASE}/sitemap_index.xml"


def tr_discover_products():
    """Walk sitemap_productreview.xml to discover product review page URLs."""
    idx_html = _get(TR_SITEMAP)
    if not idx_html:
        print("  TR sitemap index fetch failed", flush=True)
        return []
    shards = [s for s in LOC_RE.findall(idx_html) if "productreview" in s.lower()]
    print(f"  TR sitemap: {len(shards)} review shards", flush=True)
    urls = []
    for sm in shards[:3]:
        html = _get(sm)
        if not html:
            continue
        found = [u for u in LOC_RE.findall(html) if "/products/" in u and "/reviews" in u]
        urls.extend(found)
        time.sleep(DELAY)
    return urls


def tr_parse_product_page(url, html):
    """Parse a TrustRadius /products/{slug}/reviews page via JSON-LD.

    Returns (product_row, [review_rows]) or (None, []).
    """
    slug = url.rstrip("/").split("/")[-2] if url.endswith("/reviews") else url.rstrip("/").split("/")[-1]
    blocks = LD_RE.findall(html)
    product_row = None
    review_rows = []

    for block in blocks:
        try:
            data = json.loads(block)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        if data.get("@type") != "SoftwareApplication":
            continue

        agg = data.get("aggregateRating") or {}
        offers = data.get("offers") or {}
        if isinstance(offers, list):
            offers = offers[0] if offers else {}
        name = (data.get("name") or slug)[:300]
        desc = data.get("description") or ""
        rating = agg.get("ratingValue")
        rcount = agg.get("ratingCount") or agg.get("reviewCount")
        category = data.get("applicationCategory") or ""
        pricing_text = ""
        price = offers.get("price")
        if price is not None:
            pricing_text = f"{price} {offers.get('priceCurrency', 'USD')}"
        vendor = ""
        offered_by = offers.get("offeredBy") or {}
        if isinstance(offered_by, dict):
            vendor = offered_by.get("name", "") or ""

        product_row = (
            SOURCE, slug, name, vendor, category,
            "", pricing_text, Json({"price": price, "currency": offers.get("priceCurrency")}),
            rating, int(rcount) if rcount else None, desc[:5000],
            Json({"url": url, "awards": data.get("award", []),
                  "features": data.get("featureList", [])[:20]})
        )

        reviews = data.get("review") or []
        if isinstance(reviews, dict):
            reviews = [reviews]
        for rv in reviews[:MAX_REVIEWS]:
            if not isinstance(rv, dict):
                continue
            author = (rv.get("author") or {}).get("name", "") if isinstance(rv.get("author"), dict) else ""
            rr = rv.get("reviewRating") or {}
            rating_val = rr.get("ratingValue") if isinstance(rr, dict) else None
            body = rv.get("reviewBody") or ""
            title = rv.get("name") or ""
            date_pub = rv.get("datePublished") or ""
            review_id = f"{slug}-{date_pub}-{author[:30]}".replace(" ", "-").lower()
            review_rows.append((
                "trustradius_review", review_id, url, title, body,
                Json({"product_ext_id": slug, "product_name": name, "rating": rating_val,
                      "reviewer": author, "date": date_pub, "source": "trustradius"})
            ))
        break

    return product_row, review_rows


# --------------------------------------------------------------------------- DB

def _flush_products(cur, rows):
    if not rows:
        return 0
    execute_values(cur, """INSERT INTO saas_products
        (source, ext_id, name, vendor, category, website, pricing_text, pricing,
         rating, n_reviews, description, metadata)
        VALUES %s ON CONFLICT (source, ext_id) DO UPDATE SET
        name=EXCLUDED.name, vendor=EXCLUDED.vendor, category=EXCLUDED.category,
        pricing_text=EXCLUDED.pricing_text, pricing=EXCLUDED.pricing,
        rating=EXCLUDED.rating, n_reviews=EXCLUDED.n_reviews,
        description=EXCLUDED.description, metadata=EXCLUDED.metadata,
        scraped_at=now()""", rows)
    return len(rows)


def _flush_reviews(cur, rows):
    if not rows:
        return 0
    uniq = {}
    for r in rows:
        uniq[(r[0], r[1])] = r
    execute_values(cur, """INSERT INTO documents (source_slug, ext_id, url, title, body, metadata)
        VALUES %s ON CONFLICT (source_slug, ext_id) DO UPDATE
        SET title=EXCLUDED.title, body=EXCLUDED.body, metadata=EXCLUDED.metadata""",
                   list(uniq.values()))
    return len(uniq)


# --------------------------------------------------------------------------- run

def run():
    conn = psycopg2.connect(PG_DSN)
    cur = conn.cursor()

    # Apply schema if needed (may lack CREATE privileges if already applied by admin)
    schema_path = os.path.join(os.path.dirname(__file__), "saas_schema.sql")
    if os.path.exists(schema_path):
        try:
            cur.execute(open(schema_path).read())
            conn.commit()
        except Exception:
            conn.rollback()  # table already exists or no DDL privileges — OK

    # Resume: skip products already collected
    cur.execute("SELECT ext_id FROM saas_products WHERE source=%s", (SOURCE,))
    seen_products = {r[0] for r in cur.fetchall()}
    cur.execute("SELECT ext_id FROM documents WHERE source_slug=%s", (f"{SOURCE}_review",))
    seen_reviews = {r[0] for r in cur.fetchall()}

    if not _HAS_CFFI:
        print("WARN: curl_cffi not importable; falling back to httpx", flush=True)

    if SOURCE == "trustradius":
        _run_trustradius(cur, conn, seen_products, seen_reviews)
    else:
        print(f"Source '{SOURCE}' not yet implemented. Use 'trustradius'.", flush=True)

    cur.close()
    conn.close()


def _run_trustradius(cur, conn, seen_products, seen_reviews):
    product_urls = tr_discover_products()
    todo = [u for u in product_urls if u.rstrip("/").split("/")[-1] not in seen_products][:MAX_PRODUCTS]
    print(f"TR: {len(product_urls)} products discovered; {len(seen_products)} already collected; "
          f"fetching {len(todo)}", flush=True)

    prod_rows, review_rows = [], []
    processed, errs, saved_p, saved_r = 0, 0, 0, 0

    for url in todo:
        html = _get(url)
        if not html:
            errs += 1
            processed += 1
            time.sleep(DELAY)
            continue
        prow, rrows = tr_parse_product_page(url, html)
        if prow:
            prod_rows.append(prow)
        new_reviews = [r for r in rrows if r[1] not in seen_reviews]
        review_rows.extend(new_reviews)
        seen_reviews.update(r[1] for r in new_reviews)

        if len(prod_rows) >= 10:
            saved_p += _flush_products(cur, prod_rows)
            conn.commit()
            prod_rows = []
        if len(review_rows) >= 50:
            saved_r += _flush_reviews(cur, review_rows)
            conn.commit()
            review_rows = []

        processed += 1
        if processed % 5 == 0:
            print(f"  TR: processed {processed}/{len(todo)}, products={saved_p + len(prod_rows)}, "
                  f"reviews={saved_r + len(review_rows)}", flush=True)
        time.sleep(DELAY)

    if prod_rows:
        saved_p += _flush_products(cur, prod_rows)
        conn.commit()
    if review_rows:
        saved_r += _flush_reviews(cur, review_rows)
        conn.commit()

    print(f"trustradius: processed {processed}, products={saved_p}, reviews={saved_r}, errors={errs}",
          flush=True)


if __name__ == "__main__":
    run()
