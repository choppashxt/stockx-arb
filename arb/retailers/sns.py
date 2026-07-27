"""Sneakersnstuff (sneakersnstuff.com) — Tier 3. Shopify storefront.

Verified 2026-07-27:
- robots.txt: Crawl-delay 10 (the polite fetcher enforces it), standard
  Shopify disallows only (cart/checkout/filtered collections).
- Product pages ship JSON-LD with PER-SIZE offers: sku "HF6881-900-7"
  (style code + US size), gtin12 per size, EUR price, availability per size.
- Style code also sits in the product handle (nike-clogposite-hf6881-900).
- Catalog via /sitemap_products_N.xml; fresh discounts via the sale
  collection page(s) configured in `categories`.
"""
from __future__ import annotations

import html as html_mod
import json
import logging
import re
from typing import Optional

from ..matching import style_code_candidates_from_sku
from ..models import Product, RetailSize
from .base import RetailerScraper

log = logging.getLogger(__name__)

BASE = "https://www.sneakersnstuff.com"
_SLUG_RE = r"https://www\.sneakersnstuff\.com/products/([a-z0-9\-]{3,120})$"
_JSONLD_RE = re.compile(r'<script[^>]*ld\+json[^>]*>(.*?)</script>', re.S | re.I)
_HANDLE_RE = re.compile(r'href="[^"]*/products/([a-z0-9\-]{3,120})"')
_US_SUFFIX_RE = re.compile(r"[-_](\d{1,2}(?:\.5)?)$")


class SnsScraper(RetailerScraper):
    name = "sns"

    async def scan(self) -> list[Product]:
        # fresh: configured collection pages (e.g. /collections/sale)
        fresh: list[str] = []
        for category in self.cfg.categories:
            html = await self.fetcher.get(f"{BASE}{category}")
            if html:
                fresh.extend(_HANDLE_RE.findall(html))
        fresh = list(dict.fromkeys(fresh))

        # Shopify's paginated product sitemaps 400 without the ?from=&to=
        # params — always take their URLs (entity-escaped) from the index
        index = await self.fetcher.get(f"{BASE}/sitemap.xml") or ""
        sitemaps = [html_mod.unescape(u)
                    for u in re.findall(r"<loc>([^<]*sitemap_products[^<]*)</loc>",
                                        index)]
        backlog = await self.cached_sitemap_slugs(sitemaps, _SLUG_RE)
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
        log.info("sns: %d fresh + %d rotation handles -> %d products",
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

        offers = node.get("offers") or []
        if isinstance(offers, dict):
            offers = [offers]
        sizes: list[RetailSize] = []
        prices: list[float] = []
        for offer in offers:
            sku = str(offer.get("sku") or "")
            m = _US_SUFFIX_RE.search(sku)
            us = m.group(1) if m else None
            try:
                prices.append(float(offer.get("price")))
            except (TypeError, ValueError):
                continue
            in_stock = "instock" in str(offer.get("availability", "")).lower()
            sizes.append(RetailSize(
                label=us or str(offer.get("name") or "?").rsplit("-", 1)[-1].strip(),
                system="US",
                us_size=us,
                ean=str(offer.get("gtin12") or offer.get("gtin13") or "") or None,
                in_stock=in_stock,
            ))
        if not prices:
            return None

        parent_sku = str(node.get("sku") or "")
        style = None
        if parent_sku:
            candidates = style_code_candidates_from_sku(parent_sku.replace("-", "_"))
            style = candidates[0] if candidates else None
        if not style:
            m = re.search(r"([a-z]{1,3}\d{4,6}-\d{3})$", handle)
            style = m.group(1).upper() if m else None

        brand = node.get("brand")
        if isinstance(brand, dict):
            brand = brand.get("name")

        return Product(
            retailer=self.name,
            name=str(node.get("name") or handle).strip(),
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
            nodes = data if isinstance(data, list) else [data]
            for node in nodes:
                if isinstance(node, dict) and node.get("@type") == "Product":
                    return node
        return None
