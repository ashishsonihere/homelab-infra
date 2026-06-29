"""News / RSS connector — free feeds (TechCrunch, etc.). stdlib XML parse, no extra deps.
Add any RSS feed (startup news, VC blogs) to FEEDS and it flows into `documents`."""
import urllib.request
import xml.etree.ElementTree as ET
from .base import Connector

FEEDS = {
    "techcrunch": "https://techcrunch.com/feed/",
    "techcrunch_startups": "https://techcrunch.com/category/startups/feed/",
    # add: VC blogs, Indie Hackers RSS, product blogs, etc.
}


class NewsRSS(Connector):
    slug = "news_rss"

    def __init__(self, feeds=None):
        self.feeds = feeds or FEEDS

    def fetch(self):
        for name, url in self.feeds.items():
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "market-research/1.0"})
                data = urllib.request.urlopen(req, timeout=30).read()
                root = ET.fromstring(data)
                for item in root.iter("item"):
                    g = lambda t: (item.findtext(t) or "")
                    yield {
                        "_feed": name, "guid": g("guid") or g("link"),
                        "title": g("title"), "link": g("link"),
                        "desc": g("description"), "pub": g("pubDate"),
                    }
            except Exception:
                continue

    def parse(self, it) -> dict:
        return {
            "ext_id": it.get("guid"),
            "url": it.get("link"),
            "title": it.get("title"),
            "body": it.get("desc") or it.get("title") or "",
            "metadata": {"feed": it.get("_feed"), "published": it.get("pub")},
        }


if __name__ == "__main__":
    print(NewsRSS().run())
