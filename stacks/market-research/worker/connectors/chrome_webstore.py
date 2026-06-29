"""Chrome Web Store connector — SUPPLY-SIDE competitive map of browser extensions.

WHY: same job as shopify_appstore.py but for the browser-extension surface. For each
ecommerce / marketing / productivity niche we want: who builds tools here and their traction
(install count + rating + rating_count). That turns "is this saturated?" into "here are the N
extensions serving this query, their users/ratings, and the gap."

DATA PATH (verified live, June 2026 — NOT guessed):
  Store lives at chromewebstore.google.com (the old chrome.google.com/webstore is gone).
  The site is a heavy SPA, BUT both the search results page and the per-extension detail page
  are SERVER-RENDERED — the data we need is in the initial HTML, so NO Playwright/JS is needed.
    * Seed:    GET /search/<query>      -> SSR HTML contains /detail/<slug>/<EXT_ID> links
               (first page only, ~10-20 ids per query). Good enough for a targeted competitive
               map; we seed many queries to get breadth.
    * Enrich:  GET /detail/<slug>/<EXT_ID>  -> SSR HTML with the fields below.
  Stable anchors used (the CSS class names like Vq0ZA/xJEoWe are obfuscated and ROTATE on every
  Google build, so we deliberately do NOT key off them — we key off semantic, durable markers):
    * name        <meta property="og:title"> minus the " - Chrome Web Store" suffix
    * description <meta property="og:description"> (also name+desc = body)
    * rating      aria-label="Average rating 4.1 out of 5 stars."  /  "4.1 out of 5 stars"
    * rating_cnt  visible text "<N> ratings"  (e.g. "1.1K ratings" -> 1100)
    * users       visible text "<N> users"    (e.g. "800,000 users" -> 800000)
    * category    anchor href="./category/extensions/<cat>/<sub>" with the human label as text
  There is also an internal batchexecute RPC, but it's brittle/undocumented and the SSR HTML is
  cleaner and more stable, so we use the HTML.

DIFFICULTY: MEDIUM. SSR => httpx/selectolax is enough (no Playwright). The real friction is
  (1) Google throttles datacenter IPs hard -> we use curl_cffi with a Chrome TLS/JA3 fingerprint
      and back off on 429/403/503; (2) obfuscated classes -> we anchor on og tags / aria / text;
  (3) the full sitemap has ~310k extensions (33 shards) which is the WRONG scope for a competitive
      map, so we seed by keyword search instead. (Sitemap shard parsing is included but OFF by
      default — flip CHROME_USE_SITEMAP=1 if you ever want a broad sweep.)

Writes to the universal `documents` table (source_slug='chrome_extension'). Resumable (skips
collected ext_ids), idempotent upsert, per-item error isolation, polite delay + retry/backoff.

Run:  python -m connectors.chrome_webstore
Env:  PG_DSN (required), CHROME_QUERIES, CHROME_MAX, CHROME_DELAY, CHROME_USE_SITEMAP,
      CHROME_SITEMAP_MAX, CHROME_IMPERSONATE
Image deps used: curl_cffi (preferred), httpx (fallback), selectolax. psycopg2 for the DB.
"""
import os
import re
import time

import psycopg2
from psycopg2.extras import Json, execute_values
from selectolax.parser import HTMLParser

# curl_cffi gives us a real Chrome TLS fingerprint, which Google's edge is far more likely to
# serve than a bare httpx client. httpx is kept as a graceful fallback so the file still imports
# and runs if curl_cffi is ever missing.
try:
    from curl_cffi import requests as cffi_requests  # type: ignore
    _HAS_CFFI = True
except Exception:  # pragma: no cover - fallback path
    _HAS_CFFI = False
    import httpx

PG_DSN = os.environ["PG_DSN"]
SLUG = "chrome_extension"

BASE = "https://chromewebstore.google.com"

# Seed queries: ecommerce / marketing / productivity — the niches we care about. Each query's
# SSR search page yields ~10-20 extension ids; together this gives a broad competitive map.
QUERIES = os.environ.get(
    "CHROME_QUERIES",
    "ecommerce,shopify,dropshipping,amazon seller,product research,price tracker,"
    "coupon,affiliate marketing,email marketing,seo,keyword research,google ads,"
    "facebook ads,social media scheduler,linkedin automation,lead generation,crm,"
    "screen recorder,screenshot,grammar checker,ai writer,chatgpt,productivity,"
    "tab manager,note taking,time tracking,calendar,proxy,web scraper,analytics",
).split(",")

MAX = int(os.environ.get("CHROME_MAX", "400"))            # cap on detail pages fetched per run
DELAY = float(os.environ.get("CHROME_DELAY", "1.0"))      # polite delay between detail fetches
IMPERSONATE = os.environ.get("CHROME_IMPERSONATE", "chrome")  # curl_cffi browser profile

