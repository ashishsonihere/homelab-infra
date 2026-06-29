"""Embed documents with Ollama (nomic-embed-text, local/free) -> chunks.embedding (pgvector)."""
import os
import httpx
import psycopg2
from psycopg2.extras import Json

PG_DSN = os.environ["PG_DSN"]
OLLAMA = os.environ.get("OLLAMA_URL", "http://mr-ollama:11434")
LIMIT = int(os.environ.get("EMBED_MAX", "1500"))


def embed(text):
    r = httpx.post(f"{OLLAMA}/api/embeddings",
                   json={"model": "nomic-embed-text", "prompt": text[:2000]}, timeout=60)
    r.raise_for_status()
    return r.json()["embedding"]


def run():
    conn = psycopg2.connect(PG_DSN)
    cur = conn.cursor()
    cur.execute("""SELECT id, left(coalesce(title,'')||'. '||coalesce(body,''),2000), source_slug
                   FROM documents WHERE length(coalesce(body,''))>30
                   ORDER BY fetched_at DESC LIMIT %s""", (LIMIT,))
    n = 0
    for did, text, src in cur.fetchall():
        try:
            emb = embed(text)
        except Exception as e:
            print("  embed err:", repr(e)[:80]); continue
        emb_str = "[" + ",".join(str(x) for x in emb) + "]"
        cur.execute("INSERT INTO chunks (content, embedding, metadata) VALUES (%s, %s::vector, %s)",
                    (text, emb_str, Json({"document_id": str(did), "source": src})))
        n += 1
        if n % 200 == 0:
            conn.commit(); print("  embedded", n)
    conn.commit(); cur.close(); conn.close()
    print(f"embedded {n} docs into chunks")


if __name__ == "__main__":
    run()
