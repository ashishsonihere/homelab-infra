"""Stack Exchange connector — free API (no key for low volume).
Mines Software Recommendations + Webmasters SE: "recommend a tool that does X" = demand + tool-gap signal."""
import httpx
from .base import Connector

SITES = ["softwarerecs", "webmasters"]


class StackExchange(Connector):
    slug = "stackexchange"

    def __init__(self, queries=None, pagesize=50):
        self.queries = queries or ["ecommerce", "shopify", "saas", "automation", "attribution", "amazon seller"]
        self.pagesize = pagesize

    def fetch(self):
        with httpx.Client(timeout=30) as c:
            for site in SITES:
                for q in self.queries:
                    r = c.get(
                        "https://api.stackexchange.com/2.3/search/advanced",
                        params={"order": "desc", "sort": "votes", "q": q, "site": site,
                                "pagesize": self.pagesize, "filter": "withbody"},
                    )
                    if r.status_code != 200:
                        continue
                    for it in r.json().get("items", []):
                        it["_site"] = site
                        it["_q"] = q
                        yield it

    def parse(self, it) -> dict:
        return {
            "ext_id": f"{it.get('_site')}_{it.get('question_id')}",
            "url": it.get("link"),
            "title": it.get("title"),
            "body": it.get("body") or it.get("title") or "",
            "metadata": {
                "site": it.get("_site"), "score": it.get("score"),
                "answers": it.get("answer_count"), "tags": it.get("tags"),
                "is_answered": it.get("is_answered"), "query": it.get("_q"),
            },
        }


if __name__ == "__main__":
    print(StackExchange().run())
