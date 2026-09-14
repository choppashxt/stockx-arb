"""Campaign-window throughput boost.

A storewide markdown makes a large slice of a catalogue arbitrageable at once,
so the retailer running it is worth scanning harder — but the heavier crawl
must lapse with the campaign. It is gated on the same discount_expires date as
the discount itself, so a one-day sale cannot leave a permanent over-crawl (or
a permanently larger share of the shared StockX budget) behind it.
"""
from datetime import date

import pytest

from arb.config import RetailerConfig, load_config
from arb.retailers import create_scraper

TODAY = date(2026, 9, 14)
TOMORROW = date(2026, 9, 15)


def _boosted(**kw) -> RetailerConfig:
    base = dict(scan_interval_minutes=20, sitemap_slice_per_scan=1200,
                max_pages=50, discount_pct=0.20, discount_expires=TODAY,
                promo_scan_interval_minutes=14,
                promo_sitemap_slice_per_scan=1800)
    base.update(kw)
    return RetailerConfig(**base)


def _freeze(monkeypatch, today: date):
    import arb.config as config
    from arb.config import promo_live
    monkeypatch.setattr(
        config, "promo_live",
        lambda expires, *, today=today: promo_live(expires, today=today))


class TestBoostWindow:
    def test_boost_applies_while_the_campaign_runs(self, monkeypatch):
        _freeze(monkeypatch, TODAY)
        r = _boosted()
        assert r.promo_boost_active is True
        assert r.effective_scan_interval_minutes == 14
        assert r.promo_adjusted().sitemap_slice_per_scan == 1800

    def test_boost_lapses_with_the_campaign(self, monkeypatch):
        _freeze(monkeypatch, TOMORROW)
        r = _boosted()
        assert r.promo_boost_active is False
        assert r.effective_scan_interval_minutes == 20
        assert r.promo_adjusted().sitemap_slice_per_scan == 1200

    def test_a_boost_without_an_end_date_never_activates(self, monkeypatch):
        # the whole safety property: no expiry, no heavier crawl
        _freeze(monkeypatch, TODAY)
        r = _boosted(discount_expires=None)
        assert r.promo_boost_active is False
        assert r.effective_scan_interval_minutes == 20

    def test_unset_knobs_keep_their_normal_value(self, monkeypatch):
        _freeze(monkeypatch, TODAY)
        r = _boosted(promo_sitemap_slice_per_scan=None)
        assert r.effective_scan_interval_minutes == 14      # still boosted
        assert r.promo_adjusted().sitemap_slice_per_scan == 1200

    def test_promo_adjusted_is_a_copy_not_a_mutation(self, monkeypatch):
        _freeze(monkeypatch, TODAY)
        r = _boosted()
        r.promo_adjusted()
        assert r.sitemap_slice_per_scan == 1200, "base config must be untouched"

    def test_no_boost_configured_returns_self_unchanged(self, monkeypatch):
        _freeze(monkeypatch, TODAY)
        r = RetailerConfig(discount_pct=0.20, discount_expires=TODAY)
        assert r.promo_boost_active is False
        assert r.promo_adjusted() is r


class TestCostPolicySignature:
    """What run_scan compares to notice a campaign starting or ending."""

    def test_changes_when_a_campaign_starts(self, monkeypatch):
        r = _boosted()
        _freeze(monkeypatch, TOMORROW)
        off = r.cost_policy_signature
        _freeze(monkeypatch, TODAY)
        assert r.cost_policy_signature != off

    def test_stable_while_nothing_moves(self, monkeypatch):
        _freeze(monkeypatch, TODAY)
        r = _boosted()
        assert r.cost_policy_signature == r.cost_policy_signature

    def test_tracks_forwarding_cost_too(self):
        a = RetailerConfig(discount_pct=0.1, extra_cost_eur=0.0)
        b = RetailerConfig(discount_pct=0.1, extra_cost_eur=13.0)
        assert a.cost_policy_signature != b.cost_policy_signature


@pytest.fixture(scope="module")
def retailers():
    return load_config().retailers


