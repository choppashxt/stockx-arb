"""Scan pipeline: scrape -> resolve -> market data -> profit -> filter/rank ->
enrich-confirm -> dedup -> alert -> persist.

The bot only finds and notifies. It never buys, lists, or transacts.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from .config import AppConfig
from .db import Database
from .matching import size_match_confidence, style_code_candidates_from_sku
from .models import MarketData, Opportunity, Product
from .notify import Notifier, format_opportunity
from .profit import est_days_to_clear, opportunity_score, scenarios
from .retailers import create_scraper
from .retailers.base import RetailerScraper
from .stockx.catalog import CatalogResolver
from .stockx.client import AccountNotReady, BudgetExhausted, StockXAPIError
from .stockx.market import MarketDataProvider

log = logging.getLogger(__name__)


class ScanStats:
    def __init__(self) -> None:
        self.products_seen = 0
        self.candidates = 0
        self.opportunities = 0
        self.alerts_sent = 0


async def run_scan(retailer_name: str, cfg: AppConfig, db: Database,
                   resolver: CatalogResolver, provider: MarketDataProvider,
                   notifier: Notifier, limit: Optional[int] = None) -> ScanStats:
    started_at = datetime.now(timezone.utc).isoformat()
    stats = ScanStats()
    # so the dashboard can tell "still scanning" from "hasn't run in ages"
    db.kv_set(f"scan_started:{retailer_name}", started_at)
    scraper = create_scraper(retailer_name, cfg.retailers[retailer_name], db)
    resolver.resolutions_this_scan = 0
    market_calls = 0

    try:
        products = await scraper.scan()
        stats.products_seen = len(products)
        log.info("%s: %d products scraped", retailer_name, len(products))

        for product in products:
            db.upsert_retail_product(product)

        candidates = [p for p in products if _cheap_screen(p, cfg)]
        if limit:
            candidates = candidates[:limit]
        stats.candidates = len(candidates)
        log.info("%s: %d candidates after cheap screen", retailer_name,
                 len(candidates))

        for product in candidates:
            try:
                opportunities, used_market_call = await _evaluate_product(
                    product, cfg, db, resolver, provider, scraper,
                    allow_market_call=market_calls < cfg.stockx.market_calls_per_scan)
            except BudgetExhausted as e:
                log.warning("stopping scan early: %s", e)
                break
            except AccountNotReady as e:
                log.error("StockX refuses market data until the account can "
                          "sell: %s — add billing + shipping details on "
                          "stockx.com, then re-run. Stopping this scan.",
                          e.message)
                break
            except StockXAPIError as e:
                # one product's API failure must never abort the whole retailer
                log.warning("skipping %s — StockX said %s", product.url, e.message)
                continue
            if used_market_call:
                market_calls += 1
            for opp in opportunities:
                stats.opportunities += 1
                await _maybe_alert(opp, scraper, cfg, db, notifier, stats)

    finally:
        await scraper.close()
        db.log_scan(retailer_name, started_at, stats.products_seen,
                    stats.candidates, stats.opportunities, stats.alerts_sent)
        db.kv_set(f"scan_finished:{retailer_name}",
                  datetime.now(timezone.utc).isoformat())
    log.info("%s scan done: %d products, %d candidates, %d opportunities, "
             "%d alerts", retailer_name, stats.products_seen, stats.candidates,
             stats.opportunities, stats.alerts_sent)
    return stats


def _cheap_screen(p: Product, cfg: AppConfig) -> bool:
    return (p.in_stock and p.style_code is not None
            and 0 < p.price <= cfg.filters.max_retail_price_eur
            and p.currency == "EUR")


async def _evaluate_product(product: Product, cfg: AppConfig, db: Database,
                            resolver: CatalogResolver,
                            provider: MarketDataProvider,
                            scraper: Optional[RetailerScraper],
                            allow_market_call: bool
                            ) -> tuple[list[Opportunity], bool]:
    """All profitable size-level opportunities for one retail product."""
    candidates = style_code_candidates_from_sku(product.style_code or "")
    if product.style_code and product.style_code not in candidates:
        candidates.insert(0, product.style_code)
    sx_product, confidence = await resolver.resolve(
        candidates or [product.style_code or ""],
        brand=product.brand, name=product.name)
    if sx_product is None:
        return [], False
    if confidence < cfg.filters.alert_min_confidence:
        db.add_review(product.retailer, product.url, product.style_code,
                      "low_match_confidence",
                      {"confidence": confidence, "stockx": sx_product.title})
        return [], False

    variants = await resolver.variants(sx_product.product_id)
    if not variants:
        return [], False

    # market data: cached snapshots first, one API call per product otherwise
    fresh = {
        v.variant_id: md for v in variants
        if (md := db.get_market_snapshot(
            v.variant_id, cfg.stockx.market_refresh_minutes)) is not None
    }
    used_call = False
    if len(fresh) < len(variants):
        if not allow_market_call:
            return [], False
        fresh = await provider.get_market_data(sx_product.product_id)
        used_call = True

    # any size worth a closer look at all? (product-level, before per-size work)
    if not _any_upside(product.price, fresh.values(), cfg):
        return [], used_call

    # product-page confirmation BEFORE size matching: the grid only has EU
    # labels; the page yields per-size stock + the retailer's own US sizes
    if not product.price_verified and scraper is not None:
        confirmed = await scraper.enrich(product)
        if confirmed is None:
            log.info("could not confirm %s — skipping", product.url)
            return [], used_call
        product = confirmed
        db.upsert_retail_product(confirmed)
        if not product.in_stock or product.price > cfg.filters.max_retail_price_eur:
            return [], used_call
        if not _any_upside(product.price, fresh.values(), cfg):
            return [], used_call

    sizes = product.sizes
    if product.size_stock_unverified and not sizes:
        # retailer exposes no size data at all (e.g. Footshop): consider every
        # StockX variant of the matched product; the alert is product-level
        # and clearly labeled, so this synthesizes candidates, not stock claims
        from .models import RetailSize
        sizes = [RetailSize(label=v.conversions.get("eu") or (v.size or "?"),
                            system="EU" if "eu" in v.conversions else "US",
                            us_size=v.size, in_stock=True)
                 for v in variants if v.size]

    opportunities = []
    for size in sizes:
        if not size.in_stock:
            continue
        variant, size_conf = _match_variant(size.us_size, size.label, variants)
        if variant is None or size_conf < 1.0:
            if variant is not None and 0 < size_conf < 1.0:
                db.add_review(product.retailer, product.url, product.style_code,
                              "size_mapping_conflict",
                              {"retail_size": size.label, "us": size.us_size,
                               "variant": variant.variant_id})
            continue
        market = fresh.get(variant.variant_id)
        if market is None:
            continue
        opp = _build_opportunity(product, size.label, size.us_size or "?",
                                 sx_product, variant, market, confidence, cfg)
        if opp is not None:
            opportunities.append(opp)

    if product.size_stock_unverified and opportunities:
        # per-size stock is unknowable at this retailer: emit ONE clearly
        # labeled product-level opportunity (the best size) instead of a
        # spray of per-size alerts we can't stand behind
        best = max(opportunities, key=lambda o: o.score)
        best.key_override = (f"{product.retailer}|{best.stockx.product_id}"
                             f"|unverified")
        opportunities = [best]
    return opportunities, used_call


def _gate_profit(sell_now, list_ask, cfg: AppConfig) -> float:
    """The profit figure the min_profit filter judges. With require_live_bid
    (default) that is the sell-now payout against the highest bid — no live
    bid means no opportunity, however juicy the ask spread looks."""
    if cfg.filters.require_live_bid:
        return sell_now.profit if sell_now else float("-inf")
    profits = [b.profit for b in (sell_now, list_ask) if b]
    return max(profits) if profits else float("-inf")


def _any_upside(retail_price: float, markets, cfg: AppConfig) -> bool:
    """Could ANY variant clear min_profit at this retail price? Cheap gate so we
    only spend product-page requests (and per-size work) on plausible items."""
    for market in markets:
        sell_now, list_ask = scenarios(retail_price, market, cfg.profit, cfg.vat)
        if _gate_profit(sell_now, list_ask, cfg) >= cfg.filters.min_profit_eur:
            return True
    return False


def _match_variant(us_size: Optional[str], eu_label: Optional[str], variants):
    """Best (variant, confidence) for a retail size row. Never guesses:
    an EU-only match that fits MORE than one variant of the product is
    ambiguous and gets downgraded to review-queue confidence."""
    full_matches = []
    best, best_conf = None, 0.0
    for v in variants:
        conf = size_match_confidence(
            us_size, eu_label, v.size,
            v.conversions.get("eu") or v.conversions.get("eu w")
            or v.conversions.get("eu m"))
        if conf >= 1.0:
            full_matches.append(v)
        if conf > best_conf:
            best, best_conf = v, conf
    if len(full_matches) > 1:
        return full_matches[0], 0.5
    return best, best_conf


def _build_opportunity(product: Product, size_label: str, us_size: str,
                       sx_product, variant, market: MarketData,
                       confidence: float, cfg: AppConfig) -> Optional[Opportunity]:
    sell_now, list_ask = scenarios(product.price, market, cfg.profit, cfg.vat)
    if sell_now is None and list_ask is None:
        return None        # null market data: logged by provider, skip cleanly

    if _gate_profit(sell_now, list_ask, cfg) < cfg.filters.min_profit_eur:
        return None

    if not _liquidity_ok(market, cfg):
        return None

    opp = Opportunity(
        retail=product, size_label=size_label, us_size=us_size,
        stockx=sx_product, variant=variant, market=market,
        sell_now=sell_now, list_ask=list_ask,
        est_days_to_clear=est_days_to_clear(market),
        match_confidence=confidence,
    )
    opp.score = opportunity_score(
        opp.best_profit, sell_now is not None and sell_now.profit > 0, market)
    return opp


def _liquidity_ok(market: MarketData, cfg: AppConfig) -> bool:
    """Apply liquidity floors where the provider supplies the numbers.
    Unknown (None) fields only fail the check under strict_liquidity."""
    f = cfg.filters
    checks = [(market.num_bids, f.min_bids), (market.sales_72h, f.min_sales_72h)]
    for value, floor in checks:
        if value is None:
            if f.strict_liquidity and floor > 0:
                return False
        elif value < floor:
            return False
    return True


async def _maybe_alert(opp: Opportunity, scraper: RetailerScraper, cfg: AppConfig,
                       db: Database, notifier: Notifier, stats: ScanStats) -> None:
    gate_profit = _gate_profit(opp.sell_now, opp.list_ask, cfg)
    should, reason = db.should_alert(opp.key, gate_profit, opp.retail.in_stock,
                                     cfg.alerts.re_alert_profit_delta_eur)
    if not should:
        return
    if stats.alerts_sent >= cfg.alerts.max_per_scan:
        log.info("alert cap reached; suppressing %s (would be '%s')",
                 opp.key, reason)
        return

    # confirm against the product page before telling the human to run for it
    if not opp.retail.price_verified:
        confirmed = await scraper.enrich(opp.retail)
        if confirmed is None:
            log.info("could not confirm %s on product page — not alerting",
                     opp.retail.url)
            return
        size_row = next((s for s in confirmed.sizes
                         if s.label == opp.size_label), None)
        if not confirmed.in_stock or size_row is None or not size_row.in_stock:
            log.info("size %s of %s gone on product page — not alerting",
                     opp.size_label, opp.retail.url)
            db.mark_out_of_stock(opp.key)
            return
        if confirmed.price > opp.retail.price + 0.01:
            rebuilt = _build_opportunity(
                confirmed, opp.size_label, opp.us_size, opp.stockx, opp.variant,
                opp.market, opp.match_confidence, cfg)
            if rebuilt is None:
                log.info("%s no longer profitable at confirmed price €%.2f",
                         opp.key, confirmed.price)
                return
            opp = rebuilt
        else:
            opp.retail = confirmed
        db.upsert_retail_product(confirmed)

    await notifier.send(format_opportunity(opp, reason))
    db.record_alert(opp.key, _gate_profit(opp.sell_now, opp.list_ask, cfg),
                    opp.retail.in_stock, opp.model_dump_json())
    stats.alerts_sent += 1


async def run_loop(cfg: AppConfig, db: Database, resolver: CatalogResolver,
                   provider: MarketDataProvider, notifier: Notifier,
                   retailer_filter: Optional[str] = None) -> None:
    """Simple asyncio loop: each enabled retailer scans on its own interval."""

    async def loop_one(name: str) -> None:
        interval = cfg.retailers[name].scan_interval_minutes * 60
        while True:
            try:
                await run_scan(name, cfg, db, resolver, provider, notifier)
            except Exception:
                log.exception("scan of %s failed; continuing after interval", name)
            await asyncio.sleep(interval)

    names = [n for n, rc in cfg.retailers.items()
             if rc.enabled and (retailer_filter is None or n == retailer_filter)]
    if not names:
        raise SystemExit("no enabled retailers matched")
    await asyncio.gather(*(loop_one(n) for n in names))
