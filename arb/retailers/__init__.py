"""Retailer registry. Each retailer is one module implementing RetailerScraper;
register it here and give it a block in config.yaml under `retailers:`."""
from __future__ import annotations

from ..config import RetailerConfig
from .base import RetailerScraper
from .ballzy import BallzyScraper
from .rademar import RademarScraper
from .reede import ReedeScraper
from .footshop import FootshopScraper
from .overkill import OverkillScraper
from .sns import SnsScraper
from .sportland import SportlandLTScraper, SportlandLVScraper, SportlandScraper
from .teamsport import TeamsportScraper

_SCRAPERS: dict[str, type[RetailerScraper]] = {
    # Tier 1 — Estonia
    "ballzy": BallzyScraper,            # also serves LV/LT/FI/SE storefronts
    "reede": ReedeScraper,
    "teamsport": TeamsportScraper,
    "sportland": SportlandScraper,
    "rademar": RademarScraper,
    # sportsdirect.ee: drops connections to non-browser clients (Frasers bot
    # wall, verified 2026-07-27). Skipped per the no-anti-detection guardrail.
    # Tier 2 — Baltics (same Sportland platform, different domain/sitemap;
    # rademar.lv/.lt and sneakerspoint.fi don't resolve — verified 2026-07-27)
    "sportland_lv": SportlandLVScraper,
    "sportland_lt": SportlandLTScraper,
    # Tier 3 — EU (least frequent; big robots crawl-delays honored)
    "sns": SnsScraper,                  # Shopify, per-size JSON-LD + GTINs
    "overkill": OverkillScraper,        # Shopify; ships EE EUR13 via DHL
    "footshop": FootshopScraper,        # microdata; size stock unverified
    # stadium.fi: homepage answers but the INTERSHOP catalog paths 403 plain
    # clients (verified 2026-07-27) — skipped per the no-anti-detection rule.
}


def create_scraper(name: str, cfg: RetailerConfig, db=None) -> RetailerScraper:
    try:
        cls = _SCRAPERS[name]
    except KeyError:
        raise ValueError(f"unknown retailer '{name}' — known: {sorted(_SCRAPERS)}")
    return cls(cfg, db)


def known_retailers() -> list[str]:
    return sorted(_SCRAPERS)
