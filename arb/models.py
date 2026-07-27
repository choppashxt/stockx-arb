"""Core data models. Everything downstream of the scrapers speaks these types."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RetailSize(BaseModel):
    """One size row on a retailer product page."""

    label: str                      # exactly as the retailer shows it, e.g. "42.5"
    system: str = "EU"              # EU / US / UK / letter (apparel)
    us_size: Optional[str] = None   # US size when derivable WITHOUT guessing
                                    # (e.g. Ballzy encodes it in the variant SKU)
    ean: Optional[str] = None       # GTIN/EAN when the retailer exposes it —
                                    # enables exact StockX variant match by barcode
    in_stock: bool = True


class Product(BaseModel):
    """Normalized retail product record produced by a RetailerScraper."""

    retailer: str
    name: str
    brand: Optional[str] = None
    style_code: Optional[str] = None    # normalized manufacturer code, e.g. HQ2010-005
    colorway: Optional[str] = None
    url: str
    price: float                        # current buy-now price
    currency: str = "EUR"
    sizes: list[RetailSize] = Field(default_factory=list)
    in_stock: bool = True
    pickup_available: Optional[bool] = None
    price_verified: bool = False        # True once the product page (not just the
                                        # category grid) confirmed price/stock
    size_stock_unverified: bool = False # retailer exposes size labels but not
                                        # per-size stock (e.g. Sportland): alert
                                        # at product level, clearly labeled
    stock_note: Optional[str] = None    # freeform availability note for alerts
    buy_note: Optional[str] = None      # e.g. "via LT reshipper" — shown on alerts
    extra_cost_eur: float = 0.0         # reshipping/forwarding cost per unit
    scraped_at: datetime = Field(default_factory=utcnow)


class StockXProduct(BaseModel):
    product_id: str
    style_id: Optional[str] = None
    title: Optional[str] = None
    brand: Optional[str] = None
    url_key: Optional[str] = None
    gender: Optional[str] = None
    retail_price: Optional[float] = None
    product_type: Optional[str] = None

    @property
    def url(self) -> str:
        return f"https://stockx.com/{self.url_key}" if self.url_key else \
            f"https://stockx.com/search?s={self.style_id or self.product_id}"


class StockXVariant(BaseModel):
    product_id: str
    variant_id: str
    size: Optional[str] = None          # variantValue, usually the US size
    size_system: str = "US"
    conversions: dict[str, str] = Field(default_factory=dict)  # e.g. {"eu": "42.5"}


class MarketData(BaseModel):
    """Market snapshot for one variant. Every field the official API can't
    provide stays None — consumers must treat None as 'unknown', never 0."""

    variant_id: str
    currency: str = "EUR"
    lowest_ask: Optional[float] = None
    highest_bid: Optional[float] = None
    num_asks: Optional[int] = None
    num_bids: Optional[int] = None
    sales_72h: Optional[int] = None
    sales_30d: Optional[int] = None
    last_sale: Optional[float] = None
    fetched_at: datetime = Field(default_factory=utcnow)


class ProfitBreakdown(BaseModel):
    scenario: Literal["sell_now", "list_ask"]
    sale_price: float
    transaction_fee: float
    processing_fee: float
    shipping: float
    vat_wedge: float
    payout: float
    profit: float
    margin_pct: float                   # profit / retail_price
    roic: float                         # profit / total capital tied up per cycle


class Opportunity(BaseModel):
    retail: Product
    size_label: str                     # retailer size label this signal is for
    us_size: str
    stockx: StockXProduct
    variant: StockXVariant
    market: MarketData
    sell_now: Optional[ProfitBreakdown] = None   # None when there is no live bid
    list_ask: Optional[ProfitBreakdown] = None   # None when there is no live ask
    est_days_to_clear: Optional[float] = None
    match_confidence: float
    score: float = 0.0
    key_override: Optional[str] = None  # e.g. product-level dedup for
                                        # size-stock-unverified retailers

    @property
    def key(self) -> str:
        """Dedup identity: same shoe, same size, same shop."""
        if self.key_override:
            return self.key_override
        return f"{self.retail.retailer}|{self.variant.variant_id}|{self.size_label}"

    @property
    def landed_cost(self) -> float:
        """What the shoe actually costs you delivered, before StockX fees."""
        return self.retail.price + self.retail.extra_cost_eur

    @property
    def best_profit(self) -> float:
        candidates = [b.profit for b in (self.sell_now, self.list_ask) if b]
        return max(candidates) if candidates else 0.0
