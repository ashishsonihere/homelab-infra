"""AppSumo connector — SSR behind Cloudflare passive layers; curl_cffi (Chrome TLS) + datacenter proxy.

Software-deal + candid review/comment signal. We enumerate software deals from the browse /
category pages, then visit each deal page to pull the verbatim review text. Writes to the universal
`documents` table (source_slug='appsumo').

Per Scraping-Playbook.md 2.7 / 6: AppSumo is server-rendered but **fingerprint-gated** by Cloudflare
(IP-reputation + TLS/JA3), not a hard per-request JS challenge. Plain httpx/requests on a datacenter
IP gets flagged at the TLS layer; a browser-like TLS fingerprint passes the passive checks. So we use
**curl_cffi** with `impersonate="chrome"` + a Webshare DATACENTER proxy + slow, jittered pacing.
Escalate to Playwright+stealth (+ residential) only if you start hitting active Managed Challenge /
Turnstile — for AppSumo curl_cffi is usually enough.

====================================================================================================
WHAT WE FOUND ON THE LIVE SITE  (verified 2026-06-21 against real HTML — see notes & VERIFY below)

>>> We parse the embedded **__NEXT_DATA__ JSON blob**, NOT CSS selectors. <<<

AppSumo is a Next.js (Pages Router) app: every page ships a `<script id="__NEXT_DATA__" ...>` tag
containing the full server state as JSON. This is far more robust than scraping hashed Tailwind /
CSS-module class names off the rendered DOM, so this connector reads JSON exclusively.

BROWSE / CATEGORY pages  (e.g. https://appsumo.com/software/ , https://appsumo.com/software/<cat>/):
  props.pageProps.fallbackData[0].deals  -> list of ~20 deal cards (the SSR'd first page). Each card
  is already rich enough to build a full deal row WITHOUT visiting the product page:
    .slug                          deal slug (our ext_id)            e.g. "tidycal"
    .public_name                   deal name (title)                 e.g. "TidyCal"
    .card_description              tagline / short description
    .get_absolute_url              "/products/<slug>/"  (the deal URL)
    .price / .original_price       current deal price / list price   e.g. 29 / 144
    .deal_review.review_count      total review count                e.g. 903
    .deal_review.average_rating    star rating (string)              e.g. "4.35"
    .taxonomy.category / .subcategory   category slugs               e.g. "operations" / "calendar-scheduling"
    .reviews_summary               AppSumo's AI summary of reviews (extra body signal)
    .story_highlights[]            {"highlight": "..."} bullet points
  props.pageProps.fallbackData[0].meta -> {total_results, total_pages, page, per_page}

  NOTE ON PAGINATION (verified): the browse SSR HTML ALWAYS embeds page 1 — `?page=2` returns the
  SAME 20 deals (further pages load client-side via XHR, not in __NEXT_DATA__). So we CANNOT paginate
  the browse page via the embedded JSON. Instead we enumerate across the main /software/ page + the
  CATEGORY pages (each category SSRs its own distinct first 20 deals). browse + 6 categories gave
  ~120 unique slugs on 2026-06-21 — plenty to cap at MAX_DEALS (~60).

DEAL / PRODUCT page  (https://appsumo.com/products/<slug>/):
  props.pageProps.deal:
    .public_name, .slug
    .products[0].story.meta_title       a tagline ("The scheduling tool that gets you paid")
    .products[0].story.card_description short description
    .products[0].story.features.title   feature headline (extra body)
    .price / .original_price
    .deal_review.{review_count, average_rating, review_count_1_tacos..5_tacos}  (per-star breakdown)
    .deal_comment.comment_count
    .taxonomy.{category, subcategory}
    .reviews_summary                    AI summary of reviews
    .top_5_reviews[]                    {id, user, comment, submit_date, rating, title}  (fallback)
  props.pageProps.reviews.comments[]    PREFERRED full review objects (richer than top_5_reviews):
    {id, comment, title, rating, would_recommend, created, user{first_name,last_name,username},
     up_votes, down_votes, purchased, display_path, children[...]}
    -> ~5 reviews are SSR'd per page; the rest paginate via an XHR API (meta.search_after cursor) we
       do NOT call here (keeps it polite + simple). ~5 verbatim reviews/deal is solid pain signal.
  props.pageProps.questions.comments[]  Q&A threads (same shape) — captured as bonus review-ish rows.

ROW MODEL (per task spec):
  - 1 row per DEAL:   ext_id=<slug>, url=deal URL, title=deal name,
                      body = tagline + description + feature headline + AI reviews_summary,
                      metadata = {category, price, rating, review_count, ...}.
  - 1 row per REVIEW: ext_id=f"{slug}:rev:{i}", url=deal URL, title=review title (or deal name),
                      body = review comment text, metadata = {rating, author, date, kind:'review', ...}.
  Dedup on (source_slug, ext_id) before the idempotent upsert.

====================================================================================================
VERIFY ON DEPLOY (derived from a handful of live pages on 2026-06-21 — human should sanity-check):
  * __NEXT_DATA__ JSON PATHS are stable across Next.js builds but CAN change if AppSumo restructures.
    If a run returns 0 deals, dump one page's __NEXT_DATA__ and re-confirm the
    `pageProps.fallbackData[0].deals` and `pageProps.deal` / `pageProps.reviews.comments` paths.
  * CATEGORY SLUGS used below were observed in the browse-page nav on 2026-06-21. If a category 404s
    or yields 0 deals, drop/adjust it (the code skips empties gracefully).
  * CLOUDFLARE: on the test machine a plain fetch happened to pass (no challenge). In production from
    a datacenter IP you may see 403 / "Just a moment" challenge HTML. curl_cffi impersonate=chrome +
    proxy should pass the PASSIVE layer; if you get persistent 403s, that's an ACTIVE challenge ->
    escalate to Playwright+stealth (+ residential) per the playbook. We detect challenge HTML and
    treat it as a 403 (rotate proxy / back off).
  * REVIEW PAGINATION (search_after cursor XHR) is intentionally NOT followed — only the ~5 SSR'd
    reviews + ~5 questions per deal are captured. Wire up the cursor API later if deeper review depth
    is needed (and raise the per-deal politeness budget accordingly).

Run:  python -m connectors.appsumo
Env:  PG_DSN (required), AS_CATEGORIES (csv), AS_MAX_DEALS, AS_PROXY_FILE,
      AS_DELAY_MIN/MAX, AS_MAX_RETRIES, AS_REVIEWS_PER_DEAL
====================================================================================================
"""
import os
import re
import json
import html as _html
import random
import time

