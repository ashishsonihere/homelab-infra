"""Reddit public-feed connector — free .json endpoints, NO OAuth (breadth now; the PRAW worker adds
full nested comment trees later, once creds exist). Polite User-Agent + pacing required."""
import time
import httpx
from .base import Connector

UA = "linux:market-research:v1.0 (by u/ashish)"


class RedditFeed(Connector):
    slug = "reddit_feed"

    def __init__(self, subreddits=None, listing="new", limit=100):
        self.subs = subreddits or [
            "shopify", "ecommerce", "FulfillmentByAmazon", "AmazonSeller", "EtsySellers",
        ]
        self.listing = listing
        self.limit = limit

    def fetch(self):
        with httpx.Client(timeout=30, headers={"User-Agent": UA}) as c:
            for sub in self.subs:
                r = c.get(f"https://www.reddit.com/r/{sub}/{self.listing}.json",
                          params={"limit": self.limit})
                if r.status_code != 200:
                    continue
                for child in r.json().get("data", {}).get("children", []):
                    d = child.get("data", {})
                    d["_sub"] = sub
                    yield d
                time.sleep(2)  # be polite to the free endpoint

    def parse(self, d) -> dict:
        return {
            "ext_id": d.get("id"),
            "url": "https://www.reddit.com" + (d.get("permalink") or ""),
            "title": d.get("title"),
            "body": d.get("selftext") or d.get("title") or "",
            "metadata": {
                "sub": d.get("_sub"), "score": d.get("score"),
                "num_comments": d.get("num_comments"), "author": d.get("author"),
                "created_utc": d.get("created_utc"), "flair": d.get("link_flair_text"),
            },
        }


if __name__ == "__main__":
    print(RedditFeed().run())
