"""Sizeless (single-variant) products: Lego, trading cards, electronics, watches.

Live probe 2026-07-29 established the shape these arrive in:
  - exactly ONE variant, `variantValue` None, size chart conversions empty
  - `styleId` is an EMPTY STRING — the identifying code is in the TITLE
  - healthy live bids exist (Lego 75192 €442, Pokemon 151 ETB €370,
    G-Shock €283, PS5 Pro €1984), so they are genuinely alertable
  - region-specific electronics are SEPARATE products whose titles differ only
    by a "(US Plug)" / "(EU Plug)" marker

Before this path existed, every one of these produced zero opportunities: the
size loop was the only generator and `Opportunity.size_label` was required.
"""
import asyncio

import pytest

from arb.config import ProfitConfig, VatConfig
from arb.matching import code_in_title, region_marker, region_mismatch
from arb.models import (
    MarketData,
    Opportunity,
    Product,
    StockXProduct,
    StockXVariant,
)
from arb.notify import format_opportunity
from arb.profit import scenarios
from arb.scanner import _is_sizeless

LEGO_TITLE = ("LEGO Star Wars Millennium Falcon Ultimate Collector Series "
              "Set 75192")
PS5_TITLE = ("Sony PlayStation 5 PS5 Pro 30th Anniversary Limited Edition "
             "Bundle (US Plug)")


def _variant(size=None, vid="v1"):
    return StockXVariant(product_id="p1", variant_id=vid, size=size,
                         conversions={})


class TestIsSizeless:
    def test_single_variant_without_size(self):
        assert _is_sizeless([_variant(size=None)]) is True

    def test_empty_string_and_whitespace_count_as_no_size(self):
        assert _is_sizeless([_variant(size="")]) is True
        assert _is_sizeless([_variant(size="   ")]) is True

    def test_single_variant_with_a_size_is_still_a_sneaker(self):
        """A one-variant SNEAKER must keep going down the size-matching path —
        it has a real size and claiming otherwise would drop the size check."""
        assert _is_sizeless([_variant(size="10")]) is False

    def test_multiple_variants_never_sizeless(self):
        assert _is_sizeless([_variant(size=None, vid="a"),
                             _variant(size=None, vid="b")]) is False

    def test_no_variants(self):
        assert _is_sizeless([]) is False


class TestCodeInTitle:
    def test_lego_set_number(self):
        assert code_in_title("75192", LEGO_TITLE) is True

    def test_watch_model_reference(self):
        assert code_in_title(
            "GA-110PKM-7A",
            "Casio G-Shock x Pokemon 30th Anniversary GA-110PKM-7A") is True

    def test_separator_noise_is_tolerated(self):
        assert code_in_title("GA110PKM7A",
                             "Casio G-Shock GA-110PKM-7A") is True

    def test_wrong_set_number_rejected(self):
        assert code_in_title("75193", LEGO_TITLE) is False

    def test_substring_of_a_longer_number_rejected(self):
        """Searching '75192' really did return a Gucci jogger with styleId
        '715192 XJETI 2270'. Codes must match on a token boundary, so a code
        that is merely a substring of a longer number must not match."""
        assert code_in_title("5192", "Gucci Jumbo GG Jogging Pant 715192") is False
        assert code_in_title("21044", "LEGO Architecture Set 210440") is False

    def test_short_codes_are_never_identifying(self):
        """3 characters collide with years, counts and piece numbers."""
        assert code_in_title("192", LEGO_TITLE) is False

    def test_no_title(self):
        assert code_in_title("75192", None) is False


