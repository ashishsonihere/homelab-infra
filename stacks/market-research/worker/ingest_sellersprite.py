"""SellerSprite report consolidator (Track B / merchandise).

You export 50-60 CSVs/day from SellerSprite. Drop them on the server, run this, and every row lands
in one queryable table (raw JSONB + best-effort normalized columns) ready for LLM bulk analysis to
surface the most profitable products to sell.

Flow:
  1. local:  scp *.csv devcore:/opt/market-research/sellersprite_drop/
  2. server: docker run --rm --network devcore_net -v /opt/market-research/worker:/app -w /app \
               -v /opt/market-research/sellersprite_drop:/drop --env-file /opt/market-research/worker.env \
               mr-worker:lite python ingest_sellersprite.py /drop
  (processed files are moved to /drop/_done so re-runs are idempotent)

report_type is inferred from the filename (e.g. "product-research-2026-06-20.csv" -> product-research).
Export from SellerSprite as CSV (this reads CSV via stdlib; xlsx would need openpyxl added to the image).
"""
import os
import sys
import csv
import glob
import json
import shutil
import datetime as dt
import psycopg2
from psycopg2.extras import Json, execute_values

PG_DSN = os.environ["PG_DSN"]
DROP = sys.argv[1] if len(sys.argv) > 1 else "/drop"

DDL = """
CREATE TABLE IF NOT EXISTS seller_reports (
    id            bigserial PRIMARY KEY,
    report_type   text,
    asin          text,
    title         text,
    brand         text,
    category      text,
    price         numeric,
    monthly_revenue numeric,
    monthly_units numeric,
    rating        numeric,
    reviews       integer,
    raw           jsonb,
    source_file   text,
    snapshot_date date,
    imported_at   timestamptz DEFAULT now(),
    UNIQUE (report_type, asin, snapshot_date)
);
"""

# map many possible SellerSprite header spellings -> our columns
ALIASES = {
    "asin": ["asin", "ASIN", "Asin"],
    "title": ["title", "Title", "product name", "Product Name", "name"],
    "brand": ["brand", "Brand"],
    "category": ["category", "Category", "category path", "Category Path"],
    "price": ["price", "Price", "current price"],
    "monthly_revenue": ["monthly revenue", "Monthly Revenue", "revenue", "Revenue", "parent revenue"],
    "monthly_units": ["monthly sales", "Monthly Sales", "monthly units", "units sold", "sales"],
    "rating": ["rating", "Rating", "ratings", "star"],
    "reviews": ["reviews", "Reviews", "review count", "ratings count", "num reviews"],
}


def _num(v):
    if v is None:
        return None
    s = str(v).strip().replace(",", "").replace("$", "").replace("₹", "").replace("%", "")
    try:
        return float(s)
    except ValueError:
        return None


def _pick(row_lc, names):
    for n in names:
        if n.lower() in row_lc:
            return row_lc[n.lower()]
    return None


def _snapshot_from_name(fn):
    import re
    m = re.search(r"(\d{4})[-_]?(\d{2})[-_]?(\d{2})", fn)
    if m:
        try:
            return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    return dt.date.today()


def ingest_file(cur, path):
    fn = os.path.basename(path)
    report_type = fn.split("-")[0:3]
    report_type = "-".join(p for p in fn.replace(".csv", "").split("-") if not p.isdigit())[:60] or "report"
    snap = _snapshot_from_name(fn)
    rows = []
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for r in reader:
            row_lc = {(k or "").strip().lower(): v for k, v in r.items()}
            asin = _pick(row_lc, ALIASES["asin"]) or ""
            rows.append((
                report_type, asin.strip(),
                _pick(row_lc, ALIASES["title"]), _pick(row_lc, ALIASES["brand"]),
                _pick(row_lc, ALIASES["category"]), _num(_pick(row_lc, ALIASES["price"])),
                _num(_pick(row_lc, ALIASES["monthly_revenue"])), _num(_pick(row_lc, ALIASES["monthly_units"])),
                _num(_pick(row_lc, ALIASES["rating"])),
                int(_num(_pick(row_lc, ALIASES["reviews"])) or 0),
                Json(r), fn, snap,
            ))
    rows = [x for x in rows if x[1]]  # need an ASIN to dedup
    if rows:
        execute_values(cur, """
            INSERT INTO seller_reports
              (report_type,asin,title,brand,category,price,monthly_revenue,monthly_units,rating,reviews,raw,source_file,snapshot_date)
            VALUES %s
            ON CONFLICT (report_type, asin, snapshot_date) DO UPDATE SET
              raw=EXCLUDED.raw, monthly_revenue=EXCLUDED.monthly_revenue, monthly_units=EXCLUDED.monthly_units
        """, rows)
    return len(rows)


def run():
    conn = psycopg2.connect(PG_DSN)
    cur = conn.cursor()
    cur.execute(DDL)
    conn.commit()
    done_dir = os.path.join(DROP, "_done")
    os.makedirs(done_dir, exist_ok=True)
    files = sorted(glob.glob(os.path.join(DROP, "*.csv")))
    total = 0
    for p in files:
        try:
            n = ingest_file(cur, p)
            conn.commit()
            total += n
            shutil.move(p, os.path.join(done_dir, os.path.basename(p)))
            print(f"  {os.path.basename(p)}: {n} rows")
        except Exception as e:
            conn.rollback()
            print(f"  {os.path.basename(p)}: ERROR {repr(e)[:120]}")
    cur.close(); conn.close()
    print(f"done: {len(files)} files, {total} rows into seller_reports")


if __name__ == "__main__":
    run()
