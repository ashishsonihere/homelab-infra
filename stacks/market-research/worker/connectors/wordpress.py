"""WordPress.org plugins — COMPREHENSIVE: browse all most-popular plugins (paginated) + keyword passes.

Free official API, no key, no proxy. The browse pass walks `request[browse]=popular` so we capture the
top ~thousands of plugins (the relevant universe), not a 15-keyword sample. documents(source_slug='wordpress_plugins').
Run: python -m connectors.wordpress
"""
import os
import html
import httpx
import psycopg2
from psycopg2.extras import Json, execute_values

PG_DSN = os.environ["PG_DSN"]
SLUG = "wordpress_plugins"
KEYWORDS = os.environ.get(
    "WP_KEYWORDS",
    "ecommerce,woocommerce,inventory,shipping,seo,email marketing,analytics,booking,"
    "invoice,subscription,marketing,reviews,popup,crm,page builder,payments,membership,forms"
).split(",")
PER_KW = int(os.environ.get("WP_PER_KW", "100"))
BROWSE_PAGES = int(os.environ.get("WP_BROWSE_PAGES", "40"))   # 40 x 250 = top ~10k popular plugins
API = "https://api.wordpress.org/plugins/info/1.2/"
UA = {"User-Agent": "Mozilla/5.0 (market-research bot)"}


def clean(s):
    return html.unescape(s or "").strip()


def _row(p, src):
    slug = p.get("slug")
    if not slug:
        return None
    installs, rating, nr = p.get("active_installs"), p.get("rating"), p.get("num_ratings")
    st, strs = p.get("support_threads"), p.get("support_threads_resolved")
    desc = clean(p.get("short_description"))
    body = (f"{desc}\n\n[active_installs={installs}, rating={rating}/100 ({nr} ratings), "
            f"support_threads={st}, resolved={strs}]")
    return (SLUG, slug, p.get("homepage") or f"https://wordpress.org/plugins/{slug}/",
            clean(p.get("name")), body,
            Json({"active_installs": installs, "rating": rating, "num_ratings": nr,
                  "support_threads": st, "support_threads_resolved": strs, "src": src}))


def run():
    conn = psycopg2.connect(PG_DSN)
    cur = conn.cursor()
    rows = []
    with httpx.Client(timeout=40, headers=UA, follow_redirects=True) as c:
        # COMPREHENSIVE: most-popular plugins, paginated
        for page in range(1, BROWSE_PAGES + 1):
            try:
                r = c.get(API, params={"action": "query_plugins", "request[browse]": "popular",
                                       "request[page]": page, "request[per_page]": 250})
                r.raise_for_status()
                plugins = r.json().get("plugins", [])
            except Exception as e:
                print("  browse err p", page, repr(e)[:50]); break
            if not plugins:
                break
            for p in plugins:
                row = _row(p, "popular")
                if row:
                    rows.append(row)
            print(f"  browse popular p{page}: +{len(plugins)} ({len(rows)} total)", flush=True)
        # TARGETED: keyword passes (catch niche plugins below the popular cut)
        for kw in [k.strip() for k in KEYWORDS if k.strip()]:
            try:
                r = c.get(API, params={"action": "query_plugins", "request[search]": kw,
                                       "request[per_page]": PER_KW})
                r.raise_for_status()
                plugins = r.json().get("plugins", [])
            except Exception as e:
                print("  wp err", kw, repr(e)[:50]); continue
            for p in plugins:
                row = _row(p, kw)
                if row:
                    rows.append(row)
    uniq = {}
    for r in rows:
        uniq[(r[0], r[1])] = r
    rows = list(uniq.values())
    if rows:
        execute_values(cur, """INSERT INTO documents (source_slug,ext_id,url,title,body,metadata)
            VALUES %s ON CONFLICT (source_slug,ext_id) DO UPDATE
            SET title=EXCLUDED.title, body=EXCLUDED.body, metadata=EXCLUDED.metadata""", rows)
    conn.commit()
    n = len(rows)
    cur.close(); conn.close()
    print(f"wordpress_plugins: upserted {n} plugins", flush=True)


if __name__ == "__main__":
    run()
