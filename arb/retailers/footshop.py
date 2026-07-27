"""Footshop (footshop.eu) — Tier 3. Astro-rendered storefront.

Verified 2026-07-27:
- robots.txt: Crawl-delay 5 (enforced by the polite fetcher); /api/ and
  /graphql disallowed — untouched. Sitemap index at sitemaps.footshop.eu.
- Product pages carry schema.org microdata as itemProp meta tags: sku/mpn
  (the manufacturer style code), brand, offer price + currency + availability.
  Colorway sits in a _colorWay_* class element.
- Per-size stock is client-side only -> products go out size_stock_unverified
  and alert at product level (the pipeline synthesizes candidate sizes from
  the matched StockX variants).
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from ..models import Product
from .base import RetailerScraper

log = logging.getLogger(__name__)

BASE = "https://www.footshop.eu"
SITEMAP_INDEX = "https://sitemaps.footshop.eu/sitemaps/sitemap_6_index.xml"
_SLUG_RE = r"https://www\.footshop\.eu/(en/[a-z0-9\-]+/\d+-[a-z0-9\-]+\.html)$"
_LINK_RE = re.compile(r'href="(?:https://www\.footshop\.eu)?/(en/[a-z0-9\-]+/\d+-[a-z0-9\-]+\.html)"')

_META_RE = {
    "sku": re.compile(r'itemProp="sku"\s+content="([^"]+)"'),
    "brand": re.compile(r'itemProp="brand"\s+content="([^"]+)"'),
    "price": re.compile(r'itemProp="price"\s+content="([\d.]+)"'),
    "currency": re.compile(r'itemProp="priceCurrency"\s+content="([^"]+)"'),
}
_AVAIL_RE = re.compile(r'itemProp="availability"\s+href="[^"]*/(\w+)"')
_COLOR_RE = re.compile(r'class="_colorWay_[^"]*">([^<]{2,60})<')
_TITLE_RE = re.compile(r"<title>([^<|]{3,90})")


class FootshopScraper(RetailerScraper):
    name = "footshop"

    async def scan(self) -> list[Product]:
        fresh: list[str] = []
        for category in self.cfg.categories:
            html = await self.fetcher.get(f"{BASE}{category}")
            if html:
                fresh.extend(_LINK_RE.findall(html))
        fresh = list(dict.fromkeys(fresh))

        index = await self.fetcher.get(SITEMAP_INDEX)
        product_sitemaps = re.findall(r"<loc>([^<]*sitemap_products[^<]*)</loc>",
                                      index or "")
        backlog = await self.cached_sitemap_slugs(product_sitemaps, _SLUG_RE)
        backlog = [s for s in backlog if self.slug_wanted(s)]
        rotation = self.rotation_slice(backlog, self.cfg.sitemap_slice_per_scan)

        products, seen = [], set()
        for slug in fresh + rotation:
            if slug in seen:
                continue
            seen.add(slug)
            p = await self._fetch_product(slug)
            if p is not None:
                products.append(p)
        log.info("footshop: %d fresh + %d rotation slugs -> %d products",
                 len(fresh), len(rotation), len(products))
        return products

    async def enrich(self, product: Product) -> Optional[Product]:
        slug = product.url.removeprefix(BASE + "/")
        return await self._fetch_product(slug)

    async def _fetch_product(self, slug: str) -> Optional[Product]:
        html = await self.fetcher.get(f"{BASE}/{slug}")
        if not html:
            return None
        meta = {k: (m.group(1) if (m := rx.search(html)) else None)
                for k, rx in _META_RE.items()}
        if not meta["sku"] or not meta["price"]:
            return None
        avail = _AVAIL_RE.search(html)
        in_stock = bool(avail and avail.group(1).lower() == "instock")
        title = _TITLE_RE.search(html)
        color = _COLOR_RE.search(html)
        return Product(
            retailer=self.name,
            name=(title.group(1).strip() if title else meta["sku"]),
            brand=meta["brand"],
            style_code=meta["sku"].strip().upper().replace(" ", "-"),
            colorway=color.group(1).strip() if color else None,
            url=f"{BASE}/{slug}",
            price=float(meta["price"]),
            currency=meta["currency"] or "EUR",
            sizes=[],
            in_stock=in_stock,
            price_verified=True,
            size_stock_unverified=True,
            stock_note="size availability unknown at Footshop — CHECK the page",
        )
