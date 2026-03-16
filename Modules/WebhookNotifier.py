import base64
import os
import json
import asyncio
import aiohttp
from datetime import datetime, timezone


class WebhookRoute:
    def __init__(self, webhook_url, min_discount, max_discount, categories=None, name=""):
        self.webhook_url = webhook_url
        self.min_discount = min_discount
        self.max_discount = max_discount
        self.categories = categories or []
        self.name = name

    def matches(self, discount_pct, category=None):
        if not (self.min_discount <= discount_pct <= self.max_discount):
            return False
        if self.categories and category not in self.categories:
            return False
        return True


class WebhookNotifier:
    def __init__(self, associate_tag=""):
        self.routes = []
        self.associate_tag = associate_tag
        self._load_routes()

    def _load_routes(self):
        raw_b64 = os.environ.get("DEAL_WEBHOOKS_B64", "")
        if raw_b64:
            try:
                raw = base64.b64decode(raw_b64).decode("utf-8")
            except Exception as e:
                print(f"[WebhookNotifier] Failed to decode DEAL_WEBHOOKS_B64: {e}")
                return
        else:
            raw = os.environ.get("DEAL_WEBHOOKS", "")
        if not raw:
            return
        try:
            for r in json.loads(raw):
                self.routes.append(WebhookRoute(
                    webhook_url=r["webhook_url"],
                    min_discount=r.get("min_discount", 0),
                    max_discount=r.get("max_discount", 100),
                    categories=r.get("categories", []),
                    name=r.get("name", ""),
                ))
            print(f"[WebhookNotifier] Loaded {len(self.routes)} webhook route(s)")
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            print(f"[WebhookNotifier] Failed to parse DEAL_WEBHOOKS: {e}")

    @property
    def enabled(self):
        return len(self.routes) > 0

    def make_affiliate_link(self, url):
        if not self.associate_tag or not url:
            return url
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}tag={self.associate_tag}"

    def get_routes_for_deal(self, discount_pct, category=None):
        return [r for r in self.routes if r.matches(discount_pct, category)]

    def _parse_discount(self, raw):
        """Extract integer discount from strings like '85%', '-85', '85'."""
        cleaned = str(raw).replace("%", "").replace("-", "").strip()
        digits = "".join(c for c in cleaned if c.isdigit())
        return int(digits) if digits else 0

    def build_embed(self, deal):
        amz_link = self.make_affiliate_link(deal.get("amz_link", ""))
        discount_pct = self._parse_discount(deal.get("discount", "0"))
        title = deal.get("title", "Unknown Product")[:256]

        embed = {
            "title": title,
            "url": amz_link if amz_link else None,
            "color": 0x2ECC71,
            "thumbnail": {"url": deal.get("img_src", "")},
            "fields": [
                {"name": "Price", "value": f"~~{deal.get('regular_price', '?')}~~ → **{deal.get('discounted_price', '?')}**", "inline": True},
                {"name": "Discount", "value": f"**{discount_pct}% off**", "inline": True},
                {"name": "Fulfillment", "value": deal.get("fulfillment", "?"), "inline": True},
            ],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        if deal.get("shipping"):
            embed["fields"].append({"name": "Shipping", "value": str(deal["shipping"]), "inline": True})
        if deal.get("review") and deal.get("review_count"):
            embed["fields"].append({"name": "Rating", "value": f"⭐ {deal['review']} ({deal['review_count']} reviews)", "inline": True})
        if deal.get("category"):
            embed["fields"].append({"name": "Category", "value": deal["category"], "inline": True})

        return embed

    async def post_deal(self, route, deal):
        embed = self.build_embed(deal)
        payload = {"embeds": [embed]}

        async with aiohttp.ClientSession() as session:
            for attempt in range(3):
                try:
                    async with session.post(route.webhook_url, json=payload) as resp:
                        if resp.status in (200, 204):
                            return True
                        if resp.status == 429:
                            data = await resp.json()
                            retry_after = data.get("retry_after", 5)
                            print(f"[WebhookNotifier] Rate limited on {route.name}, waiting {retry_after}s")
                            await asyncio.sleep(retry_after)
                            continue
                        print(f"[WebhookNotifier] Failed to post to {route.name}: HTTP {resp.status}")
                        return False
                except aiohttp.ClientError as e:
                    print(f"[WebhookNotifier] Connection error posting to {route.name}: {e}")
                    if attempt < 2:
                        await asyncio.sleep(2)
        return False