USE_SITEMAP = os.environ.get("CHROME_USE_SITEMAP", "0") == "1"
SITEMAP_MAX = int(os.environ.get("CHROME_SITEMAP_MAX", "2000"))

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9",
           "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}

# Extension ids are 32 chars, a-p only. This matches /detail/<slug>/<id> anywhere in HTML.
ID_RE = re.compile(r"/detail/[a-z0-9._-]+/([a-p]{32})")
LOC_RE = re.compile(r"<loc>([^<]+)</loc>")

# --- detail-page field patterns (anchored on stable markers, not obfuscated classes) ---
OG_TITLE_RE = re.compile(r'<meta\s+property="og:title"\s+content="([^"]*)"', re.I)
OG_DESC_RE = re.compile(r'<meta\s+property="og:description"\s+content="([^"]*)"', re.I)
META_DESC_RE = re.compile(r'<meta\s+name="description"\s+content="([^"]*)"', re.I)
# "Average rating 4.1 out of 5 stars."  or  "4.1 out of 5 stars"
RATING_RE = re.compile(r'(\d(?:\.\d)?)\s*out of\s*5\s*stars', re.I)
RATING_CNT_RE = re.compile(r'([0-9][0-9.,]*\s*[KMB]?)\s*ratings?\b', re.I)
USERS_RE = re.compile(r'([0-9][0-9.,]*\s*[KMB]?\+?)\s*users\b', re.I)
# category anchor: href="./category/extensions/productivity/workflow" ... >Workflow &amp; Planning</a>
CATEGORY_RE = re.compile(
    r'href="\.?/?category/extensions/[^"]*"[^>]*>([^<]+)</a>', re.I)

TITLE_SUFFIX = " - Chrome Web Store"


# --------------------------------------------------------------------------- HTTP

def _get(url, tries=4):
    """Fetch HTML with retry/backoff. Google throttles datacenter IPs, so back off on 429/403/503.

    Returns the response text on HTTP 200, else None.
    """
    for i in range(tries):
        try:
            if _HAS_CFFI:
                r = cffi_requests.get(url, headers=HEADERS, timeout=40,
                                      impersonate=IMPERSONATE)
                status = r.status_code
                text = r.text
            else:  # httpx fallback
                with httpx.Client(timeout=40, headers=HEADERS, follow_redirects=True) as c:
                    resp = c.get(url)
                    status = resp.status_code
                    text = resp.text
            if status == 200:
                return text
            if status in (429, 403, 503):
                time.sleep(3 * (i + 1))      # escalating backoff on throttle/block
                continue
            return None                       # 404 etc. — don't retry
        except Exception:
            time.sleep(2 * (i + 1))
    return None


# --------------------------------------------------------------------------- seeding

def seed_ids_from_search():
    """Run each query against the SSR /search/<q> page and collect extension ids."""
    seeds = {}  # ext_id -> a detail url to fetch (we rebuild canonical url from id later anyway)
    for q in QUERIES:
        q = q.strip()
        if not q:
            continue
        url = f"{BASE}/search/{q.replace(' ', '%20')}"
        html = _get(url)
        if not html:
            print("  search err", q)
            continue
        found = 0
        for ext_id in ID_RE.findall(html):
            if ext_id not in seeds:
                seeds[ext_id] = q       # remember which query surfaced it (a saturation signal)
                found += 1
        print(f"  search '{q}': +{found} new ids ({len(seeds)} total)")
        time.sleep(DELAY)
    return seeds


def seed_ids_from_sitemap():
    """Optional broad sweep: walk sitemap shards and harvest detail ids. OFF by default."""
    idx = _get(f"{BASE}/sitemap")
    if not idx:
        print("  sitemap index fetch failed")
        return {}
    shards = LOC_RE.findall(idx)
    print(f"  sitemap index: {len(shards)} shards")
    seeds = {}
    for sm in shards:
        html = _get(sm)
        if not html:
            continue
        for u in LOC_RE.findall(html):
            m = ID_RE.search(u) or re.search(r"/([a-p]{32})$", u)
            if m:
                seeds[m.group(1)] = "sitemap"
                if len(seeds) >= SITEMAP_MAX:
                    return seeds
        time.sleep(DELAY)
    return seeds


# --------------------------------------------------------------------------- parsing

def _to_int(s):
    """'800,000' -> 800000 ; '1.1K' -> 1100 ; '2M' -> 2000000."""
    if not s:
        return None
    s = s.strip().replace(",", "").replace("+", "").upper()
    mult = 1
    if s.endswith("K"):
        mult, s = 1_000, s[:-1]
    elif s.endswith("M"):
        mult, s = 1_000_000, s[:-1]
    elif s.endswith("B"):
        mult, s = 1_000_000_000, s[:-1]
    try:
        return int(round(float(s) * mult))
    except ValueError:
        return None


