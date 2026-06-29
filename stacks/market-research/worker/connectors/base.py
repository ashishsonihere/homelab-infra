"""Connector framework. Each Tier-1 source subclasses Connector and implements fetch()+parse().

Contract:
  fetch()  -> yields raw items (dict) from an API / RSS / JSON / scrape
  parse(raw) -> dict with keys: ext_id, url, title, body, metadata
  run()    -> idempotent upsert of documents (ON CONFLICT). No rebuild to add a source.
"""
import os
import hashlib
import psycopg2
from psycopg2.extras import Json

PG_DSN = os.environ.get("PG_DSN", "postgresql://devcore@devcore-postgres:5432/market_research")


def db():
    return psycopg2.connect(PG_DSN)


def content_hash(s: str) -> str:
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()


class Connector:
    slug = "base"

    def fetch(self):
        raise NotImplementedError

    def parse(self, raw) -> dict:
        raise NotImplementedError

    def run(self) -> str:
        conn = db()
        cur = conn.cursor()
        n = 0
        for raw in self.fetch():
            row = self.parse(raw)
            if not row or not row.get("ext_id"):
                continue
            cur.execute(
                """
                INSERT INTO documents (source_slug, ext_id, url, title, body, content_hash, metadata)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (source_slug, ext_id) DO UPDATE
                  SET title = EXCLUDED.title, body = EXCLUDED.body, metadata = EXCLUDED.metadata
                """,
                (self.slug, row["ext_id"], row.get("url"), row.get("title"),
                 row.get("body"), content_hash(row.get("body", "")), Json(row.get("metadata", {}))),
            )
            n += 1
        conn.commit()
        cur.close()
        conn.close()
        return f"{self.slug}: upserted {n} documents"
