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

# Per-size stock IS knowable here (verified 2026-08-03), despite an earlier
# read of this API concluding otherwise. `variants { attributes }` really does
# come back null, which is what that conclusion rested on — but each CHILD
# product exposes `footwear_size` as a raw option value_index, and
# `configurable_options.values` maps value_index -> human label. Joining the
# two gives an exact size->stock table.
#
# This matters: without the join every size was marked unknown, so the scanner
# treated all of them as buyable and alerted on whichever carried the best bid.
# On the Nike Lunar Force Duckboot that meant three alerts for EU 45.5 when
# only EU 42 was in stock — the other ten sizes were OUT_OF_STOCK the whole
# time and the API had been saying so.
_DETAIL_QUERY = """
{ products(filter: {url_key: {eq: "%s"}}) { items {
    sku name stock_status
    price_range { minimum_price { final_price { value currency } } }
    ... on ConfigurableProduct {
      configurable_options { attribute_code values { label value_index } }
      variants { product { sku stock_status footwear_size } }
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

        sizes, unresolved = self._sizes_with_stock(item)
        variants = item.get("variants") or []
        in_stock_count = sum(1 for s in sizes if s.in_stock)
        parent_in_stock = item.get("stock_status") == "IN_STOCK" and bool(variants)

        enriched.sizes = sizes
        enriched.in_stock = parent_in_stock and (in_stock_count > 0 or unresolved)
        # Only fall back to the old product-level caveat when the join actually
        # failed (a variant whose footwear_size is missing from the option map).
        # Otherwise every size below carries real, retailer-confirmed stock and
        # the scanner can name one with confidence.
        enriched.size_stock_unverified = unresolved
        if unresolved:
            enriched.stock_note = (
                f"{in_stock_count}/{len(variants)} sizes in stock — size-level "
                "stock could not be resolved for this product, CHECK the page")
        else:
            enriched.stock_note = (f"{in_stock_count}/{len(variants)} sizes "
                                   "in stock (per-size verified)")
        enriched.price_verified = True
        return enriched

    @staticmethod
    def _sizes_with_stock(item: dict) -> tuple[list[RetailSize], bool]:
        """Exact size -> stock, by joining each variant's footwear_size
        value_index against the configurable option's index -> label map.

        Returns (sizes, unresolved). `unresolved` is True when any variant
        could not be mapped to a label, which is the only case where we fall
        back to treating per-size stock as unknown — never guess which size a
        variant is.
        """
        index_to_label: dict[str, str] = {}
        for opt in item.get("configurable_options") or []:
            if "size" not in (opt.get("attribute_code") or ""):
                continue
            for value in opt.get("values") or []:
                label = str(value.get("label") or "").replace(",", ".").strip()
                index = value.get("value_index")
                if label and index is not None:
                    index_to_label[str(index)] = label

        by_label: dict[str, bool] = {}
        unresolved = False
        for variant in item.get("variants") or []:
            child = variant.get("product") or {}
            label = index_to_label.get(str(child.get("footwear_size")))
            if label is None:
                unresolved = True
                continue
            in_stock = child.get("stock_status") == "IN_STOCK"
            # a label appearing twice is in stock if ANY of its variants is
            by_label[label] = by_label.get(label, False) or in_stock

        if not by_label:
            # nothing joined at all: fall back to bare labels, stock unknown
            return ([RetailSize(label=lbl, system="EU", in_stock=None)
                     for lbl in sorted(index_to_label.values())], True)

        return ([RetailSize(label=lbl, system="EU", in_stock=stock)
                 for lbl, stock in sorted(by_label.items())], unresolved)


class SportlandLVScraper(SportlandScraper):
    name = "sportland_lv"
    base = "https://sportland.lv"
    sitemap_path = "/media/sitemap_LV.xml"


class SportlandLTScraper(SportlandScraper):
    name = "sportland_lt"
    base = "https://sportland.lt"
    sitemap_path = "/media/sitemap_LT.xml"
