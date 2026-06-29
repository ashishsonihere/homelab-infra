"""Custom Reddit scraper (no official API) — Playwright + old.reddit.com, proxy-aware.

Why: Reddit's free .json/.rss are 403-blocked from datacenter IPs. This drives a real (headless) browser
and routes through a proxy. To be RELIABLE it needs RESIDENTIAL/mobile proxies — datacenter proxies
(free Webshare) will still get blocked. See homelab-vault/Research/Reddit-Scraping.md.

Run (needs a Playwright image):  python -m connectors.reddit_scraper
Env: PROXY_FILE=/opt/market-research/proxies.txt (Webshare format ip:port:user:pass per line)
     REDDIT_SUBS=shopify,ecommerce,FulfillmentByAmazon,AmazonSeller,EtsySellers
"""
import os
import time
import random
import psycopg2
from psycopg2.extras import Json
from .base import PG_DSN, content_hash

SUBS = os.environ.get("REDDIT_SUBS", "shopify,ecommerce,FulfillmentByAmazon,AmazonSeller,EtsySellers").split(",")
PROXY_FILE = os.environ.get("PROXY_FILE", "/opt/market-research/proxies.txt")


def _load_proxy():
    """Return one proxy dict for Playwright from a Webshare-format line (ip:port:user:pass)."""
    try:
        lines = [l.strip() for l in open(PROXY_FILE) if l.strip()]
        if not lines:
            return None
        ip, port, user, pwd = random.choice(lines).split(":")[:4]
        return {"server": f"http://{ip}:{port}", "username": user, "password": pwd}
    except Exception:
        return None


def scrape():
    from playwright.sync_api import sync_playwright  # imported lazily (heavy dep)
    conn = psycopg2.connect(PG_DSN); cur = conn.cursor(); n = 0
    with sync_playwright() as p:
        for sub in SUBS:
            proxy = _load_proxy()
            browser = p.chromium.launch(headless=True, proxy=proxy,
                                        args=["--disable-blink-features=AutomationControlled"])
            ctx = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/127.0 Safari/537.36",
                locale="en-US", viewport={"width": 1366, "height": 900})
            page = ctx.new_page()
            try:
                page.goto(f"https://old.reddit.com/r/{sub}/new/", wait_until="domcontentloaded", timeout=30000)
                if "blocked" in page.content().lower() or page.title().lower().startswith("blocked"):
                    print(f"  r/{sub}: BLOCKED (needs residential proxy)"); browser.close(); continue
                for el in page.query_selector_all("div.thing.link"):
                    pid = el.get_attribute("data-fullname") or ""
                    title_el = el.query_selector("a.title")
                    if not title_el:
                        continue
                    title = title_el.inner_text().strip()
                    href = title_el.get_attribute("href") or ""
                    score = el.get_attribute("data-score")
                    author = el.get_attribute("data-author")
                    cur.execute(
                        """INSERT INTO documents (source_slug, ext_id, url, title, body, content_hash, metadata)
                           VALUES ('reddit',%s,%s,%s,%s,%s,%s)
                           ON CONFLICT (source_slug, ext_id) DO UPDATE SET title=EXCLUDED.title""",
                        (pid, ("https://old.reddit.com"+href) if href.startswith("/") else href,
                         title, title, content_hash(title),
                         Json({"sub": sub, "score": score, "author": author})))
                    n += 1
                conn.commit()
            except Exception as e:
                print(f"  r/{sub}: {repr(e)[:120]}")
            finally:
                browser.close()
            time.sleep(random.uniform(3, 6))
    cur.close(); conn.close()
    return f"reddit_scraper: upserted {n} posts"


if __name__ == "__main__":
    print(scrape())
