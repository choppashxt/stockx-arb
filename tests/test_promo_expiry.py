"""Promo-expiry regression tests.

A storewide campaign left in config after it ends prices stock below what it
actually costs, which is how an EUR 9 loss once alerted as +EUR 7 profit
(see the ballzy block in config.yaml). `discount_expires` retires a campaign on
its own, so these tests pin the gate's semantics and the live config's values.
"""
from datetime import date

import pytest

from arb.config import RetailerConfig, load_config, promo_live
from arb.models import Product

TODAY = date(2026, 9, 14)          # the day both campaigns run
TOMORROW = date(2026, 9, 15)


class TestPromoLive:
    def test_no_expiry_is_standing_pricing(self):
        # a loyalty/registered-client rate never lapses
        assert promo_live(None, today=date(2099, 1, 1)) is True

    def test_expiry_day_is_inclusive(self):
        # "today there is -20%" must still be true on the stated day
        assert promo_live(TODAY, today=TODAY) is True

    def test_before_expiry(self):
        assert promo_live(TODAY, today=date(2026, 9, 13)) is True

    def test_after_expiry(self):
        assert promo_live(TODAY, today=TOMORROW) is False


class TestEffectiveDiscounts:
    def test_campaign_applies_on_its_day(self, monkeypatch):
        r = RetailerConfig(discount_pct=0.25, discount_expires=TODAY)
        _freeze(monkeypatch, TODAY)
        assert r.effective_discount_pct == pytest.approx(0.25)

    def test_campaign_lapses_after_its_day(self, monkeypatch):
        r = RetailerConfig(discount_pct=0.25, discount_expires=TODAY)
        _freeze(monkeypatch, TOMORROW)
        assert r.effective_discount_pct == 0.0

    def test_sale_discount_lapses_with_it(self, monkeypatch):
        r = RetailerConfig(sale_discount_pct=0.15, discount_expires=TODAY)
        _freeze(monkeypatch, TOMORROW)
        assert r.effective_sale_discount_pct == 0.0

    def test_standing_discount_never_lapses(self, monkeypatch):
        # teamsport's registered-client 10% carries no expiry and must survive
        r = RetailerConfig(discount_pct=0.10)
        _freeze(monkeypatch, date(2099, 1, 1))
        assert r.effective_discount_pct == pytest.approx(0.10)

    def test_expired_campaign_restores_the_real_landed_cost(self, monkeypatch):
        """The actual bug this guards: the EUR 89 shoe must cost EUR 89 again."""
        r = RetailerConfig(discount_pct=0.25, discount_expires=TODAY)
        _freeze(monkeypatch, TOMORROW)
        p = Product(retailer="ballzy", name="AF1", url="https://x/y", price=89.0,
                    discount_pct=r.effective_discount_pct)
        assert p.landed_cost == pytest.approx(89.0)


@pytest.fixture(scope="module")
def retailers():
    return load_config().retailers


class TestLiveConfig:
    """The campaigns actually running on 2026-09-14."""

    @pytest.mark.parametrize("name,pct", [
        ("ballzy", 0.25),
        ("sportland", 0.20),
    ])
    def test_campaign_is_configured_and_dated(self, retailers, name, pct):
        r = retailers[name]
        assert r.discount_pct == pytest.approx(pct)
        assert r.discount_expires is not None, "a campaign must carry an expiry"

    def test_sportland_lt_is_not_in_the_ee_campaign(self, retailers):
        # Sportland runs storewide campaigns per country. .ee and .lt share a
        # catalog and list price, so an .ee-only promo copied here would price
        # every LT alert 20% under what the LT contact actually pays — and
        # nothing downstream would catch it (confirmed 2026-09-14).
        assert retailers["sportland_lt"].discount_pct == 0.0
        assert retailers["sportland"].discount_pct > 0.0, \
            "guard is meaningless if .ee has no campaign — re-check both"

    def test_ballzy_promo_covers_the_whole_catalogue(self, retailers):
        # Confirmed against Ballzy's promo email: the -25% applies to the whole
        # catalogue, stacking on already-marked-down stock. It must therefore
        # ride discount_pct (unconditional) — moving it to sale_discount_pct
        # would silently stop applying it to full-price items, and the sale
        # categories are ~100% of where Ballzy candidates come from.
        r = retailers["ballzy"]
        assert r.discount_pct > 0.0 and r.sale_discount_pct == 0.0

        def landed(on_sale: bool) -> float:
            return Product(retailer="ballzy", name="X", url="https://x/y",
                           price=100.0, on_sale=on_sale,
                           discount_pct=r.effective_discount_pct,
                           sale_discount_pct=r.effective_sale_discount_pct
                           ).landed_cost

        assert landed(on_sale=True) == pytest.approx(75.0)
        assert landed(on_sale=False) == pytest.approx(75.0)

    def test_teamsport_standing_rate_carries_no_expiry(self, retailers):
        r = retailers["teamsport"]
        assert r.discount_pct == pytest.approx(0.10)
        assert r.discount_expires is None

    def test_every_configured_discount_is_sane(self, retailers):
        for name, r in retailers.items():
            assert 0.0 <= r.discount_pct < 1.0, f"{name} discount_pct out of range"
            assert 0.0 <= r.sale_discount_pct < 1.0, f"{name} sale_discount_pct"


def _freeze(monkeypatch, today: date):
    """Pin the gate's idea of 'now' without touching datetime globally."""
    import arb.config as config

    monkeypatch.setattr(
        config, "promo_live",
        lambda expires, *, today=today: promo_live(expires, today=today))
