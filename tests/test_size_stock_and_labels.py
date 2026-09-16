"""Regressions for the 2026-09-16 Phantom 6 alert.

1. Alerted in US 10, which teamsport would not sell: Magento MSI ships
   products[] (exists with stock somewhere) AND salable[] (purchasable on this
   website); the scraper read products[]. Now reads salable when present.
2. The alert printed 'size EU 10 / US 10' — a size that does not exist — because
   the formatter hard-coded 'EU' regardless of the retailer's labelling.
"""
import json

from arb.models import (
    MarketData, Opportunity, Product, ProfitBreakdown, StockXProduct, StockXVariant,
)
from arb.notify import size_text
from arb.retailers.teamsport import _sizes_from_jsonconfig


def _page(with_salable: bool) -> str:
    cfg = {
        "attributes": {"155": {"code": "size", "options": [
            {"id": "63", "label": "10,5", "products": ["131156"]},
            {"id": "60", "label": "10", "products": ["131155"]},
            {"id": "57", "label": "8", "products": ["131159"]},
            {"id": "70", "label": "12", "products": []},
        ]}},
        "index": {}, "productId": "1",
    }
    if with_salable:
        cfg["salable"] = {"155": {"63": ["131156"], "57": ["131159"]}}
    return ('<script>var spConfig = {"jsonConfig": ' + json.dumps(cfg)
            + ', "jsonSwatchConfig": {}};</script>')


class TestSalableStock:
    def test_salable_map_wins_over_products(self):
        sizes = {s.label: s.in_stock for s in _sizes_from_jsonconfig(_page(True))}
        assert sizes == {"10.5": True, "10": False, "8": True, "12": False}

    def test_without_salable_products_is_the_signal(self):
        sizes = {s.label: s.in_stock for s in _sizes_from_jsonconfig(_page(False))}
        assert sizes == {"10.5": True, "10": True, "8": True, "12": False}

    def test_labels_are_us_with_dot_decimals(self):
        s = next(x for x in _sizes_from_jsonconfig(_page(True)) if x.label == "10.5")
        assert s.system == "US" and s.us_size == "10.5"

    def test_unparseable_returns_none_for_fallback(self):
        assert _sizes_from_jsonconfig("<html>no config here</html>") is None


def _opp(size_label, system, us_size, eu_conv=None) -> Opportunity:
    retail = Product(retailer="teamsport", name="x", url="https://t/x",
                     style_code="HV8988-446", price=150.0)
    variant = StockXVariant(product_id="p", variant_id="v", size=us_size,
                            conversions={"eu": eu_conv} if eu_conv else {})
    bd = ProfitBreakdown(scenario="sell_now", sale_price=203, transaction_fee=18.27,
                         processing_fee=6.09, shipping=10, vat_wedge=0,
                         payout=168.64, profit=33.64, margin_pct=0.25, roic=0.23)
    return Opportunity(retail=retail, size_label=size_label, size_system=system,
                       us_size=us_size, stockx=StockXProduct(product_id="p"),
                       variant=variant, market=MarketData(variant_id="v", highest_bid=203),
                       sell_now=bd, match_confidence=1.0)


class TestSizeText:
    def test_us_label_is_not_called_eu(self):
        assert size_text(_opp("10", "US", "10", "EU 44")) == "US 10 (EU 44)"

    def test_us_label_without_conversion(self):
        assert size_text(_opp("10", "US", "10")) == "US 10"

    def test_eu_label_keeps_both_systems(self):
        assert size_text(_opp("44", "EU", "10")) == "EU 44 / US 10"

    def test_default_system_is_eu_for_old_payloads(self):
        o = _opp("44", "EU", "10")
        assert Opportunity.model_validate(o.model_dump(exclude={"size_system"})).size_system == "EU"


def _reede_parser():
    import arb.retailers.reede as reede
    cls = next(v for v in vars(reede).values()
               if isinstance(v, type) and hasattr(v, "_parse_size_config"))
    return cls._parse_size_config


def _reede_page(with_salable: bool) -> str:
    cfg = {"attributes": {"93": {"code": "size", "options": [
        {"id": "5", "label": "42", "products": ["1"], "default_size_type": "EU",
         "brand": "Nike", "gender": "men"},
        {"id": "6", "label": "43", "products": ["2"], "default_size_type": "EU"},
        {"id": "7", "label": "44", "products": [], "default_size_type": "EU"},
    ]}}}
    if with_salable:
        cfg["salable"] = {"93": {"6": ["2"]}}
    return '<script>{"jsonConfig": ' + json.dumps(cfg) + ', "x": 1}</script>'


class TestReedeSalable:
    def test_salable_map_wins(self):
        _, brand, _, sizes = _reede_parser()(_reede_page(True))
        assert brand == "Nike"
        assert {s.label: s.in_stock for s in sizes} == {"42": False, "43": True, "44": False}

    def test_without_salable_products_is_the_signal(self):
        _, _, _, sizes = _reede_parser()(_reede_page(False))
        assert {s.label: s.in_stock for s in sizes} == {"42": True, "43": True, "44": False}
