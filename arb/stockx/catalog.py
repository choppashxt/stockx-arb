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
from ..matching import code_in_title, style_ids_match
from ..models import StockXProduct, StockXVariant
from .client import StockXAPIError, StockXClient

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


def _brands_agree(retail_brand: Optional[str],
                  stockx_brand: Optional[str]) -> bool:
    """Do the two brand strings name the same maker?

    Compared on the first token, case-insensitively, so 'LEGO' agrees with
    'LEGO Star Wars' but not with 'Pokemon'. Returns False when either side is
    missing: this gates the title-code path, where the absence of corroboration
    is a reason to decline, not to proceed.
    """
    if not retail_brand or not stockx_brand:
        return False
    a = retail_brand.strip().lower().split()
    b = stockx_brand.strip().lower().split()
    return bool(a) and bool(b) and a[0] == b[0]


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
                 negative_cache_days: int, max_resolutions_per_scan: int = 10**9,
                 max_gtin_lookups_per_scan: int = 10**9):
        self.client = client        # None => offline: cache/fixtures only
        self.db = db
        self.negative_cache_days = negative_cache_days
        self.max_resolutions_per_scan = max_resolutions_per_scan
        self.max_gtin_lookups_per_scan = max_gtin_lookups_per_scan
        self.resolutions_this_scan = 0
        self.gtin_lookups_this_scan = 0

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
            hits = data.get("products") or []
            for hit in hits:
                if style_ids_match(code, hit.get("styleId")):
                    product = _product_from_search_hit(hit)
                    confidence = 1.0 if rank == 0 else 0.95
                    log.info("resolved %s -> %s (%s) conf=%.2f",
                             code, product.product_id, product.title, confidence)
                    return product, confidence
            # Non-sneaker categories carry an EMPTY styleId, so the loop above
            # can never match them. Their identifying code sits in the title
            # ('... Set 75192', '... GA-110PKM-7A') instead.
            #
            # Only consulted when the hit has NO styleId at all: a product that
            # HAS a code which failed to match is evidence AGAINST the match,
            # and the title must not be used to talk us past it.
            # A title code is also weaker evidence than a styleId, so it must be
            # corroborated by the BRAND. Without that check, a LEGO advent
            # calendar whose name contained '2025' matched the StockX title
            # '2025 Pokemon Mega Evolution Charizard X ex Ultra-Premium
            # Collection' and got priced against a card-box bid (caught on the
            # first live Apollo run, 2026-07-29). No retail brand means no
            # corroboration available, so no title match.
            for hit in hits:
                if (hit.get("styleId") or "").strip():
                    continue
                if not _brands_agree(brand, hit.get("brand")):
                    continue
                if code_in_title(code, hit.get("title")):
                    product = _product_from_search_hit(hit)
                    confidence = 1.0 if rank == 0 else 0.95
                    log.info("resolved %s -> %s (%s) via title code conf=%.2f",
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

    async def resolve_gtin(self, gtin: str) -> Optional[StockXVariant]:
        """Barcode -> exact StockX variant. The strongest match we can make:
        a GTIN names one product in one size, so there is nothing to infer.
        Cached permanently (barcodes never change) including misses."""
        gtin = re.sub(r"\D", "", gtin or "")
        if len(gtin) < 8:
            return None
        cached = self.db.get_gtin(gtin)
        if cached is not None:
            return cached[0]
        if self.client is None or self.gtin_lookups_this_scan >= self.max_gtin_lookups_per_scan:
            return None

        self.gtin_lookups_this_scan += 1
        try:
            data = await self.client.get_variant_by_gtin(gtin)
        except StockXAPIError as e:
            if e.status in (400, 404):
                # malformed or unknown barcode — a miss, not a failure. Cache it
                # so one bad EAN never costs a lookup (or a product) twice.
                self.db.put_gtin(gtin, None)
                return None
            raise
        variant = _variant_from_api(data) if data else None
        self.db.put_gtin(gtin, variant)
        if variant:
            log.info("gtin %s -> %s size %s", gtin, variant.product_id[:8],
                     variant.size)
        return variant

    async def variants(self, product_id: str) -> list[StockXVariant]:
        cached = self.db.get_variants(product_id)
        if cached or self.client is None:
            return cached
        raw = await self.client.get_variants(product_id)
        variants = [_variant_from_api(v) for v in raw]
        if variants:
            self.db.put_variants(product_id, variants)
        return variants
