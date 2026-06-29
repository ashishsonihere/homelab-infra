"""Index documents into Meilisearch for fast keyword search."""
import os
import httpx
import psycopg2

PG_DSN = os.environ["PG_DSN"]
MEILI_URL = os.environ.get("MEILI_URL", "http://mr-meilisearch:7700")
MEILI_KEY = os.environ["MEILI_MASTER_KEY"]


def run():
    conn = psycopg2.connect(PG_DSN)
    cur = conn.cursor()
    cur.execute("SELECT id::text, source_slug, coalesce(title,''), left(coalesce(body,''),2000), coalesce(url,'') FROM documents")
    docs = [{"id": r[0], "source": r[1], "title": r[2], "body": r[3], "url": r[4]} for r in cur.fetchall()]
    h = {"Authorization": f"Bearer {MEILI_KEY}", "Content-Type": "application/json"}
    with httpx.Client(timeout=120, headers=h) as c:
        c.patch(f"{MEILI_URL}/indexes/documents/settings",
                json={"searchableAttributes": ["title", "body"], "filterableAttributes": ["source"]})
        for i in range(0, len(docs), 1000):
            c.post(f"{MEILI_URL}/indexes/documents/documents", json=docs[i:i + 1000])
    print(f"queued {len(docs)} docs into Meilisearch index 'documents'")


if __name__ == "__main__":
    run()
