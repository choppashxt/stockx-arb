"""Net payout / profit math. Config-driven; VAT is isolated in vat_wedge()."""
from __future__ import annotations

from typing import Optional

from .config import ProfitConfig, VatConfig
from .models import MarketData, ProfitBreakdown


def vat_wedge(sale_price: float, retail_price: float, vat: VatConfig) -> float:
    """Net VAT cost of one flip, as an amount subtracted from the payout.
    Negative means a net reclaim. Returns 0.0 while vat.enabled is false.

    This is the ONLY place VAT math lives. Enabling it later (once the KMKR
    registration is confirmed) must not require touching anything else.
    """
    if not vat.enabled:
        return 0.0
    wedge = 0.0
    r = vat.rate
    if vat.output_vat_on_sale:
        # VAT owed to the state out of the gross sale price (price is VAT-inclusive)
        wedge += sale_price * r / (1 + r)
    if vat.input_vat_reclaimable:
        # VAT embedded in the retail purchase price, reclaimable as input VAT
        wedge -= retail_price * r / (1 + r)
    return wedge


def breakdown(scenario: str, sale_price: float, retail_price: float,
              profit_cfg: ProfitConfig, vat_cfg: VatConfig) -> ProfitBreakdown:
    tx = sale_price * profit_cfg.transaction_fee_pct
    proc = sale_price * profit_cfg.processing_fee_pct
    ship = profit_cfg.shipping_to_stockx_eur
    vat = vat_wedge(sale_price, retail_price, vat_cfg)
    payout = sale_price - tx - proc - ship - vat
    profit = payout - retail_price
    capital = retail_price + ship          # cash tied up per cycle
    return ProfitBreakdown(
        scenario=scenario, sale_price=sale_price, transaction_fee=tx,
        processing_fee=proc, shipping=ship, vat_wedge=vat, payout=payout,
        profit=profit,
        margin_pct=profit / retail_price if retail_price else 0.0,
        roic=profit / capital if capital else 0.0,
    )


def scenarios(retail_price: float, market: MarketData, profit_cfg: ProfitConfig,
              vat_cfg: VatConfig) -> tuple[Optional[ProfitBreakdown], Optional[ProfitBreakdown]]:
    """(sell_now, list_ask) — each None when the market side it needs is absent."""
    sell_now = None
    if market.highest_bid is not None and market.highest_bid > 0:
        sell_now = breakdown("sell_now", market.highest_bid, retail_price,
                             profit_cfg, vat_cfg)
    list_ask = None
    if market.lowest_ask is not None and market.lowest_ask > 0:
        ask_price = max(market.lowest_ask - profit_cfg.undercut_eur, 0.0)
        list_ask = breakdown("list_ask", ask_price, retail_price, profit_cfg, vat_cfg)
    return sell_now, list_ask


def est_days_to_clear(market: MarketData) -> Optional[float]:
    """Rough queue/velocity estimate for the list_ask scenario. None if the
    provider gives no sales velocity (the official StockX API does not)."""
    if not market.sales_72h:
        return None
    per_day = market.sales_72h / 3.0
    queue = market.num_asks if market.num_asks is not None else 1
    return round(max(queue, 1) / per_day, 1)


def opportunity_score(best_profit: float, sell_now_viable: bool,
                      market: MarketData) -> float:
    """Rank: profit weighted by liquidity. A live bid you can sell straight into
    doubles the score — the whole point is not losing the sale window."""
    factor = 2.0 if sell_now_viable else 1.0
    if market.sales_72h:
        factor *= 1.0 + min(market.sales_72h, 10) / 10.0
    if market.highest_bid is None:
        factor *= 0.5      # no bid at all: weakest evidence of demand
    return round(best_profit * factor, 2)
