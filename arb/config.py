"""Settings: secrets from .env (pydantic-settings), tunables from config.yaml."""
from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


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
    transaction_fee_pct: float = 0.095
    processing_fee_pct: float = 0.03
    shipping_to_stockx_eur: float = 7.00
    undercut_eur: float = 0.00


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
    negative_cache_days: int = 3


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
    buy_note: str = ""
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


class AppConfig(BaseModel):
    db_path: str = "state.db"
    profit: ProfitConfig = ProfitConfig()
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