class TestRegionMarker:
    @pytest.mark.parametrize("title,expected", [
        (PS5_TITLE, "US Plug"),
        ("Nintendo Switch 2 (EU Plug)", "EU Plug"),
        ("Dyson Airwrap (220-240V US Plug)", "220-240V US Plug"),
        ("Apple AirPods Max (2024)", None),
        (LEGO_TITLE, None),
    ])
    def test_extraction(self, title, expected):
        assert region_marker(title) == expected

    def test_unconfirmed_marker_blocks(self):
        """An Estonian shop selling a EU-plug console rarely says so, and
        silence is not agreement — this has to block, not assume."""
        assert region_mismatch(
            PS5_TITLE, "Sony PlayStation 5 Pro 30th Anniversary") == "US Plug"

    def test_matching_region_is_allowed(self):
        assert region_mismatch(PS5_TITLE, "Sony PS5 Pro (US Plug)") is None

    def test_no_marker_never_blocks(self):
        assert region_mismatch(LEGO_TITLE, "LEGO Millennium Falcon") is None


def _sizeless_opportunity():
    retail = Product(retailer="apollo", name="LEGO Millennium Falcon 75192",
                     brand="LEGO", style_code="75192", category="collectibles",
                     url="https://www.apollo.ee/x", price=599.0, sizes=[],
                     in_stock=True, price_verified=True)
    market = MarketData(variant_id="fix-lego-one", currency="EUR",
                        lowest_ask=900.0, highest_bid=780.0)
    profit = ProfitConfig(transaction_fee_pct=0.09, processing_fee_pct=0.03,
                          min_transaction_fee_eur=5.0,
                          shipping_to_stockx_eur=7.0, undercut_eur=0.0)
    sell_now, list_ask = scenarios(retail.landed_cost, market, profit,
                                   VatConfig(enabled=False))
    return Opportunity(
        retail=retail, stockx=StockXProduct(product_id="p1", style_id="",
                                            title=LEGO_TITLE, brand="LEGO"),
        variant=_variant(vid="fix-lego-one"), market=market,
        sell_now=sell_now, list_ask=list_ask, match_confidence=1.0,
        size_match_method="sizeless (single variant)")


class TestSizelessOpportunity:
    def test_sizeless_flag(self):
        assert _sizeless_opportunity().sizeless is True

    def test_dedup_key_has_no_none_in_it(self):
        """Interpolating a missing size produced keys ending in '|None', which
        collide across genuinely different things."""
        key = _sizeless_opportunity().key
        assert key == "apollo|fix-lego-one"
        assert "None" not in key

    def test_alert_states_no_size_and_warns_about_sealed(self):
        text = format_opportunity(_sizeless_opportunity(), "new")
        assert "one variant, no size" in text
        assert "SEALED" in text
        assert "collectibles" in text
        # must never invent a size for something that has none
        assert "size EU" not in text
        assert "None" not in text

    def test_alert_surfaces_a_region_marker_when_present(self):
        opp = _sizeless_opportunity()
        opp.stockx.title = PS5_TITLE
        assert "US Plug" in format_opportunity(opp, "new")


class TestFixturePipeline:
    """End-to-end through the real evaluator with the offline fixtures."""

    def test_sizeless_produces_one_opportunity_and_region_blocks(self):
        # asyncio.run rather than pytest-asyncio: this is the only async test
        # in the suite and it is not worth a new dependency for one case.
        asyncio.run(self._run())

    async def _run(self):
        from arb import fixtures
        from arb.config import load_config
        from arb.db import Database
        from arb.scanner import _evaluate_product
        from arb.stockx.catalog import CatalogResolver
        from arb.stockx.market import FixtureProvider

        cfg = load_config()
        db = Database(":memory:")
        fixtures.seed_catalog(db)
        provider = FixtureProvider(fixtures.market_data())
        resolver = CatalogResolver(None, db, 3)

        found = {}
        for product in fixtures.retail_products():
            opps, _ = await _evaluate_product(
                product, cfg, db, resolver, provider, scraper=None,
                allow_market_call=True)
            found[product.style_code] = opps

        lego = found["75192"]
        assert len(lego) == 1, "sizeless Lego must yield one product-level signal"
        assert lego[0].sizeless
        assert lego[0].sell_now.profit > cfg.filters.min_profit_eur

        # clears the profit floor by a mile, but the region marker is unconfirmed
        assert found["PS5PRO30TH"] == []
