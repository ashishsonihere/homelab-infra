"""AlternativeTo connector — server-rendered HTML, httpx + selectolax, datacenter proxy.

SWITCHING signal ("people leaving X for Y") + tool dislikes. For each seed software slug we fetch
its AlternativeTo page (/software/<slug>/) and extract:
  - the listed alternatives (the "Y" people move to): name, slug/url, like count, short description
  - the page's user reviews/comments (the dislikes / pain text we actually analyze)
One `documents` row per alternative item; plus one summary row per seed page carrying the comment
text (so the verbatim pain isn't lost even when it's attached to the seed, not an alternative).

Per Scraping-Playbook.md 2.1: AlternativeTo is server-rendered, NO Cloudflare challenge, light
soft rate limiting. httpx + selectolax through a Webshare DATACENTER proxy + polite pacing is
sufficient. Writes to the universal `documents` table (source_slug='alternativeto').

----------------------------------------------------------------------------------------------------
DERIVED SELECTORS (verified against live HTML of https://alternativeto.net/software/shopify/ on
2026-06-21; the site is Next.js app-router but the alternatives + reviews are in the initial HTML):

  Alternative card      : node[data-testid^="item-"]   -> <article class="app-item-container">
                          (the testid value is the alt's slug, e.g. data-testid="item-woocommerce")
  Alt name              : h2 inside the card  (e.g. <h2 ...>WooCommerce</h2>)
  Alt link              : a[href^="/software/"]  inside the card (href like /software/<slug>/about/
                          or /software/<slug>/ ; we normalize to the slug)
  Alt like count        : inside div#like-button-container -> a <span> whose text is "N likes"
                          (we also fall back to scanning the card text for a "N likes" pattern)
  Alt short description : div.md_Desc > p   (inside the card)
  Page reviews/comments : div.md_Comments > p   (the dislike / pain text; ~16 present on Shopify)

CAUTION / VERIFY ON DEPLOY (these are derived from ONE page; the human should sanity-check):
  - Selectors are CSS-module / Tailwind classes that CAN change without notice. md_Desc / md_Comments
    look stable (plain class names, not hashed); the data-testid="item-<slug>" hook is the most
    reliable anchor for cards. If a future run returns 0 alternatives, re-derive selectors.
  - Comments belong to the *seed* page, not to individual alternatives — AlternativeTo doesn't render
    per-alternative comments in the initial HTML (they lazy-load on expand). So per-alternative `body`
    is the alt's own description; the seed's comment/dislike text is stored as one extra row per seed
    (ext_id "<seed>:__page__"). This keeps the "what we analyze" text without falsely attributing it.
  - Category pages (/category/<x>/) were NOT used: the real path is /category/business-and-commerce/
    (NOT /category/ecommerce/, which 404s) and its card markup wasn't confirmed. The seed-page path
    above is the verified, spec-required one. To add categories later, confirm their card selectors.

Run:  python -m connectors.alternativeto
Env:  PG_DSN (required), AT_SEEDS (csv slugs), AT_PROXY_FILE, AT_DELAY_MIN/MAX
----------------------------------------------------------------------------------------------------
"""
import os
import re
import html
import random
import time
import httpx
import psycopg2
from selectolax.parser import HTMLParser
from psycopg2.extras import Json, execute_values

PG_DSN = os.environ["PG_DSN"]
SLUG = "alternativeto"

SEEDS = os.environ.get(
    "AT_SEEDS",
    "shopify,klaviyo,gorgias,triple-whale,quickbooks,mailchimp,hubspot,zendesk,google-analytics"
).split(",")

PROXY_FILE = os.environ.get("AT_PROXY_FILE", "/opt/market-research/proxies.txt")
BASE = "https://alternativeto.net"
DELAY_MIN = float(os.environ.get("AT_DELAY_MIN", "3.0"))   # polite pacing per playbook (~1 req/3-5s)
DELAY_MAX = float(os.environ.get("AT_DELAY_MAX", "5.0"))
MAX_RETRIES = int(os.environ.get("AT_MAX_RETRIES", "3"))

# A real, internally-consistent recent-Chrome header bundle (per playbook 1.1 — a bare UA is the
# #1 tell). One stable identity per run is fine for this low volume; the proxy rotates the IP.
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}

LIKES_RE = re.compile(r"([\d,]+)\s+likes?", re.IGNORECASE)
SLUG_RE = re.compile(r"^/software/([a-z0-9][a-z0-9-]*)/?")


