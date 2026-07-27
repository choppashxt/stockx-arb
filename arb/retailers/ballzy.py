"""Ballzy (ballzy.eu) — Tier 1, Estonia. Next.js storefront over a Magento
backend; category grids and product pages are server-rendered with the product
data embedded in RSC flight chunks (self.__next_f.push).

Verified 2026-07-27:
- robots.txt allows category/product pages (Crawl-delay 2; faceted filter
  params and /catalogsearch/ are disallowed — we use neither).
- Grid objects carry: sku ("HQ2010_005_8.5_CNF" = style code + size + CNF),
  name, brands_value, color_value, url_key[], size_value[] (EU labels of
  available sizes), regular_price, discount_percentage.
- Product pages carry JSON-LD (authoritative price) and per-size variants:
  EU label <-> variant sku whose suffix is the US size, + stock_status.
  That suffix is the retailer's own EU->US mapping, so no size guessing.
- Pagination via ?page=N, 60 products per page.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Iterator, Optional

from ..matching import style_code_candidates_from_sku
from ..models import Product, RetailSize
from .base import RetailerScraper

log = logging.getLogger(__name__)

BASE = "https://ballzy.eu"
PAGE_SIZE = 60

_FLIGHT_RE = re.compile(r'self\.__next_f\.push\(\[1,\s*"((?:[^"\\]|\\.)*)"\]\)')
_GRID_ITEM_MARKER = '{"store_id":"1","sku":"'
_JSONLD_RE = re.compile(
    r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', re.S | re.I)
_EU_LABEL_RE = re.compile(r"^\d{2}(\.5)?$|^\d{2} 1/2$|^\d{2} 1/3$|^\d{2} 2/3$")
_US_SUFFIX_RE = re.compile(r"^\d{1,2}(\.\d)?$")


def _decode_flight(html: str) -> str:
    chunks = []
    for m in _FLIGHT_RE.finditer(html):
        try:
            chunks.append(json.loads('"' + m.group(1) + '"'))
        except json.JSONDecodeError:
            continue
    return "".join(chunks)


def _iter_json_objects(text: str, marker: str) -> Iterator[dict]:
    decoder = json.JSONDecoder()
    start = 0
    while (i := text.find(marker, start)) != -1:
        try:
            obj, end = decoder.raw_decode(text, i)
            yield obj
            start = end
        except json.JSONDecodeError:
            start = i + len(marker)


class BallzyScraper(RetailerScraper):
    name = "ballzy"

    # -- catalog sweep -------------------------------------------------------
    async def scan(self) -> list[Product]:
        products: dict[str, Product] = {}
        for category in self.cfg.categories:
            for page in range(1, self.cfg.max_pages + 1):
                url = f"{BASE}{category}" + (f"?page={page}" if page > 1 else "")
                html = await self.fetcher.get(url)
                if html is None:
                    break
                items = list(_iter_json_objects(_decode_flight(html),
                                                _GRID_ITEM_MARKER))
                if not items:
                    break
                for item in items:
                    p = self._product_from_grid_item(item)
                    if p is not None:
                        products.setdefault(p.url, p)
                log.info("ballzy %s page %d: %d items", category, page, len(items))
                if len(items) < PAGE_SIZE:
                    break
        return list(products.values())

    def _product_from_grid_item(self, item: dict) -> Optional[Product]:
        sku = _first_str(item.get("sku"))
        url_key = _first_str(item.get("url_key"))
        if not sku or not url_key:
            return None
        candidates = style_code_candidates_from_sku(sku)

        regular = _to_float(item.get("regular_price"))
        discount = _to_float(item.get("discount_percentage")) or 0.0
        if regular is None or regular <= 0:
            return None
        price = round(regular * (1 - discount / 100.0), 2)

        size_labels = [str(s) for s in item.get("size_value") or []]
        sizes = [RetailSize(label=lbl,
                            system="EU" if _EU_LABEL_RE.match(lbl) else "letter")
                 for lbl in size_labels]

        return Product(
            retailer=self.name,
            name=(_first_str(item.get("name")) or sku).title(),
            brand=_first_str(item.get("brands_value")) or None,
            style_code=candidates[0] if candidates else None,
            colorway=_first_str(item.get("color_value")) or None,
            url=f"{BASE}/en/product/{url_key}",
            price=price,
            currency="EUR",
            sizes=sizes,
            in_stock=bool(sizes),
            price_verified=False,
        )

    # -- product-page confirmation ------------------------------------------
    async def enrich(self, product: Product) -> Optional[Product]:
        html = await self.fetcher.get(product.url)
        if html is None:
            return None

        price, currency, offer_in_stock = self._parse_jsonld_offer(html)
        sizes = self._parse_variant_sizes(_decode_flight(html))

        enriched = product.model_copy(deep=True)
        if price is not None:
            enriched.price = price
            enriched.currency = currency or "EUR"
        if sizes:
            enriched.sizes = sizes
            enriched.in_stock = any(s.in_stock for s in sizes)
        elif offer_in_stock is not None:
            enriched.in_stock = offer_in_stock
        enriched.price_verified = price is not None
        return enriched

    @staticmethod
    def _parse_jsonld_offer(html: str) -> tuple[Optional[float], Optional[str],
                                                Optional[bool]]:
        for m in _JSONLD_RE.finditer(html):
            try:
                data = json.loads(m.group(1).strip())
            except json.JSONDecodeError:
                continue
            nodes = data if isinstance(data, list) else [data]
            for node in nodes:
                if not isinstance(node, dict) or node.get("@type") != "Product":
                    continue
                offers = node.get("offers") or []
                if isinstance(offers, dict):
                    offers = [offers]
                for offer in offers:
                    price = _to_float(offer.get("price"))
                    if price is None:
                        continue
                    in_stock = "instock" in str(offer.get("availability", "")).lower()
                    return price, offer.get("priceCurrency"), in_stock
        return None, None, None

    @staticmethod
    def _parse_variant_sizes(flight: str) -> list[RetailSize]:
        """Per-size rows from the ConfigurableProduct variants block:
        EU label from attributes, US size from the simple-product SKU suffix,
        stock from stock_status."""
        sizes: list[RetailSize] = []
        decoder = json.JSONDecoder()
        for m in re.finditer(r'"variants":\[', flight):
            start = m.end() - 1
            try:
                variants, _ = decoder.raw_decode(flight, start)
            except json.JSONDecodeError:
                continue
            if not (isinstance(variants, list) and variants
                    and isinstance(variants[0], dict)
                    and "attributes" in variants[0]):
                continue
            for v in variants:
                label = None
                for attr in v.get("attributes") or []:
                    if attr.get("code") == "size":
                        label = str(attr.get("label") or "").strip()
                simple = v.get("product") or {}
                simple_sku = (simple.get("sku") or "").strip()
                us = simple_sku.rsplit("_", 1)[-1] if "_" in simple_sku else ""
                us_size = us if _US_SUFFIX_RE.match(us) else None
                if not label:
                    continue
                sizes.append(RetailSize(
                    label=label,
                    system="EU" if _EU_LABEL_RE.match(label) else "letter",
                    us_size=us_size,
                    in_stock=simple.get("stock_status") == "IN_STOCK",
                ))
            if sizes:
                break
        return sizes


def _to_float(value) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _first_str(value) -> str:
    """Grid fields vary between scalar and list-of-variant-values; take the
    first non-empty string either way."""
    if isinstance(value, list):
        value = next((v for v in value if v), "")
    return str(value).strip() if value is not None else ""
