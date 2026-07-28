"""Budget-tiering regression (audit 0.3 — the fix that freed ~46k calls/day):
`best_profit IS NULL` must mean two different things —
  * no watch row at all  -> never assessed -> HOT (look now)
  * row with NULL profit -> assessed, no live bid anywhere -> COLD
Pre-fix, both mapped to hot, so 51% of the watchlist (bid-less, can never
alert under require_live_bid) burned half the daily budget at 32x/day."""
from arb.config import AppConfig
from arb.db import Database
from arb.scanner import _market_ttl_minutes


def make() -> tuple[Database, AppConfig]:
    return Database(":memory:"), AppConfig()


class TestMarketTtlTiers:
    def test_never_assessed_is_hot(self):
        db, cfg = make()
        assert _market_ttl_minutes(db, "p-new", cfg) == \
            cfg.stockx.refresh_minutes_hot

    def test_assessed_no_bid_is_cold_not_hot(self):
        db, cfg = make()
        db.put_watch("p-nobid", None)
        assert _market_ttl_minutes(db, "p-nobid", cfg) == \
            cfg.stockx.refresh_minutes_cold

    def test_profitable_is_hot(self):
        db, cfg = make()
        db.put_watch("p-hot", cfg.filters.min_profit_eur + 1.0)
        assert _market_ttl_minutes(db, "p-hot", cfg) == \
            cfg.stockx.refresh_minutes_hot

    def test_near_miss_is_warm(self):
        db, cfg = make()
        gap = cfg.stockx.near_miss_eur / 2
        db.put_watch("p-warm", cfg.filters.min_profit_eur - gap)
        assert _market_ttl_minutes(db, "p-warm", cfg) == \
            cfg.stockx.refresh_minutes_warm

    def test_hopeless_is_cold(self):
        db, cfg = make()
        db.put_watch("p-cold",
                     cfg.filters.min_profit_eur - cfg.stockx.near_miss_eur - 50)
        assert _market_ttl_minutes(db, "p-cold", cfg) == \
            cfg.stockx.refresh_minutes_cold