import psycopg2
from curl_cffi import requests as creq
from psycopg2.extras import Json, execute_values

PG_DSN = os.environ["PG_DSN"]
SLUG = "appsumo"

BASE = "https://appsumo.com"

# Browse page + category pages each SSR their own distinct first 20 deals (the browse `?page=N` param
# does NOT paginate the embedded JSON — see module docstring). Enumerate across these to reach ~60.
CATEGORIES = [c.strip() for c in os.environ.get(
    "AS_CATEGORIES",
    "marketing-sales,operations,media-tools,finance,development-it,customer-experience,build-it-yourself",
).split(",") if c.strip()]

MAX_DEALS = int(os.environ.get("AS_MAX_DEALS", "60"))          # cap per task spec (~60 deals)
REVIEWS_PER_DEAL = int(os.environ.get("AS_REVIEWS_PER_DEAL", "10"))  # SSR'd reviews+questions kept/deal

PROXY_FILE = os.environ.get("AS_PROXY_FILE", "/opt/market-research/proxies.txt")
DELAY_MIN = float(os.environ.get("AS_DELAY_MIN", "4.0"))       # playbook 2.7: 1 req / 4-8s, polite
DELAY_MAX = float(os.environ.get("AS_DELAY_MAX", "8.0"))
MAX_RETRIES = int(os.environ.get("AS_MAX_RETRIES", "3"))