class TestLiveConfig:
    def test_promo_retailers_are_boosted_today(self, retailers, monkeypatch):
        _freeze(monkeypatch, TODAY)
        assert retailers["ballzy"].effective_scan_interval_minutes == 8
        assert retailers["sportland"].effective_scan_interval_minutes == 12
        assert retailers["ballzy"].scan_interval_minutes == 15      # base intact
        assert retailers["sportland"].scan_interval_minutes == 20

    def test_boost_never_touches_politeness(self, retailers):
        # robots.txt Crawl-delay compliance is not a throughput knob
        assert retailers["ballzy"].request_delay_seconds >= 2.5
        assert retailers["sportland"].request_delay_seconds >= 4.0

    def test_sportland_stays_far_under_the_volume_that_got_us_dropped(
            self, retailers):
        r = retailers["sportland"].promo_adjusted()
        batches = r.sitemap_slice_per_scan / r.graphql_batch_size
        assert batches <= 60, f"{batches} batches/scan approaches the 125 that failed"

    def test_every_boost_in_config_carries_an_expiry(self, retailers):
        for name, r in retailers.items():
            if any((r.promo_scan_interval_minutes, r.promo_sitemap_slice_per_scan,
                    r.promo_max_pages)):
                assert r.discount_expires is not None, \
                    f"{name} has a boost that would never expire"


class TestScraperGetsTheBoost:
    def test_factory_hands_the_scraper_the_adjusted_config(self, monkeypatch):
        _freeze(monkeypatch, TODAY)
        s = create_scraper("sportland", _boosted())
        assert s.cfg.sitemap_slice_per_scan == 1800

    def test_factory_hands_normal_config_once_it_lapses(self, monkeypatch):
        _freeze(monkeypatch, TOMORROW)
        s = create_scraper("sportland", _boosted())
        assert s.cfg.sitemap_slice_per_scan == 1200


class TestCampaignPause:
    """Standing a retailer down so a campaign elsewhere gets the budget.

    paused_until rather than enabled: false, because forgetting to undo this one
    is silent — the shop just stops being scanned and nobody notices the alerts
    that never arrived.
    """

    def test_paused_retailer_is_not_scanned(self, monkeypatch):
        _freeze(monkeypatch, TODAY)
        r = RetailerConfig(enabled=True, paused_until=TODAY)
        assert r.paused_today is True
        assert r.effective_enabled is False

    def test_pause_lifts_on_its_own(self, monkeypatch):
        _freeze(monkeypatch, TOMORROW)
        r = RetailerConfig(enabled=True, paused_until=TODAY)
        assert r.effective_enabled is True

    def test_pause_cannot_re_enable_a_disabled_retailer(self, monkeypatch):
        # sportland_lv is off for a reason that has nothing to do with campaigns
        _freeze(monkeypatch, TOMORROW)
        assert RetailerConfig(enabled=False, paused_until=TODAY).effective_enabled is False

    def test_no_pause_means_enabled_decides(self, monkeypatch):
        _freeze(monkeypatch, TODAY)
        assert RetailerConfig(enabled=True).effective_enabled is True


class TestTodaysFocus:
    """The live config for the 2026-09-14 campaign day."""

    PROMO = {"ballzy", "sportland"}

    def test_only_the_promo_shops_run_today(self, retailers, monkeypatch):
        _freeze(monkeypatch, TODAY)
        live = {n for n, r in retailers.items() if r.effective_enabled}
        assert live == self.PROMO

    def test_the_promo_shops_are_never_the_ones_paused(self, retailers):
        for n in self.PROMO:
            assert retailers[n].paused_until is None, f"{n} is the point of today"

    def test_everything_comes_back_tomorrow(self, retailers, monkeypatch):
        _freeze(monkeypatch, TOMORROW)
        live = {n for n, r in retailers.items() if r.effective_enabled}
        assert live > self.PROMO, "a pause that outlives the campaign is the bug"
        assert "reede" in live and "rademar" in live and "sns" in live

    def test_no_pause_is_open_ended(self, retailers):
        # a pause with no end date cannot be expressed, but check the intent:
        # every paused shop carries a real date, not a far-future placeholder
        for n, r in retailers.items():
            if r.paused_until is not None:
                assert r.paused_until <= date(2026, 9, 30), \
                    f"{n} paused until {r.paused_until} — that is not a campaign"
