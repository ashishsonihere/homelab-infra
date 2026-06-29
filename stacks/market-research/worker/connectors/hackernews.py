"""Hacker News connector — free Algolia HN Search API (no key, ToS-clean).
First Tier-1 connector: proves the framework end-to-end without any credentials or proxies.

Run:  python -m connectors.hackernews
"""
import httpx
from .base import Connector


class HackerNews(Connector):
    slug = "hackernews"

    def __init__(self, queries=None, hits=100):
        # Seed queries — tune toward the wedge being validated.
        self.queries = queries or [
            "shopify app", "ecommerce tool", "founder pain point",
            "saas idea", "attribution analytics", "amazon seller",
        ]
        self.hits = hits

    def fetch(self):
        with httpx.Client(timeout=30) as c:
            for q in self.queries:
                r = c.get(
                    "https://hn.algolia.com/api/v1/search",
                    params={"query": q, "tags": "story", "hitsPerPage": self.hits},
                )
                r.raise_for_status()
                for hit in r.json().get("hits", []):
                    hit["_query"] = q
                    yield hit

    def parse(self, hit) -> dict:
        oid = hit.get("objectID")
        return {
            "ext_id": str(oid),
            "url": hit.get("url") or f"https://news.ycombinator.com/item?id={oid}",
            "title": hit.get("title"),
            "body": hit.get("story_text") or hit.get("title") or "",
            "metadata": {
                "points": hit.get("points"),
                "num_comments": hit.get("num_comments"),
                "author": hit.get("author"),
                "created_at": hit.get("created_at"),
                "query": hit.get("_query"),
            },
        }


if __name__ == "__main__":
    print(HackerNews().run())
