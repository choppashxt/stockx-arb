"""Overkill (overkillshop.com) — Berlin sneaker boutique, Shopify.

Verified 2026-07-27:
- robots.txt clean, no crawl-delay; product sitemaps advertised.
- Ships to Estonia: DHL EUR13, 2-5 working days (set as extra_cost_eur in
  config so profit is judged on landed cost, not shelf price).
- Product pages carry JSON-LD with per-size offers: price, availability, GTIN,
  and a sku of the form "<internal article>-<EU size>" (half sizes appear as
  "-36-5"). The internal article is NOT the manufacturer code.
- The manufacturer style code is printed in the product details as
  "Manufacturer-#: DQ4121 001" (space instead of hyphen) and repeated in the
  og:description. That is the field we match StockX on.

Known limitation: their JSON-LD exposes only a handful of offers per product
(observed 5, whole sizes), so some sizes — including half sizes — are invisible
to us. That costs coverage, never correctness: an unseen size simply never
alerts. Revisit by reading the Shopify variants blob if coverage matters.
"""
from __future__ import annotations

import html as html_mod
import json
import logging
import re
from typing import Optional

from ..models import Product, RetailSize
from .base import RetailerScraper

log = logging.getLogger(__name__)

BASE = "https://www.overkillshop.com"
_SLUG_RE = r"https://www\.overkillshop\.com/products/([a-z0-9\-]{3,120})$"
_JSONLD_RE = re.compile(r'<script[^>]*ld\+json[^>]*>(.*?)</script>', re.S | re.I)
_LINK_RE = re.compile(r'href="[^"]*/products/([a-z0-9\-]{3,120})"')
# "Manufacturer-#: DQ4121 001" or "Manufacturer-#: IE7002"
_MFR_RE = re.compile(
    r"Manufacturer-#\s*:?\s*([A-Z0-9]{4,12}(?:[ \-][A-Z0-9]{2,4})?)", re.I)
_OG_CODE_RE = re.compile(
    r'og:description"\s+content="[^"]*?\|\s*([A-Z0-9]{4,12}(?:\s+\d{3})?)\s*\|')


def _eu_size_from_sku(offer_sku: str, parent_sku: str) -> Optional[str]:
    """'103181-36' -> '36', '103181-36-5' -> '36.5'. Never invents a size."""
    tail = offer_sku[len(parent_sku):] if offer_sku.startswith(parent_sku) else ""
    tail = tail.lstrip("-")
    if not tail:
        # parent sku already carries the size (single-variant listings)
        parts = offer_sku.split("-")
        tail = "-".join(parts[1:]) if len(parts) > 1 else ""
    m = re.fullmatch(r"(\d{2})(?:-(\d))?", tail)
    if not m:
        return None
    return f"{m.group(1)}.{m.group(2)}" if m.group(2) else m.group(1)


class OverkillScraper(RetailerScraper):
    name = "overkill"

    async def scan(self) -> list[Product]:
        fresh: list[str] = []
        for category in self.cfg.categories:
            html = await self.fetcher.get(f"{BASE}{category}")
            if html:
                fresh.extend(_LINK_RE.findall(html))
        fresh = list(dict.fromkeys(fresh))

        index = await self.fetcher.get(f"{BASE}/sitemap.xml") or ""
        # they paginate into 40+ files AND repeat every one under /de/ — take
        # only the canonical English set, newest first (highest id ranges)
        sitemaps = [html_mod.unescape(u) for u in
                    re.findall(r"<loc>([^<]*sitemap_products[^<]*)</loc>", index)
                    if "/de/" not in u]
        sitemaps.reverse()
        backlog = await self.cached_sitemap_slugs(sitemaps, _SLUG_RE,
                                                  max_sitemaps=8)
        backlog = [s for s in backlog if self.slug_wanted(s)]
        rotation = self.rotation_slice(backlog, self.cfg.sitemap_slice_per_scan)

        products, seen = [], set()
        for handle in fresh + rotation:
            if handle in seen:
                continue
            seen.add(handle)
            p = await self._fetch_product(handle)
            if p is not None:
                products.append(p)
        log.info("overkill: %d fresh + %d rotation handles -> %d products",
                 len(fresh), len(rotation), len(products))
        return products

    async def enrich(self, product: Product) -> Optional[Product]:
        return await self._fetch_product(product.url.rsplit("/", 1)[-1])

    async def _fetch_product(self, handle: str) -> Optional[Product]:
        html = await self.fetcher.get(f"{BASE}/products/{handle}")
        if not html:
            return None
        node = self._jsonld_product(html)
        if node is None:
            return None

        style = None
        if (m := _MFR_RE.search(re.sub(r"<[^>]+>", " ", html))):
            style = re.sub(r"[ ]+", "-", m.group(1).strip()).upper()
        elif (m := _OG_CODE_RE.search(html)):
            style = re.sub(r"\s+", "-", m.group(1).strip()).upper()
        if not style:
            return None          # no manufacturer code -> unmatchable, skip

        parent_sku = str(node.get("sku") or "").split("-")[0]
        offers = node.get("offers") or []
        if isinstance(offers, dict):
            offers = [offers]
        sizes, prices = [], []
        for offer in offers:
            try:
                prices.append(float(offer.get("price")))
            except (TypeError, ValueError):
                continue
            label = _eu_size_from_sku(str(offer.get("sku") or ""), parent_sku)
            if label is None:
                continue
            sizes.append(RetailSize(
                label=label,
                system="EU",     # German store: EU sizing on the sku suffix
                ean=str(offer.get("gtin13") or offer.get("gtin12") or "") or None,
                in_stock="instock" in str(offer.get("availability", "")).lower(),
            ))
        if not prices:
            return None

        brand = node.get("brand")
        if isinstance(brand, dict):
            brand = brand.get("name")

        return Product(
            retailer=self.name,
            name=html_mod.unescape(str(node.get("name") or handle)).strip(),
            brand=brand,
            style_code=style,
            url=f"{BASE}/products/{handle}",
            price=min(prices),
            currency=str((offers[0] or {}).get("priceCurrency") or "EUR"),
            sizes=sizes,
            in_stock=any(s.in_stock for s in sizes),
            price_verified=True,
        )

    @staticmethod
    def _jsonld_product(html: str) -> Optional[dict]:
        for m in _JSONLD_RE.finditer(html):
            try:
                data = json.loads(m.group(1).strip())
            except json.JSONDecodeError:
                continue
            for node in (data if isinstance(data, list) else [data]):
                if isinstance(node, dict) and node.get("@type") == "Product":
                    return node
        return None
