"""Profit math regression tests.

Deliberately built on EXPLICIT config objects with round numbers — not on
config.yaml — so editing real-world fees never breaks a test spuriously
(the old selftest hardcoded the yaml's fee literals, audit 10.3).
Covers audit 0.2: opportunity_score must scale the GATED profit.
"""
import pytest

from arb.config import ProfitConfig, VatConfig
from arb.models import MarketData
from arb.profit import (
    breakdown,
    est_days_to_clear,
    opportunity_score,
    scenarios,
    vat_wedge,
)

PROFIT = ProfitConfig(transaction_fee_pct=0.10, processing_fee_pct=0.03,
                      min_transaction_fee_eur=0.0,
                      shipping_to_stockx_eur=7.0, undercut_eur=0.0)
NO_VAT = VatConfig(enabled=False)


def md(bid=None, ask=None, sales_72h=None, num_asks=None) -> MarketData:
    return MarketData(variant_id="v1", highest_bid=bid, lowest_ask=ask,
                      sales_72h=sales_72h, num_asks=num_asks)


class TestBreakdown:
    def test_hand_computed(self):
        # sale 200: tx 20, proc 6, ship 7 -> payout 167; retail 100 -> profit 67
        b = breakdown("sell_now", 200.0, 100.0, PROFIT, NO_VAT)
        assert b.transaction_fee == pytest.approx(20.0)
        assert b.processing_fee == pytest.approx(6.0)
        assert b.shipping == pytest.approx(7.0)
        assert b.vat_wedge == 0.0
        assert b.payout == pytest.approx(167.0)
        assert b.profit == pytest.approx(67.0)
        assert b.margin_pct == pytest.approx(0.67)
        # capital tied up = retail + shipping = 107
        assert b.roic == pytest.approx(67.0 / 107.0)

    def test_minimum_transaction_fee_floors_cheap_sales(self):
        """StockX charges a minimum seller fee (EUR 5.00 in the EU). Without
        this floor a cheap flip looks more profitable than it is."""
        cfg = ProfitConfig(transaction_fee_pct=0.09, processing_fee_pct=0.03,
                           min_transaction_fee_eur=5.0,
                           shipping_to_stockx_eur=7.0, undercut_eur=0.0)
        # 40 * 0.09 = 3.60, below the floor -> charged 5.00
        cheap = breakdown("sell_now", 40.0, 20.0, cfg, NO_VAT)
        assert cheap.transaction_fee == pytest.approx(5.0)
        assert cheap.payout == pytest.approx(40.0 - 5.0 - 1.2 - 7.0)

        # break-even point: 5.00 / 0.09 = 55.56
        assert breakdown("sell_now", 55.0, 20.0, cfg, NO_VAT
                         ).transaction_fee == pytest.approx(5.0)
        # above it the percentage governs again
        assert breakdown("sell_now", 200.0, 20.0, cfg, NO_VAT
                         ).transaction_fee == pytest.approx(18.0)

    def test_vat_disabled_is_strict_noop(self):
        assert vat_wedge(200.0, 100.0, NO_VAT) == 0.0

    def test_vat_enabled_both_sides(self):
        vat = VatConfig(enabled=True, rate=0.24,
                        output_vat_on_sale=True, input_vat_reclaimable=True)
        # 200*0.24/1.24 - 100*0.24/1.24
        assert vat_wedge(200.0, 100.0, vat) == pytest.approx(
            (200.0 - 100.0) * 0.24 / 1.24)


class TestScenarios:
    def test_no_bid_means_no_sell_now(self):
        sn, la = scenarios(100.0, md(bid=None, ask=150.0), PROFIT, NO_VAT)
        assert sn is None and la is not None

    def test_no_ask_means_no_list_ask(self):
        sn, la = scenarios(100.0, md(bid=150.0, ask=None), PROFIT, NO_VAT)
        assert sn is not None and la is None

    def test_zero_prices_are_absent_market_sides(self):
        sn, la = scenarios(100.0, md(bid=0.0, ask=0.0), PROFIT, NO_VAT)
        assert sn is None and la is None

    def test_undercut_applies_to_ask_only(self):
        cfg = ProfitConfig(transaction_fee_pct=0.10, processing_fee_pct=0.03,
                           shipping_to_stockx_eur=7.0, undercut_eur=5.0)
        _, la = scenarios(100.0, md(ask=150.0), cfg, NO_VAT)
        assert la.sale_price == pytest.approx(145.0)


class TestOpportunityScore:
    """audit 0.2: all early production scores were exactly 2 x list_ask.profit
    because ranking used best_profit. The score must scale the same gated
    profit the min-profit filter judged."""

    def test_score_is_gated_profit_times_factor(self):
        assert opportunity_score(10.0, True, md(bid=100.0)) == 20.0
        assert opportunity_score(10.0, False, md(bid=100.0)) == 10.0

    def test_untakeable_ask_profit_does_not_inflate_score(self):
        # gate says +10 (bid); the ask-side +155 must be invisible to ranking
        assert opportunity_score(10.0, True, md(bid=100.0, ask=400.0)) == 20.0

    def test_no_bid_halves(self):
        assert opportunity_score(10.0, False, md(bid=None)) == 5.0

    def test_sales_velocity_boost_capped(self):
        assert opportunity_score(10.0, False, md(bid=100.0, sales_72h=5)) == 15.0
        assert opportunity_score(10.0, False, md(bid=100.0, sales_72h=99)) == 20.0


class TestDaysToClear:
    def test_none_without_velocity(self):
        assert est_days_to_clear(md()) is None
        assert est_days_to_clear(md(sales_72h=0)) is None

    def test_queue_over_velocity(self):
        assert est_days_to_clear(md(sales_72h=3, num_asks=10)) == 10.0
