"""Model-policy regression tests: landed_cost discount gating (audit 2.4
semantics: sale-only promos must never touch full-price stock), the
tri-state per-size stock flag (audit 0.1), and opportunity dedup keys."""
import pytest

from arb.models import (
    MarketData,
    Opportunity,
    Product,
    RetailSize,
    StockXProduct,
    StockXVariant,
)


def product(**kw) -> Product:
    base = dict(retailer="testshop", name="Test Shoe", url="https://x/y",
                price=100.0)
    base.update(kw)
    return Product(**base)


class TestLandedCost:
    def test_plain(self):
        assert product().landed_cost == 100.0

    def test_standing_discount_always_applies(self):
        assert product(discount_pct=0.10).landed_cost == pytest.approx(90.0)

    def test_sale_discount_only_when_on_sale(self):
        # the promo is sale-items-only; applying it to full price stock
        # overstates profit on every non-sale item (audit 2.4 class of bug)
        assert product(sale_discount_pct=0.15,
                       on_sale=False).landed_cost == pytest.approx(100.0)
        assert product(sale_discount_pct=0.15,
                       on_sale=True).landed_cost == pytest.approx(85.0)

    def test_discounts_stack_multiplicatively_then_extra_cost(self):
        p = product(discount_pct=0.10, sale_discount_pct=0.15, on_sale=True,
                    extra_cost_eur=8.0)
        assert p.landed_cost == pytest.approx(100.0 * 0.9 * 0.85 + 8.0)


class TestRetailSizeTriState:
    def test_default_is_true(self):
        assert RetailSize(label="42").in_stock is True

    def test_none_is_allowed_and_distinct(self):
        # audit 0.1: None = "retailer hides per-size stock" — it must survive
        # serialisation, never collapse to True
        s = RetailSize(label="42", in_stock=None)
        assert s.in_stock is None
        assert RetailSize.model_validate(s.model_dump()).in_stock is None


def opportunity(**kw) -> Opportunity:
    base = dict(
        retail=product(),
        size_label="42",
        us_size="8.5",
        stockx=StockXProduct(product_id="p1"),
        variant=StockXVariant(product_id="p1", variant_id="v1", size="8.5"),
        market=MarketData(variant_id="v1", highest_bid=150.0),
        match_confidence=1.0,
    )
    base.update(kw)
    return Opportunity(**base)


class TestOpportunityKey:
    def test_key_is_shop_variant_size(self):
        assert opportunity().key == "testshop|v1|42"

    def test_key_override_wins(self):
        assert opportunity(key_override="testshop|p1|product").key == \
            "testshop|p1|product"

    def test_best_profit_empty_when_no_scenarios(self):
        assert opportunity().best_profit == 0.0
