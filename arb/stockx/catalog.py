"""Resolve retailer style codes to StockX products + variants, cached in SQLite.

Resolution result carries a confidence score:
  1.00 exact styleId match on the primary candidate
  0.95 exact styleId match on a secondary (ambiguity-stripped) candidate
  <alert_min_confidence -> review queue only, never an alert
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from ..db import Database
from ..matching import style_ids_match
from ..models import StockXProduct, StockXVariant
from .client import StockXClient

log = logging.getLogger(__name__)

_STOP_TOKENS = {"the", "and", "for", "shoes", "shoe", "sneakers", "retro"}


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", text.lower())
            if len(t) > 1 and t not in _STOP_TOKENS}


def _fuzzy_best(brand: str, name: str, hits: list[dict]) -> Optional[dict]:
    """Top hit whose brand matches and whose title covers most of the retail
    name's tokens. Conservative: anything ambiguous returns None."""
    want = _tokens(name)
    if not want:
        return None
    for hit in hits:
        if (hit.get("brand") or "").lower() != brand.lower().split()[0]:
            continue
        got = _tokens(hit.get("title") or "")
        overlap = len(want & got) / len(want)
        if overlap >= 0.6:
            return hit
    return None


def _product_from_search_hit(hit: dict) -> StockXProduct:
    attrs = hit.get("productAttributes") or {}
    return StockXProduct(
        product_id=hit["productId"],
        style_id=hit.get("styleId"),
        title=hit.get("title"),
        brand=hit.get("brand"),
        url_key=hit.get("urlKey"),
        product_type=hit.get("productType"),
        gender=attrs.get("gender"),
        retail_price=attrs.get("retailPrice"),
    )


def _variant_from_api(v: dict) -> StockXVariant:
    conversions: dict[str, str] = {}
    chart = v.get("sizeChart") or {}
    for conv in chart.get("availableConversions") or []:
        size_type = (conv.get("type") or "").lower()
        size_val = conv.get("size")
        if size_type and size_val:
            # types look like "us m", "us w", "eu", "uk", "cm", "kr"
            conversions[size_type] = str(size_val)
    return StockXVariant(
        product_id=v["productId"],
        variant_id=v["variantId"],
        size=v.get("variantValue"),
        conversions=conversions,
    )


class CatalogResolver:
    def __init__(self, client: Optional[StockXClient], db: Database,
                 negative_cache_days: int, max_resolutions_per_scan: int = 10**9):
        self.client = client        # None => offline: cache/fixtures only
        self.db = db
        self.negative_cache_days = negative_cache_days
        self.max_resolutions_per_scan = max_resolutions_per_scan
        self.resolutions_this_scan = 0

    async def resolve(self, candidates: list[str], brand: Optional[str] = None,
                      name: Optional[str] = None
                      ) -> tuple[Optional[StockXProduct], float]:
        """Map style-code candidates (most specific first) to a StockX product.
        Cached by the primary candidate. Returns (product|None, confidence).
        A fuzzy brand+name fallback can yield confidence 0.5 — below the alert
        floor by design, so it only ever feeds the review queue."""
        if not candidates:
            return None, 0.0
        cache_key = candidates[0]
        cached = self.db.get_resolution(cache_key, self.negative_cache_days)
        if cached is not None:
            return cached
        if self.client is None:
            # offline/fixture mode: only pre-seeded resolutions exist; don't
            # write a negative cache entry the real provider would inherit
            return None, 0.0
        if self.resolutions_this_scan >= self.max_resolutions_per_scan:
            # cap NEW resolutions per scan (spreads the first big crawl across
            # runs); cached ones above already returned, uncached wait their turn
            return None, 0.0

        self.resolutions_this_scan += 1
        product, confidence = await self._resolve_uncached(candidates, brand, name)
        self.db.put_resolution(cache_key, product, confidence)
        return product, confidence

    async def _resolve_uncached(self, candidates: list[str],
                                brand: Optional[str], name: Optional[str]
                                ) -> tuple[Optional[StockXProduct], float]:
        for rank, code in enumerate(candidates):
            data = await self.client.search(code, page_size=10)
            for hit in data.get("products") or []:
                if style_ids_match(code, hit.get("styleId")):
                    product = _product_from_search_hit(hit)
                    confidence = 1.0 if rank == 0 else 0.95
                    log.info("resolved %s -> %s (%s) conf=%.2f",
                             code, product.product_id, product.title, confidence)
                    return product, confidence

        # fuzzy fallback: brand + model text search, scored on token overlap.
        # Cap at 0.5 so it can never clear the alert floor — review queue only.
        if brand and name:
            data = await self.client.search(f"{brand} {name}"[:100], page_size=5)
            hit = _fuzzy_best(brand, name, data.get("products") or [])
            if hit is not None:
                product = _product_from_search_hit(hit)
                log.info("fuzzy-resolved %s %s -> %s (conf=0.5, review only)",
                         brand, name, product.title)
                return product, 0.5
        log.info("no StockX match for %s", candidates)
        return None, 0.0

    async def variants(self, product_id: str) -> list[StockXVariant]:
        cached = self.db.get_variants(product_id)
        if cached or self.client is None:
            return cached
        raw = await self.client.get_variants(product_id)
        variants = [_variant_from_api(v) for v in raw]
        if variants:
            self.db.put_variants(product_id, variants)
        return variants
