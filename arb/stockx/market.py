"""MarketDataProvider interface + implementations.

ALL pricing flows through this interface so the source can be swapped
(official API today; KicksDB / Apify later) without touching the pipeline.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Optional

from ..config import StockXConfig
from ..db import Database
from ..models import MarketData
from .client import StockXClient

log = logging.getLogger(__name__)


class MarketDataProvider(ABC):
    """Returns {variant_id: MarketData} for one product. Missing numbers stay
    None; implementations log and degrade — they never raise on null data."""

    @abstractmethod
    async def get_market_data(self, product_id: str) -> dict[str, MarketData]:
        ...


def _to_float(value) -> Optional[float]:
    """API amounts arrive as strings, sometimes null/empty. None means unknown."""
    if value in (None, "", "null"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class StockXOfficialProvider(MarketDataProvider):
    """Official /catalog/products/{id}/market-data — one call covers all variants.
    Exposes lowest ask + highest bid only; the official API has no bid/ask
    counts, sales velocity, or last-sale fields, so those remain None."""

    def __init__(self, client: StockXClient, db: Database, cfg: StockXConfig):
        self.client = client
        self.db = db
        self.cfg = cfg
        self.calls_this_scan = 0

    async def get_market_data(self, product_id: str) -> dict[str, MarketData]:
        rows = await self.client.get_product_market_data(product_id, self.cfg.currency)
        self.calls_this_scan += 1
        out: dict[str, MarketData] = {}
        nulls = 0
        for row in rows:
            variant_id = row.get("variantId")
            if not variant_id:
                continue
            md = MarketData(
                variant_id=variant_id,
                currency=row.get("currencyCode") or self.cfg.currency,
                lowest_ask=_to_float(row.get("lowestAskAmount")),
                highest_bid=_to_float(row.get("highestBidAmount")),
            )
            if md.lowest_ask is None and md.highest_bid is None:
                nulls += 1
            out[variant_id] = md
            self.db.put_market_data(product_id, md)
        if nulls:
            log.info("market data for %s: %d/%d variants had null ask AND bid",
                     product_id, nulls, len(rows))
        return out


class CachedProvider(MarketDataProvider):
    """Wraps another provider with the SQLite snapshot cache so repeated scans
    inside market_refresh_minutes cost zero API requests."""

    def __init__(self, inner: MarketDataProvider, db: Database, cfg: StockXConfig,
                 variant_ids_by_product: Optional[dict[str, list[str]]] = None):
        self.inner = inner
        self.db = db
        self.cfg = cfg
        self._known_variants = variant_ids_by_product or {}

    def register_variants(self, product_id: str, variant_ids: list[str]) -> None:
        self._known_variants[product_id] = variant_ids

    async def get_market_data(self, product_id: str) -> dict[str, MarketData]:
        variant_ids = self._known_variants.get(product_id, [])
        if variant_ids:
            cached = {
                vid: md for vid in variant_ids
                if (md := self.db.get_market_snapshot(
                    vid, self.cfg.market_refresh_minutes)) is not None
            }
            if len(cached) == len(variant_ids):
                return cached
        return await self.inner.get_market_data(product_id)


class FixtureProvider(MarketDataProvider):
    """Offline provider for --provider fixture: serves canned market data so the
    whole pipeline can run without StockX credentials (see arb/fixtures.py)."""

    def __init__(self, fixtures: dict[str, dict[str, MarketData]]):
        self.fixtures = fixtures        # {product_id: {variant_id: MarketData}}

    async def get_market_data(self, product_id: str) -> dict[str, MarketData]:
        data = self.fixtures.get(product_id, {})
        if not data:
            log.info("fixture provider: no data for %s (simulating null response)",
                     product_id)
        return data
