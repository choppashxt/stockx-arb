"""Settings: secrets from .env (pydantic-settings), tunables from config.yaml."""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from typing import Optional

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Retail promos run on the storefront's own calendar day, not on UTC's. Every
# retailer carrying one today sits in EET/EEST (Ballzy serves the Baltics,
# Sportland is .ee/.lt), so a single zone covers them. Judging "today" in UTC
# would keep a promo alive for the three hours after Tallinn midnight — the
# over-application this gate exists to prevent.
PROMO_TZ = ZoneInfo("Europe/Tallinn")


def promo_live(expires: Optional[date], *, today: Optional[date] = None) -> bool:
    """Is a discount carrying this expiry still valid?

    No expiry means standing pricing (a loyalty rate), which never lapses. An
    expiry is the LAST day the offer runs, inclusive.
    """
    if expires is None:
        return True
    return (today or datetime.now(PROMO_TZ).date()) <= expires


class Secrets(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    stockx_api_key: str = ""
    stockx_client_id: str = ""
    stockx_client_secret: str = ""
    stockx_refresh_token: str = ""
    discord_webhook_url: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    @property
    def stockx_configured(self) -> bool:
        values = (self.stockx_api_key, self.stockx_client_id,
                  self.stockx_client_secret, self.stockx_refresh_token)
        return all(v and not v.startswith("PASTE_") for v in values)

    @property
    def telegram_configured(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    @property
    def discord_configured(self) -> bool:
        return self.discord_webhook_url.startswith("https://discord.com/api/webhooks/")


class ProfitConfig(BaseModel):
    transaction_fee_pct: float = 0.09
    processing_fee_pct: float = 0.03
    min_transaction_fee_eur: float = 5.00
    shipping_to_stockx_eur: float = 7.00
    undercut_eur: float = 0.00


class AliasConfig(BaseModel):
    """Alias (GOAT's seller app) as a second sale venue.

    Alias has no public API and its pages sit behind bot protection, so we
    cannot read its prices automatically without evasion — which this project
    does not do. Instead we model its fees, so every alert can state the price
    Alias would have to pay to beat the StockX payout, plus a link to check it
    by hand in seconds. Fees verified 2026-07-27 (alias.org/fees).
    """

    enabled: bool = True
    commission_pct: float = 0.095   # seller rating >= 90 (everyone starts at 90)
    cashout_pct: float = 0.029      # ACH / PayPal withdrawal
    seller_fee_eur: float = 5.0     # region-dependent flat fee — CHECK YOURS
    shipping_eur: float = 7.0       # your cost to get it to their hub


class VatConfig(BaseModel):
    enabled: bool = False
    rate: float = 0.24
    input_vat_reclaimable: bool = False
    output_vat_on_sale: bool = False


class FilterConfig(BaseModel):
    min_profit_eur: float = 25.0
    require_live_bid: bool = True   # gate profit on the sell-now (highest bid)
                                    # payout; ask-only spreads never alert
    max_retail_price_eur: float = 500.0
    alert_min_confidence: float = 0.90
    min_bids: int = 1
    min_sales_72h: int = 0
    strict_liquidity: bool = False


class AlertConfig(BaseModel):
    max_per_scan: int = 10
    re_alert_profit_delta_eur: float = 10.0


class StockXConfig(BaseModel):
    currency: str = "EUR"
    daily_request_budget: int = 20000
    min_request_interval_s: float = 1.05
    market_refresh_minutes: int = 60
    market_calls_per_scan: int = 150
    max_new_resolutions_per_scan: int = 300
    gtin_lookups_per_scan: int = 400   # barcode -> exact variant lookups
    # Tiered bid re-checking. One market-data call covers a whole product, so
    # the budget decides how often each SKU can be re-examined. Products that
    # nearly cleared the profit floor are watched closely; hopeless ones are
    # revisited rarely, which is what makes watching everything affordable.
    refresh_minutes_hot: int = 45      # already profitable, or within a whisker
    refresh_minutes_warm: int = 240    # within near_miss_eur of the floor
    refresh_minutes_cold: int = 1440   # nowhere near — once a day is plenty
    # Assessed, and NO variant had a live bid. Under require_live_bid these
    # cannot alert until a bid appears, so they are the least valuable call per
    # unit of budget — but not worthless (the one completed trade went from no
    # bid to EUR 176 inside a day). None = same cadence as cold.
    refresh_minutes_nobid: Optional[int] = None
    near_miss_eur: float = 30.0
    negative_cache_days: int = 3
    # Watch-refresh loop: re-prices hot/warm SKUs on their tier TTL regardless
    # of when their retailer next rescans. Without it a tier TTL shorter than
    # the retailer's scan_interval_minutes never actually applies.
    watch_refresh_interval_minutes: int = 10
    watch_batch: int = 200             # SKUs per round, hot first
    # The loop yields to retailer scans (which are what DISCOVER new stock):
    # it pauses once the rolling 24h usage passes this fraction of the daily
    # budget, so retailers keep the remainder and the hard stop stays theirs.
    watch_budget_ceiling_pct: float = 0.85


class NotificationsConfig(BaseModel):
    method: str = "discord"        # discord | telegram | console


class RetailerConfig(BaseModel):
    enabled: bool = True
    scan_interval_minutes: int = 30
    request_delay_seconds: float = 2.5
    # Landed-cost realism: anything that isn't a plain "order it, it arrives"
    # purchase costs extra money and time. extra_cost_eur is added to the buy
    # price before profit is computed, and buy_note is shown on every alert.
    extra_cost_eur: float = 0.0
    # Discount you get on EVERY product at this retailer. 0.10 = 10% off the
    # listed price, applied before profit is judged, so real edges are not
    # missed. Either standing pricing (loyalty/club/registered-customer) or a
    # storewide campaign — pair a campaign with discount_expires so it lapses
    # on its own.
    discount_pct: float = 0.0
    # Extra checkout discount that applies ONLY to already-marked-down items
    # (a running promo rather than standing pricing). Requires the scraper to
    # report on_sale; if it doesn't, this is simply never applied. Deliberately
    # separate from discount_pct so a sale-only offer never silently inflates
    # full-price stock.
    sale_discount_pct: float = 0.0
    # Last day (inclusive, Tallinn time) the two discounts above are real.
    # Leave unset for standing pricing that never lapses; set it for any
    # temporary campaign. Past it both revert to 0.0 without anyone having to
    # remember: a stale promo prices stock below what it costs, which turned a
    # EUR 9 loss into a "+EUR 7 profit" alert the last time one was left behind.
    discount_expires: Optional[date] = None
    buy_note: str = ""
    # A sibling storefront that is always preferable to buy from (same catalog
    # and price, but no reshipping). Alerts from this retailer will point at
    # the sibling when it also has the shoe — without being suppressed, since
    # the sibling often lacks the exact size.
    prefer_retailer: str = ""
    categories: list[str] = Field(default_factory=list)
    max_pages: int = 50
    # sitemap-driven retailers: how many backlog products to (re)visit per scan
    # (fresh/sale listings are always visited on top of this)
    sitemap_slice_per_scan: int = 120
    sitemap_refresh_hours: int = 24
    # slug substrings worth scanning (empty = everything); used by big-catalog
    # retailers to skip swim caps and yoga mats
    slug_filters: list[str] = Field(default_factory=list)
    graphql_batch_size: int = 40
    # --- promo-window throughput boost -------------------------------------
    # A storewide markdown makes a large slice of the catalogue arbitrageable
    # at once, so it is worth scanning this retailer harder — but only while
    # the campaign runs. These apply ONLY when discount_expires is set and
    # still live, which is what stops a one-day campaign from leaving a
    # permanently heavier crawl (and a permanently larger share of the shared
    # StockX budget) behind it. Unset fields keep the normal value.
    promo_scan_interval_minutes: Optional[int] = None
    promo_sitemap_slice_per_scan: Optional[int] = None
    promo_max_pages: Optional[int] = None

    @property
    def effective_discount_pct(self) -> float:
        """discount_pct, or 0.0 once the campaign carrying it has ended."""
        return self.discount_pct if promo_live(self.discount_expires) else 0.0

    @property
    def effective_sale_discount_pct(self) -> float:
        """sale_discount_pct, or 0.0 once the campaign carrying it has ended."""
        return self.sale_discount_pct if promo_live(self.discount_expires) else 0.0

    @property
    def promo_boost_active(self) -> bool:
        """Is this retailer inside a live campaign that asks for more crawl?

        Deliberately gated on discount_expires: a boost with no end date is how
        you end up permanently over-crawling a shop that ran one sale.
        """
        return (self.discount_expires is not None
                and promo_live(self.discount_expires)
                and any(v for v in (self.promo_scan_interval_minutes,
                                    self.promo_sitemap_slice_per_scan,
                                    self.promo_max_pages)))

    @property
    def effective_scan_interval_minutes(self) -> int:
        if self.promo_boost_active and self.promo_scan_interval_minutes:
            return self.promo_scan_interval_minutes
        return self.scan_interval_minutes

    def promo_adjusted(self) -> "RetailerConfig":
        """A copy whose crawl knobs carry the boost, for handing to a scraper.

        Scrapers read cfg.max_pages / cfg.sitemap_slice_per_scan straight off
        the config, so the boost is applied by adjusting the config they are
        built with rather than teaching a dozen call sites about promo windows.
        """
        if not self.promo_boost_active:
            return self
        update = {}
        if self.promo_sitemap_slice_per_scan:
            update["sitemap_slice_per_scan"] = self.promo_sitemap_slice_per_scan
        if self.promo_max_pages:
            update["max_pages"] = self.promo_max_pages
        return self.model_copy(update=update) if update else self

    @property
    def cost_policy_signature(self) -> str:
        """Everything that moves landed cost without moving the listed price.

        Compared scan-to-scan to notice a campaign starting or ending; see
        run_scan, which re-prices the catalogue when this changes.
        """
        return (f"{self.effective_discount_pct}|{self.effective_sale_discount_pct}"
                f"|{self.extra_cost_eur}")


class AppConfig(BaseModel):
    db_path: str = "state.db"
    profit: ProfitConfig = ProfitConfig()
    alias: AliasConfig = AliasConfig()
    vat: VatConfig = VatConfig()
    filters: FilterConfig = FilterConfig()
    alerts: AlertConfig = AlertConfig()
    stockx: StockXConfig = StockXConfig()
    notifications: NotificationsConfig = NotificationsConfig()
    retailers: dict[str, RetailerConfig] = Field(default_factory=dict)

    @property
    def db_file(self) -> Path:
        p = Path(self.db_path)
        return p if p.is_absolute() else PROJECT_ROOT / p


def load_config(path: str | Path | None = None) -> AppConfig:
    cfg_path = Path(path) if path else PROJECT_ROOT / "config.yaml"
    if cfg_path.exists():
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    else:
        raw = {}
    return AppConfig(**raw)
