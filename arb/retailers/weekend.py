"""weekend.ee — Estonian sneaker/streetwear retailer.

The storefront is ScandiPWA: a React PWA where the HTML is a JS bundle and
nothing is server-rendered, so there is no ld+json or microdata to parse. The
catalog comes from the Magento GraphQL endpoint instead (same approach as
sportland.py). No auth, no keys — it is the shop's own public storefront API,
used the way the storefront uses it, with our normal politeness delay.

Two things make this retailer worth having:

- **Real per-size stock.** Each configurable variant carries its own
  `stock_status`, so unlike Sportland we never have to guess which sizes exist.
- **Style codes in the SKU.** `NIDM0113-100` is a two-letter brand prefix plus
  the manufacturer code `DM0113-100`, so matching is exact rather than fuzzy.

Brand gate: weekend sells ~47,900 products, the large majority from brands
StockX does not trade. Enumerating by brand (rather than crawling everything
and resolving as we go) keeps us off the API budget for the ~91% that could
never produce an opportunity.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional
from urllib.parse import quote

from ..models import Product, RetailSize
from .base import RetailerScraper

log = logging.getLogger(__name__)

GRAPHQL_URL = "https://www.weekend.ee/graphql"
PRODUCT_URL = "https://www.weekend.ee/{url_key}.html"

# Brands StockX actually trades. Anything else is skipped before it can cost an
# API call. Overridable per-deployment via config `slug_filters`.
DEFAULT_BRANDS = [
    "nike", "jordan", "adidas", "new balance", "asics", "puma", "saucony",
    "crocs", "ugg", "vans", "converse", "hoka", "salomon", "reebok",
]

# Served over GET, so the arguments are inlined rather than passed as GraphQL
# variables. Every interpolated value goes through json.dumps, which is what
# quotes and escapes it — a brand name is never pasted in raw.
_LIST_QUERY = """
{
  products(search: %s, currentPage: %d, pageSize: %d) {
    total_count
    items {
      sku name url_key stock_status type_id
      price_range { minimum_price {
        final_price { value currency }
        regular_price { value }
      } }
    }
  }
}
"""

_VARIANT_QUERY = """
{
  products(filter: { sku: { eq: %s } }, pageSize: 1) {
    items {
      sku name url_key stock_status
      price_range { minimum_price {
        final_price { value currency }
        regular_price { value }
      } }
      ... on ConfigurableProduct {
        variants { product { sku stock_status } }
      }
    }
  }
}
"""

# 'NIDM0113-100' -> 'DM0113-100': a two-letter brand prefix in front of a
# manufacturer style code. Only stripped when what remains still looks like a
# style code, so a SKU that merely starts with two letters is left alone.
_PREFIXED_STYLE = re.compile(r"^[A-Z]{2}([A-Z]{1,2}\d{4,6}-\d{2,3})$")
_PREFIXED_PLAIN = re.compile(r"^[A-Z]{2}([A-Z]{1,2}\d{4,6})$")


def style_code_from_sku(sku: str) -> Optional[str]:
    """The manufacturer style code inside a weekend SKU, or None.

    Getting this wrong fails safe: a code StockX does not recognise simply
    finds no match, because resolution requires an exact styleId equality.
    """
    raw = (sku or "").strip().upper()
    if not raw:
        return None
    for pattern in (_PREFIXED_STYLE, _PREFIXED_PLAIN):
        m = pattern.match(raw)
        if m:
            return m.group(1)
    # already bare, or a shape we do not recognise — hand it over unchanged and
    # let the exact styleId comparison decide
    return raw or None


def size_from_variant_sku(variant_sku: str) -> Optional[str]:
    """'NIDM0113-100/38,5' -> '38.5'. Estonian decimals use a comma."""
    if "/" not in (variant_sku or ""):
        return None
    label = variant_sku.rsplit("/", 1)[-1].strip().replace(",", ".")
    return label or None


class WeekendScraper(RetailerScraper):
    name = "weekend"

    def _brands(self) -> list[str]:
        return [b.lower() for b in (self.cfg.slug_filters or DEFAULT_BRANDS)]

    async def _graphql(self, query: str) -> Optional[dict]:
        """Magento serves GraphQL over GET, so this rides the polite fetcher
        (robots, per-host delay, jitter, circuit breaker) like any other page."""
        raw = await self.fetcher.get(f"{GRAPHQL_URL}?query=" + quote(query))
        if not raw:
            return None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("%s: graphql returned non-JSON", self.name)
            return None
        if payload.get("errors"):
            log.info("%s graphql error: %s", self.name,
                     str(payload["errors"])[:200])
        return (payload.get("data") or {}).get("products")

    async def scan(self) -> list[Product]:
        products: dict[str, Product] = {}
        page_size = 50
        for brand in self._brands():
            for page in range(1, self.cfg.max_pages + 1):
                data = await self._graphql(
                    _LIST_QUERY % (json.dumps(brand), page, page_size))
                if not data:
                    break
                items = data.get("items") or []
                for item in items:
                    product = self._product_from_item(item, brand)
                    if product is not None:
                        products[product.url] = product
                total = data.get("total_count") or 0
                log.info("%s: %s page %d -> %d items (of %d)",
                         self.name, brand, page, len(items), total)
                if len(items) < page_size or page * page_size >= total:
                    break
        return list(products.values())

    def _product_from_item(self, item: dict[str, Any],
                           brand: str) -> Optional[Product]:
        sku = item.get("sku")
        url_key = item.get("url_key")
        if not sku or not url_key:
            return None
        # Only configurable products carry sizes; simple ones here are
        # accessories, which have no StockX market.
        if item.get("type_id") not in (None, "configurable"):
            return None
        style_code = style_code_from_sku(sku)
        if not style_code:
            return None

        price_range = ((item.get("price_range") or {}).get("minimum_price")
                       or {})
        final = (price_range.get("final_price") or {}).get("value")
        regular = (price_range.get("regular_price") or {}).get("value")
        try:
            price = float(final)
        except (TypeError, ValueError):
            return None
        if price <= 0:
            return None
        list_price = None
        try:
            list_price = float(regular)
        except (TypeError, ValueError):
            pass

        return Product(
            retailer=self.name,
            name=str(item.get("name") or "").strip(),
            brand=brand.split()[0].title(),
            style_code=style_code,
            category="sneakers",
            url=PRODUCT_URL.format(url_key=url_key),
            price=price,
            currency=str((price_range.get("final_price") or {}).get("currency")
                         or "EUR"),
            retailer_sku=str(sku),
            sizes=[],                       # filled by enrich()
            in_stock=str(item.get("stock_status")) == "IN_STOCK",
            price_verified=False,           # grid price; enrich() confirms
            on_sale=bool(list_price and list_price > price + 0.01),
            list_price=list_price,
        )

    async def enrich(self, product: Product) -> Optional[Product]:
        """Per-size availability. weekend gives each variant its own
        stock_status, so sizes are REAL here — never synthesized."""
        sku = product.retailer_sku
        if not sku:
            return None
        data = await self._graphql(_VARIANT_QUERY % json.dumps(sku))
        items = (data or {}).get("items") or []
        if not items or items[0].get("sku") != sku:
            # Magento silently IGNORES an unsupported filter and returns an
            # arbitrary product (a url_key filter handed back a gift card), so
            # the identity of what came back must be checked, not assumed.
            log.info("%s: no exact product for sku %s", self.name, sku)
            return None
        item = items[0]

        sizes: list[RetailSize] = []
        for variant in item.get("variants") or []:
            child = variant.get("product") or {}
            label = size_from_variant_sku(str(child.get("sku") or ""))
            if not label:
                continue
            sizes.append(RetailSize(
                label=label, system="EU",
                in_stock=str(child.get("stock_status")) == "IN_STOCK"))

        confirmed = self._product_from_item(
            {**item, "type_id": "configurable"},
            (product.brand or "").lower() or "weekend")
        if confirmed is None:
            return None
        confirmed.sizes = sizes
        confirmed.price_verified = True
        return confirmed

