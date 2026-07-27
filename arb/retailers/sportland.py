"""Sportland (sportland.ee) — Tier 1, Estonia (regional Baltics chain).
ScandiPWA storefront: pages are client-rendered, but the site's own public
Magento GraphQL endpoint answers plain GET queries.

Verified 2026-07-27:
- robots.txt blocks /search and heavy filter params ("to prevent server
  overload") — we honor the spirit with batched, throttled GraphQL queries.
- Catalog discovery via the advertised sitemap (~20k product URLs like
  /product/nike_dunk_low_dv1748_601); slug_filters in config keep it to
  sneaker brands.
- products(filter:{url_key:{in:[...]}}) returns sku (= style code with
  underscores), name, stock_status, and final price — 40 products per request.
- Per-size data comes from the ConfigurableProduct variants query at enrich
  time (EU size labels, per-variant sku + stock_status).
"""
from __future__ import annotations

import json
import logging
import re
from typing import Optional
from urllib.parse import quote

from ..matching import style_code_candidates_from_sku
from ..models import Product, RetailSize
from .base import RetailerScraper

log = logging.getLogger(__name__)

_SLUG_RE_TMPL = r"https://{host}/product/([a-z0-9_\-]{{3,120}})$"

_LIST_QUERY = """
{ products(filter: {url_key: {in: [%s]}}, pageSize: %d) { items {
    sku name url_key stock_status
    price_range { minimum_price { final_price { value currency } } }
} } }"""

# NOTE (verified 2026-07-27): their fork returns attributes: null on variants
# and 500s on configurable_product_options_selection — so the size LABELS and
# the COUNT of in-stock variants are knowable, but not which label is in
# stock. Products therefore carry size_stock_unverified=True and alert at
# product level with an explicit caveat instead of guessing.
_DETAIL_QUERY = """
{ products(filter: {url_key: {eq: "%s"}}) { items {
    sku name stock_status
    price_range { minimum_price { final_price { value currency } } }
    ... on ConfigurableProduct {
      configurable_options { attribute_code values { label } }
      variants { product { sku stock_status } }
    }
} } }"""


class SportlandScraper(RetailerScraper):
    """sportland.ee by default; the LV/LT subclasses below reuse everything —
    the whole Sportland group runs one ScandiPWA platform (verified 2026-07-27)."""

    name = "sportland"
    base = "https://sportland.ee"
    sitemap_path = "/media/sitemap_EE.xml"

    async def _graphql(self, query: str) -> Optional[dict]:
        raw = await self.fetcher.get(f"{self.base}/graphql?query=" + quote(query))
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if data.get("errors"):
            log.info("sportland graphql error: %s",
                     str(data["errors"])[:200])
        return data.get("data")

    async def scan(self) -> list[Product]:
        slug_re = _SLUG_RE_TMPL.format(host=re.escape(self.base.removeprefix("https://")))
        backlog = await self.cached_sitemap_slugs([self.base + self.sitemap_path],
                                                  slug_re)
        backlog = [s for s in backlog if self.slug_wanted(s)]
        rotation = self.rotation_slice(backlog, self.cfg.sitemap_slice_per_scan)

        products: list[Product] = []
        batch_size = max(1, self.cfg.graphql_batch_size)
        for i in range(0, len(rotation), batch_size):
            batch = rotation[i:i + batch_size]
            quoted = ",".join(f'"{s}"' for s in batch)
            data = await self._graphql(_LIST_QUERY % (quoted, batch_size))
            items = (((data or {}).get("products") or {}).get("items")) or []
            for item in items:
                p = self._product_from_item(item)
                if p is not None:
                    products.append(p)
        log.info("sportland: %d/%d slugs this scan -> %d products",
                 len(rotation), len(backlog), len(products))
        return products

    def _product_from_item(self, item: dict) -> Optional[Product]:
        sku = (item.get("sku") or "").strip()
        url_key = (item.get("url_key") or "").strip()
        if not sku or not url_key:
            return None
        price = (((item.get("price_range") or {}).get("minimum_price") or {})
                 .get("final_price") or {})
        value = price.get("value")
        if not value or value <= 0:
            return None
        candidates = style_code_candidates_from_sku(sku)
        return Product(
            retailer=self.name,
            name=(item.get("name") or url_key).strip().title(),
            style_code=candidates[0] if candidates else None,
            url=f"{self.base}/product/{url_key}",
            price=round(float(value), 2),
            currency=price.get("currency") or "EUR",
            sizes=[],
            in_stock=item.get("stock_status") == "IN_STOCK",
            price_verified=False,
        )

    async def enrich(self, product: Product) -> Optional[Product]:
        url_key = product.url.rsplit("/", 1)[-1]
        data = await self._graphql(_DETAIL_QUERY % url_key)
        items = (((data or {}).get("products") or {}).get("items")) or []
        if not items:
            return None
        item = items[0]
        enriched = product.model_copy(deep=True)

        price = (((item.get("price_range") or {}).get("minimum_price") or {})
                 .get("final_price") or {})
        if price.get("value"):
            enriched.price = round(float(price["value"]), 2)
            enriched.currency = price.get("currency") or "EUR"

        labels: list[str] = []
        for opt in item.get("configurable_options") or []:
            if "size" in (opt.get("attribute_code") or ""):
                labels = [str(v.get("label") or "").replace(",", ".").strip()
                          for v in opt.get("values") or [] if v.get("label")]
        variants = item.get("variants") or []
        in_stock_count = sum(1 for v in variants
                             if (v.get("product") or {}).get("stock_status") == "IN_STOCK")
        parent_in_stock = item.get("stock_status") == "IN_STOCK" and in_stock_count > 0

        enriched.sizes = [RetailSize(label=lbl, system="EU",
                                     in_stock=parent_in_stock)
                          for lbl in sorted(set(labels))]
        enriched.in_stock = parent_in_stock
        enriched.size_stock_unverified = True
        enriched.stock_note = (f"{in_stock_count}/{len(variants)} sizes in stock — "
                               "size-level stock unverified, CHECK the page")
        enriched.price_verified = True
        return enriched


class SportlandLVScraper(SportlandScraper):
    name = "sportland_lv"
    base = "https://sportland.lv"
    sitemap_path = "/media/sitemap_LV.xml"


class SportlandLTScraper(SportlandScraper):
    name = "sportland_lt"
    base = "https://sportland.lt"
    sitemap_path = "/media/sitemap_LT.xml"
