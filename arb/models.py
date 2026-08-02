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
    in_stock: Optional[bool] = True # True/False = retailer-verified; None =
                                    # UNKNOWN (retailer hides per-size stock,
                                    # e.g. Sportland). Never fabricate True.


class Product(BaseModel):
    """Normalized retail product record produced by a RetailerScraper."""

    retailer: str
    name: str
    brand: Optional[str] = None
    style_code: Optional[str] = None    # normalized manufacturer code, e.g. HQ2010-005
    # The retailer's OWN product id, when it differs from the manufacturer code
    # and is needed to look the product up again (weekend.ee prefixes its SKUs:
    # 'NIDM0113-100' for style code 'DM0113-100'). Used by enrich().
    retailer_sku: Optional[str] = None
    # What kind of thing this is: "sneakers", "apparel", "collectibles",
    # "electronics". Set by the scraper, shown on sizeless alerts so the human
    # knows what condition StockX will authenticate against (sealed box vs shoe).
    category: Optional[str] = None
    # Product-level barcode, for SIZELESS items where there is no RetailSize row
    # to carry one. Used to cross-check the code match: a GTIN that names a
    # different StockX product means the match is wrong, and a wrong barcode
    # resolves to a plausible OTHER real product rather than to nothing.
    ean: Optional[str] = None
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
    discount_pct: float = 0.0           # your standing discount at this shop
    sale_discount_pct: float = 0.0      # extra % off, SALE ITEMS ONLY
    on_sale: bool = False               # is this item actually marked down?
    list_price: Optional[float] = None  # pre-markdown price, when known

    @property
    def landed_cost(self) -> float:
        """What this actually costs you delivered.

        Listed price, less any standing discount you get at this shop, less any
        extra checkout discount that applies ONLY to marked-down items, plus
        any forwarding cost. Single source of truth — every profit calculation
        goes through here, so a promo can never be applied to full-price stock.
        """
        price = self.price * (1 - self.discount_pct)
        if self.on_sale and self.sale_discount_pct:
            price *= (1 - self.sale_discount_pct)
        return price + self.extra_cost_eur
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
    # Both None for a SIZELESS product — Lego, cards, electronics and watches
    # return exactly one StockX variant with no size value at all. There is no
    # size to match, so claiming one would be inventing information.
    size_label: Optional[str] = None     # retailer size label this signal is for
    us_size: Optional[str] = None
    stockx: StockXProduct
    variant: StockXVariant
    market: MarketData
    sell_now: Optional[ProfitBreakdown] = None   # None when there is no live bid
    list_ask: Optional[ProfitBreakdown] = None   # None when there is no live ask
    est_days_to_clear: Optional[float] = None
    match_confidence: float
    size_match_method: str = "size chart"   # "barcode" when a GTIN pinned it
    prefer_note: Optional[str] = None       # "buy from the sibling store instead"
    score: float = 0.0
    key_override: Optional[str] = None  # e.g. product-level dedup for
                                        # size-stock-unverified retailers

    @property
    def key(self) -> str:
        """Dedup identity: same item, same size, same shop. A sizeless product
        keys on the variant alone — it has exactly one, so that is already
        unique, and interpolating None would make the key read "...|None"."""
        if self.key_override:
            return self.key_override
        base = f"{self.retail.retailer}|{self.variant.variant_id}"
        return f"{base}|{self.size_label}" if self.size_label else base

    @property
    def sizeless(self) -> bool:
        return self.size_label is None

    @property
    def landed_cost(self) -> float:
        """What the shoe actually costs you delivered, before StockX fees."""
        return self.retail.landed_cost

    @property
    def best_profit(self) -> float:
        candidates = [b.profit for b in (self.sell_now, self.list_ask) if b]
        return max(candidates) if candidates else 0.0