# Full, internally-consistent recent-Chrome header bundle (playbook 1.1). curl_cffi sets the TLS
# fingerprint via impersonate=; these headers keep the HTTP layer consistent with that.
HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL
)
# Cloudflare ACTIVE-challenge tells in the HTML body (passive layer returns real HTML, no challenge).
CHALLENGE_RE = re.compile(
    r"Just a moment|cf-chl-|challenge-platform|/cdn-cgi/challenge", re.IGNORECASE
)


def _proxy():
    """Pick a random Webshare datacenter proxy line (host:port:user:pass) -> proxy URL string.

    Rotates per call. Returns None if the file is missing/empty so the connector still runs direct
    (useful for local testing; in prod the proxy rotates IP reputation to spread the Cloudflare load).
    """
    try:
        lines = [l.strip() for l in open(PROXY_FILE) if l.strip()]
        if not lines:
            return None
        host, port, user, pwd = random.choice(lines).split(":")[:4]
        return f"http://{user}:{pwd}@{host}:{port}"
    except Exception:
        return None


def _clean(s):
    """Collapse whitespace + unescape HTML entities."""
    return _html.unescape(re.sub(r"\s+", " ", s or "")).strip()


def _fetch(url):
    """GET a page with curl_cffi (Chrome TLS impersonation) + rotated proxy. Returns HTML or None.

    Treats 429/503 as 'slow down' (exponential backoff + jitter, retry). Treats 403 OR a Cloudflare
    active-challenge body as a block -> back off and re-roll the proxy on the next attempt (don't
    hammer one IP; Cloudflare blacklists fast — playbook 1.3/2.7).
    """
    for attempt in range(MAX_RETRIES):
        proxy = _proxy()
        proxies = {"http": proxy, "https": proxy} if proxy else None
        try:
            r = creq.get(
                url,
                impersonate="chrome",
                headers=HEADERS,
                proxies=proxies,
                timeout=30,
            )
            status = r.status_code
            if status == 200:
                text = r.text
                if CHALLENGE_RE.search(text[:4000]):
                    # Passive layer should have served real HTML; a challenge body => active block.
                    print(f"  CF challenge on {url} -> rotating proxy (attempt {attempt + 1})")
                    time.sleep(random.uniform(3, 6))
                    continue
                return text
            if status in (429, 503):
                wait = (2 ** attempt) * 4 + random.uniform(0, 3)  # exp backoff + jitter
                print(f"  {status} on {url} -> backoff {wait:.1f}s")
                time.sleep(wait)
                continue
            if status == 403:
                print(f"  403 on {url} -> rotating proxy/headers (attempt {attempt + 1})")
                time.sleep(random.uniform(3, 6))
                continue
            print(f"  HTTP {status} on {url}")
            return None
        except Exception as e:
            print(f"  fetch err {url}: {repr(e)[:120]}")
            time.sleep(random.uniform(3, 6))
    return None


def _next_data(html_text):
    """Extract + json.loads the <script id='__NEXT_DATA__'> blob -> dict, or None."""
    if not html_text:
        return None
    m = NEXT_DATA_RE.search(html_text)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except Exception as e:
        print(f"  __NEXT_DATA__ parse err: {repr(e)[:120]}")
        return None


def _page_props(html_text):
    data = _next_data(html_text)
    if not data:
        return None
    try:
        return data["props"]["pageProps"]
    except Exception:
        return None


def enumerate_deal_cards(html_text):
    """Yield deal-card dicts from a browse/category page's __NEXT_DATA__.

    Each card carries name/price/rating/category/description directly, so we can build a full deal
    row from it even before fetching the product page.
    """
    pp = _page_props(html_text)
    if not pp:
        return []
    fb = pp.get("fallbackData") or []
    if not (isinstance(fb, list) and fb and isinstance(fb[0], dict)):
        return []
    return fb[0].get("deals") or []


def _deal_url(slug, card=None):
    if card and card.get("get_absolute_url"):
        return BASE + card["get_absolute_url"]
    return f"{BASE}/products/{slug}/"