def _unescape(s):
    if not s:
        return s
    return (s.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
             .replace("&quot;", '"').replace("&#39;", "'").replace("&#x27;", "'"))


def parse_detail(ext_id, url, html):
    """Extract (row) tuple from a detail page's SSR HTML, or None if it doesn't look valid.

    Uses selectolax to drop scripts/styles before scanning visible text (so an obfuscated CSS
    rule like '...ratings...' inside a <style> can't poison the 'N ratings' match), while pulling
    name/description straight from the <meta> tags in the raw HTML.
    """
    name = None
    m = OG_TITLE_RE.search(html)
    if m:
        name = _unescape(m.group(1))
        if name.endswith(TITLE_SUFFIX):
            name = name[: -len(TITLE_SUFFIX)]
    desc = ""
    m = OG_DESC_RE.search(html) or META_DESC_RE.search(html)
    if m:
        desc = _unescape(m.group(1))

    # Visible-text-only scan for the noisy numeric fields.
    try:
        tree = HTMLParser(html)
        for tag in tree.css("script,style,noscript,svg"):
            tag.decompose()
        body_node = tree.body or tree.root
        visible = body_node.text(separator=" ", strip=True) if body_node else ""
    except Exception:
        visible = re.sub(r"<[^>]+>", " ", html)

    rating = None
    m = RATING_RE.search(visible)
    if m:
        try:
            rating = float(m.group(1))
        except ValueError:
            rating = None

    rating_count = None
    m = RATING_CNT_RE.search(visible)
    if m:
        rating_count = _to_int(m.group(1))

    users = None
    m = USERS_RE.search(visible)
    if m:
        users = _to_int(m.group(1))

    category = None
    m = CATEGORY_RE.search(html)   # category lives in an anchor href, scan raw HTML
    if m:
        category = _unescape(m.group(1)).strip()

    # Guard: if we got neither a name nor any signal, treat as a miss (blocked/empty page).
    if not name and rating is None and users is None:
        return None
    if not name:
        name = ext_id
    name = name[:300]

    body = f"{name}. {desc}".strip()
    metadata = {"users": users, "rating": rating,
                "rating_count": rating_count, "category": category}
    return (SLUG, ext_id, url, name, body, Json(metadata))


def detail_url(ext_id, slug_hint="ext"):
    # Canonical detail URL works with any slug segment; the id is what matters for routing.
    return f"{BASE}/detail/{slug_hint}/{ext_id}"


# --------------------------------------------------------------------------- DB

def _flush(cur, rows):
    uniq = {}
    for r in rows:
        uniq[(r[0], r[1])] = r   # dedup on (source_slug, ext_id) within this batch
    if not uniq:
        return 0
    execute_values(cur, """INSERT INTO documents (source_slug, ext_id, url, title, body, metadata)
        VALUES %s ON CONFLICT (source_slug, ext_id) DO UPDATE
        SET title=EXCLUDED.title, body=EXCLUDED.body, metadata=EXCLUDED.metadata""",
                   list(uniq.values()))
    return len(uniq)


def run():
    conn = psycopg2.connect(PG_DSN)
    cur = conn.cursor()
    cur.execute("SELECT ext_id FROM documents WHERE source_slug=%s", (SLUG,))
    seen = {r[0] for r in cur.fetchall()}

    if not _HAS_CFFI:
        print("WARN: curl_cffi not importable; falling back to httpx (more likely to be blocked)")

    seeds = seed_ids_from_sitemap() if USE_SITEMAP else seed_ids_from_search()
    todo = [eid for eid in seeds if eid not in seen][:MAX]
    print(f"seeded {len(seeds)} ids; {len(seen)} already collected; fetching {len(todo)} details")

    rows, processed, errs, saved = [], 0, 0, 0
    for ext_id in todo:
        url = detail_url(ext_id)
        try:
            html = _get(url)
            if not html:
                errs += 1
            else:
                row = parse_detail(ext_id, url, html)
                if row:
                    rows.append(row)
                else:
                    errs += 1
        except Exception as e:
            errs += 1
            print("  detail err", ext_id, repr(e)[:60])
        processed += 1
        if len(rows) >= 100:
            saved += _flush(cur, rows)
            conn.commit()
            rows = []
        time.sleep(DELAY)

    if rows:
        saved += _flush(cur, rows)
        conn.commit()
    cur.close()
    conn.close()
    print(f"chrome_extension: processed {processed}, saved {saved}, errors {errs}")


if __name__ == "__main__":
    run()