def _proxy():
    """Pick a random Webshare datacenter proxy line (host:port:user:pass) -> httpx proxy URL.

    Rotates per call (we call it once per seed request). Returns None if the file is missing or
    empty so the connector still runs direct (fine at this volume per playbook 2.1).
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
    return html.unescape(re.sub(r"\s+", " ", s or "")).strip()


def _fetch(url):
    """GET a page with a rotated proxy + browser headers. Returns HTML text or None.

    Backs off on 429/503 (slow down, same approach); treats 403 as a block (rotate proxy via the
    next attempt). Each attempt re-rolls the proxy so a blocked/rate-limited IP isn't reused.
    """
    for attempt in range(MAX_RETRIES):
        proxy = _proxy()
        try:
            # httpx>=0.26 uses `proxy=`; older uses `proxies=`. Try the modern arg, fall back.
            try:
                client = httpx.Client(timeout=30, headers=HEADERS, follow_redirects=True, proxy=proxy)
            except TypeError:
                client = httpx.Client(timeout=30, headers=HEADERS, follow_redirects=True,
                                      proxies=proxy)
            with client as c:
                r = c.get(url)
            if r.status_code == 200:
                return r.text
            if r.status_code in (429, 503):
                wait = (2 ** attempt) * 3 + random.uniform(0, 2)  # exp backoff + jitter
                print(f"  {r.status_code} on {url} -> backoff {wait:.1f}s")
                time.sleep(wait)
                continue
            if r.status_code == 403:
                print(f"  403 on {url} -> rotating proxy/headers (attempt {attempt + 1})")
                time.sleep(random.uniform(2, 4))
                continue
            print(f"  HTTP {r.status_code} on {url}")
            return None
        except Exception as e:
            print(f"  fetch err {url}: {repr(e)[:100]}")
            time.sleep(random.uniform(2, 4))
    return None


def _alt_slug_from_card(card):
    """Best stable slug for an alternative card: prefer data-testid='item-<slug>', else first
    /software/<slug>/ link, else None."""
    tid = card.attributes.get("data-testid", "")
    if tid.startswith("item-"):
        return tid[len("item-"):]
    for a in card.css("a[href^='/software/']"):
        m = SLUG_RE.match(a.attributes.get("href", ""))
        if m:
            return m.group(1)
    return None


def _alt_url(card, slug):
    """Canonical AlternativeTo URL for the alternative (prefer the in-card href; else build it)."""
    for a in card.css("a[href^='/software/']"):
        href = a.attributes.get("href", "")
        if SLUG_RE.match(href):
            return BASE + href
    return f"{BASE}/software/{slug}/"


def _alt_name(card, slug):
    h2 = card.css_first("h2")
    if h2 and _clean(h2.text()):
        return _clean(h2.text())
    return slug.replace("-", " ").title()


def _alt_likes(card):
    """Like count for the card. Prefer the like-button container; fall back to scanning card text."""
    container = card.css_first("#like-button-container")
    scope = container.text() if container else card.text()
    m = LIKES_RE.search(scope or "")
    if m:
        try:
            return int(m.group(1).replace(",", ""))
        except ValueError:
            return None
    return None


def _alt_desc(card):
    d = card.css_first("div.md_Desc")
    if d:
        return _clean(d.text())
    return ""


def _page_comments(tree):
    """All server-rendered review/comment <p> text on the seed page (the dislike/pain signal)."""
    out = []
    for c in tree.css("div.md_Comments"):
        t = _clean(c.text())
        if t:
            out.append(t)
    return out


def parse_seed(seed, html_text):
    """Return list of document rows for one seed page.

    Rows: one per alternative card + one '__page__' row carrying the seed's comment/dislike text.
    """
    rows = []
    tree = HTMLParser(html_text)

    # --- alternatives (the "Y" people switch to) ---
    cards = tree.css("[data-testid^='item-']")
    for card in cards:
        alt_slug = _alt_slug_from_card(card)
        if not alt_slug or alt_slug == seed:
            continue
        name = _alt_name(card, alt_slug)
        url = _alt_url(card, alt_slug)
        likes = _alt_likes(card)
        desc = _alt_desc(card)
        body = desc or name  # description is the per-alternative analyzable text
        ext_id = f"{seed}:{alt_slug}"
        rows.append((
            SLUG, ext_id, url, name, body,
            Json({
                "seed_software": seed,
                "likes": likes,
                "category": None,          # not reliably present on the seed page card
                "keyword": seed,           # the seed acts as our query keyword for this corpus
                "alt_slug": alt_slug,
            }),
        ))

    # --- seed-page reviews/comments (the dislikes / verbatim pain) as one summary row ---
    comments = _page_comments(tree)
    if comments:
        joined = "\n\n".join(comments)
        rows.append((
            SLUG, f"{seed}:__page__", f"{BASE}/software/{seed}/", seed.replace("-", " ").title(),
            joined,
            Json({
                "seed_software": seed,
                "likes": None,
                "category": None,
                "keyword": seed,
                "kind": "seed_page_comments",
                "comment_count": len(comments),
                "alt_count": len(rows),  # alternatives found on this page (rows so far)
            }),
        ))
    return rows


def run():
    conn = psycopg2.connect(PG_DSN)
    cur = conn.cursor()
    rows = []
    seeds_ok = 0

    for seed in SEEDS:
        seed = seed.strip()
        if not seed:
            continue
        url = f"{BASE}/software/{seed}/"
        try:
            html_text = _fetch(url)
            if not html_text:
                print(f"  {seed}: no HTML (skipped)")
                continue
            seed_rows = parse_seed(seed, html_text)
            rows.extend(seed_rows)
            seeds_ok += 1
            print(f"  {seed}: {len(seed_rows)} rows")
        except Exception as e:
            print(f"  {seed}: parse err {repr(e)[:120]}")
        time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))  # polite jittered pacing

    # Dedup rows on (source_slug, ext_id) in Python to avoid CardinalityViolation on upsert.
    uniq = {}
    for r in rows:
        uniq[(r[0], r[1])] = r
    rows = list(uniq.values())

    if rows:
        execute_values(cur, """INSERT INTO documents (source_slug, ext_id, url, title, body, metadata)
            VALUES %s ON CONFLICT (source_slug, ext_id) DO UPDATE
            SET title=EXCLUDED.title, body=EXCLUDED.body, metadata=EXCLUDED.metadata""", rows)
    conn.commit()
    n = len(rows)
    cur.close()
    conn.close()
    print(f"{SLUG}: upserted {n} rows from {seeds_ok}/{len(SEEDS)} seed pages")


if __name__ == "__main__":
    run()