def _highlights_text(card_or_deal):
    """Join story_highlights[].highlight bullets into one extra body line."""
    hs = card_or_deal.get("story_highlights") or []
    out = []
    for h in hs:
        if isinstance(h, dict):
            t = _clean(h.get("highlight"))
            if t:
                out.append(t)
    return " | ".join(out)


def deal_row_from_card(card):
    """Build the per-deal documents row tuple straight from a browse/category card dict.

    Returns (slug, row_tuple) or (None, None) if the card lacks a slug.
    """
    slug = card.get("slug")
    if not slug:
        return None, None
    name = _clean(card.get("public_name")) or slug.replace("-", " ").title()
    url = _deal_url(slug, card)

    tagline = _clean(card.get("card_description"))
    highlights = _highlights_text(card)
    ai_summary = _clean(card.get("reviews_summary"))
    body = "\n\n".join([p for p in (tagline, highlights, ai_summary) if p]) or name

    taxonomy = card.get("taxonomy") or {}
    review = card.get("deal_review") or {}
    rating = review.get("average_rating")
    try:
        rating = float(rating) if rating is not None else None
    except (TypeError, ValueError):
        pass

    metadata = {
        "kind": "deal",
        "category": taxonomy.get("category"),
        "subcategory": taxonomy.get("subcategory"),
        "price": card.get("price"),
        "original_price": card.get("original_price"),
        "rating": rating,
        "review_count": review.get("review_count"),
        "deal_id": card.get("id"),
        "slug": slug,
    }
    return slug, (SLUG, slug, url, name, body, Json(metadata))


def _author(user):
    """Human-ish author label from a review's user dict (no PII de-anon; first/last + handle only)."""
    if not isinstance(user, dict):
        return None
    name = " ".join(p for p in (user.get("first_name"), user.get("last_name")) if p).strip()
    return name or user.get("username")


def review_rows_from_deal_page(slug, deal_url, deal_name, pp):
    """Build per-review rows from a product page's pageProps.

    Prefers pageProps.reviews.comments[] (richest); merges in pageProps.questions.comments[] (Q&A);
    falls back to pageProps.deal.top_5_reviews[] if the reviews list is absent. ext_id = slug:rev:{i}.
    """
    items = []

    reviews = (pp.get("reviews") or {}).get("comments") or []
    for c in reviews:
        if isinstance(c, dict):
            items.append(("review", c))

    questions = (pp.get("questions") or {}).get("comments") or []
    for c in questions:
        if isinstance(c, dict):
            items.append(("question", c))

    if not reviews:
        # Fallback path: top_5_reviews has a slimmer shape (no 'created', uses 'submit_date').
        for c in (pp.get("deal") or {}).get("top_5_reviews") or []:
            if isinstance(c, dict):
                items.append(("review", c))

    rows = []
    for i, (kind, c) in enumerate(items[:REVIEWS_PER_DEAL]):
        comment = _clean(c.get("comment"))
        title = _clean(c.get("title"))
        if not comment and not title:
            continue
        body = "\n\n".join([p for p in (title, comment) if p]) or comment or title
        ext_id = f"{slug}:rev:{i}"
        rating = c.get("rating")
        metadata = {
            "kind": kind,                       # "review" or "question"
            "parent_deal": slug,
            "rating": rating,
            "would_recommend": c.get("would_recommend"),
            "author": _author(c.get("user")),
            "date": c.get("created") or c.get("submit_date"),
            "up_votes": c.get("up_votes"),
            "down_votes": c.get("down_votes"),
            "purchased": c.get("purchased"),
        }
        rows.append((SLUG, ext_id, deal_url, title or deal_name, body, Json(metadata)))
    return rows


