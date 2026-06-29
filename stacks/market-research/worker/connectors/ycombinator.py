"""Y Combinator companies connector — free open-source yc-oss API (no key).
Source: https://yc-oss.github.io/api/companies/all.json (~6k companies, refreshed regularly)."""
import httpx
from .base import Connector

URL = "https://yc-oss.github.io/api/companies/all.json"


class YCombinator(Connector):
    slug = "ycombinator"

    def fetch(self):
        with httpx.Client(timeout=120, headers={"User-Agent": "market-research/1.0"}) as c:
            r = c.get(URL)
            r.raise_for_status()
            for co in r.json():
                yield co

    def parse(self, co) -> dict:
        return {
            "ext_id": str(co.get("id") or co.get("slug")),
            "url": co.get("website") or f"https://www.ycombinator.com/companies/{co.get('slug')}",
            "title": co.get("name"),
            "body": co.get("long_description") or co.get("one_liner") or "",
            "metadata": {
                "one_liner": co.get("one_liner"), "batch": co.get("batch"),
                "industry": co.get("industry"), "subindustry": co.get("subindustry"),
                "tags": co.get("tags"), "team_size": co.get("team_size"),
                "locations": co.get("all_locations"), "top_company": co.get("top_company"),
                "is_hiring": co.get("isHiring"), "status": co.get("status"),
                "launched_at": co.get("launched_at"),
            },
        }


if __name__ == "__main__":
    print(YCombinator().run())
