"""Product Hunt connector — GraphQL API v2 (client-credentials OAuth).
Pulls recent launches (name, tagline, description, votes, comments, topics) = traction + hot-category signal."""
import os
import httpx
from .base import Connector

TOKEN_URL = "https://api.producthunt.com/v2/oauth/token"
GQL_URL = "https://api.producthunt.com/v2/api/graphql"

QUERY = """
query($after:String){
  posts(first:50, after:$after){
    pageInfo{ endCursor hasNextPage }
    edges{ node{ id name tagline description votesCount commentsCount createdAt url
                 topics{ edges{ node{ name } } } } }
  }
}
"""


class ProductHunt(Connector):
    slug = "producthunt"

    def __init__(self, pages=4):
        self.key = os.environ["PRODUCTHUNT_API_KEY"]
        self.secret = os.environ["PRODUCTHUNT_API_SECRET"]
        self.pages = pages

    def _token(self):
        r = httpx.post(TOKEN_URL, json={
            "client_id": self.key, "client_secret": self.secret,
            "grant_type": "client_credentials"}, timeout=30)
        r.raise_for_status()
        return r.json()["access_token"]

    def fetch(self):
        headers = {"Authorization": f"Bearer {self._token()}", "Content-Type": "application/json"}
        after = None
        with httpx.Client(timeout=40, headers=headers) as c:
            for _ in range(self.pages):
                r = c.post(GQL_URL, json={"query": QUERY, "variables": {"after": after}})
                r.raise_for_status()
                data = r.json().get("data", {}).get("posts", {})
                for e in data.get("edges", []):
                    yield e["node"]
                pi = data.get("pageInfo", {})
                if not pi.get("hasNextPage"):
                    break
                after = pi.get("endCursor")

    def parse(self, n) -> dict:
        topics = [t["node"]["name"] for t in n.get("topics", {}).get("edges", [])]
        return {
            "ext_id": str(n["id"]),
            "url": n.get("url"),
            "title": n.get("name"),
            "body": ((n.get("tagline") or "") + "\n" + (n.get("description") or "")).strip(),
            "metadata": {"votes": n.get("votesCount"), "comments": n.get("commentsCount"),
                         "created_at": n.get("createdAt"), "topics": topics},
        }


if __name__ == "__main__":
    print(ProductHunt().run())