def enrich_deal_from_page(slug, deal_url, pp):
    """Optionally produce a richer deal row from the product page itself (better tagline/description
    than the card) PLUS the review rows. Returns (deal_row_or_None, [review_rows])."""
    deal = pp.get("deal") or {}
    if not deal:
        return None, []

    name = _clean(deal.get("public_name")) or slug.replace("-", " ").title()
    products = deal.get("products") or []
    story = (products[0].get("story") if products and isinstance(products[0], dict) else {}) or {}
    tagline = _clean(story.get("meta_title"))
    desc = _clean(story.get("card_description"))
    feat = _clean((story.get("features") or {}).get("title"))
    ai_summary = _clean(deal.get("reviews_summary"))
    body = "\n\n".join([p for p in (tagline, desc, feat, ai_summary) if p]) or name

    taxonomy = deal.get("taxonomy") or {}
    review = deal.get("deal_review") or {}
    rating = review.get("average_rating")
    try:
        rating = float(rating) if rating is not None else None
    except (TypeError, ValueError):
        pass

    metadata = {
        "kind": "deal",
        "category": taxonomy.get("category"),
        "subcategory": taxonomy.get("subcategory"),
        "price": deal.get("price"),
        "original_price": deal.get("original_price"),
        "rating": rating,
        "review_count": review.get("review_count"),
        "comment_count": (deal.get("deal_comment") or {}).get("comment_count"),
        "deal_id": deal.get("id"),
        "slug": slug,
    }
    deal_row = (SLUG, slug, deal_url, name, body, Json(metadata))
    review_rows = review_rows_from_deal_page(slug, deal_url, name, pp)
    return deal_row, review_rows


def run():
    conn = psycopg2.connect(PG_DSN)
    cur = conn.cursor()
    rows = []

    # --- 1) enumerate deal cards from browse + category pages (SSR __NEXT_DATA__) ---
    listing_urls = [f"{BASE}/software/"] + [f"{BASE}/software/{c}/" for c in CATEGORIES]
    cards_by_slug = {}     # slug -> card dict (first win); preserves order via insertion
    for url in listing_urls:
        if len(cards_by_slug) >= MAX_DEALS:
            break
        html_text = _fetch(url)
        cards = enumerate_deal_cards(html_text)
        added = 0
        for card in cards:
            s = card.get("slug")
            if not s or s in cards_by_slug:
                continue
            cards_by_slug[s] = card
            added += 1
            if len(cards_by_slug) >= MAX_DEALS:
                break
        print(f"  listing {url}: +{added} new deals (total {len(cards_by_slug)})")
        time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))

    deal_slugs = list(cards_by_slug.keys())[:MAX_DEALS]
    print(f"  enumerated {len(deal_slugs)} deals (cap {MAX_DEALS})")

    # Seed one deal row per card up front (so we still have deals even if a product page fails).
    for s in deal_slugs:
        _, row = deal_row_from_card(cards_by_slug[s])
        if row:
            rows.append(row)

    # --- 2) visit each deal page -> richer deal row + verbatim review rows ---
    deals_ok = 0
    for s in deal_slugs:
        url = _deal_url(s, cards_by_slug.get(s))
        try:
            html_text = _fetch(url)
            pp = _page_props(html_text)
            if not pp:
                print(f"  {s}: no pageProps (kept card-only row)")
            else:
                deal_row, review_rows = enrich_deal_from_page(s, url, pp)
                if deal_row:
                    rows.append(deal_row)       # overrides the card row on dedup (same ext_id)
                rows.extend(review_rows)
                deals_ok += 1
                print(f"  {s}: deal + {len(review_rows)} reviews")
        except Exception as e:
            print(f"  {s}: page err {repr(e)[:120]}")
        time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))

    # --- 3) dedup on (source_slug, ext_id); product-page rows (appended later) win ---
    uniq = {}
    for r in rows:
        uniq[(r[0], r[1])] = r
    rows = list(uniq.values())

    if rows:
        execute_values(
            cur,
            "INSERT INTO documents (source_slug,ext_id,url,title,body,metadata) VALUES %s "
            "ON CONFLICT (source_slug,ext_id) DO UPDATE SET "
            "title=EXCLUDED.title,body=EXCLUDED.body,metadata=EXCLUDED.metadata",
            rows,
        )
    conn.commit()
    print(f"{SLUG}: upserted {len(rows)} rows ({deals_ok}/{len(deal_slugs)} deal pages fetched)")
    cur.close()
    conn.close()


if __name__ == "__main__":
    run()
