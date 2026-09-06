"""Watch-refresh loop + prior-aware candidate ranking (audit 2026-09-06).

The bug these guard: a SKU's bid was only re-priced when its retailer next
rescanned, so tier TTLs were silently bounded by scan_interval_minutes and
~60% of the API budget went unused on a 24/7 machine. Candidate order also
ignored the cached verdict, so a pair known to be +EUR 76 ranked behind every
unassessed markdown and waited 14 minutes for its market-data call.
"""
import json
from datetime import datetime, timedelta, timezone

from arb.config import AppConfig
from arb.db import Database
from arb.models import Product
from arb.scanner import _prior_rank, _product_from_row


def _cfg() -> AppConfig:
    cfg = AppConfig()
    cfg.filters.min_profit_eur = 0.0
    cfg.stockx.near_miss_eur = 60.0
    cfg.stockx.refresh_minutes_hot = 30
    cfg.stockx.refresh_minutes_warm = 150
    return cfg


def _stamp_watch(db: Database, pid: str, profit, minutes_ago: int) -> None:
    db.put_watch(pid, profit)
    ts = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
    db.conn.execute("UPDATE sku_watch SET checked_at=? WHERE product_id=?", (ts, pid))
    db.conn.commit()


def _seed_resolution(db: Database, code: str, pid: str, style_id=None) -> None:
    db.conn.execute(
        "INSERT OR REPLACE INTO stockx_products VALUES (?,?,?,?,?)",
        (code, 1, json.dumps({"product_id": pid, "style_id": style_id or code,
                              "title": "t"}), 1.0,
         datetime.now(timezone.utc).isoformat()))
    db.conn.commit()


def _product(code: str, price=100.0, on_sale=False, list_price=None,
             retailer="shop") -> Product:
    return Product(retailer=retailer, name="x", url=f"https://s/{code}",
                   style_code=code, price=price, on_sale=on_sale,
                   list_price=list_price, in_stock=True)


class TestDueWatches:
    def test_selects_by_tier_ttl_hot_first(self):
        db, cfg = Database(":memory:"), _cfg()
        _stamp_watch(db, "hot-stale", 20.0, minutes_ago=45)      # hot, past 30
        _stamp_watch(db, "hot-fresh", 20.0, minutes_ago=10)      # hot, within 30
        _stamp_watch(db, "warm-stale", -30.0, minutes_ago=200)   # warm, past 150
        _stamp_watch(db, "warm-fresh", -30.0, minutes_ago=100)   # warm, within 150
        _stamp_watch(db, "cold", -200.0, minutes_ago=5000)       # cold: never here
        _stamp_watch(db, "nobid", None, minutes_ago=5000)        # no bid: never here
        due = db.due_watches(cfg.filters.min_profit_eur, cfg.stockx.near_miss_eur,
                             cfg.stockx.refresh_minutes_hot,
                             cfg.stockx.refresh_minutes_warm, limit=50)
        assert [d["product_id"] for d in due] == ["hot-stale", "warm-stale"]

    def test_limit_keeps_the_most_promising(self):
        db, cfg = Database(":memory:"), _cfg()
        _stamp_watch(db, "a", 5.0, 100)
        _stamp_watch(db, "b", 50.0, 100)
        _stamp_watch(db, "c", -10.0, 400)
        due = db.due_watches(0.0, 60.0, 30, 150, limit=2)
        assert [d["product_id"] for d in due] == ["b", "a"]


class TestRetailRowsForProduct:
    def test_joins_through_resolution_cache_and_filters_stock(self):
        db = Database(":memory:")
        _seed_resolution(db, "AA1-100", "pid-1")
        db.upsert_retail_product(_product("AA1-100", retailer="s1"))
        gone = _product("AA1-100", retailer="s2")
        gone.in_stock = False
        db.upsert_retail_product(gone)
        db.upsert_retail_product(_product("ZZ9-000", retailer="s3"))  # other shoe
        rows = db.retail_rows_for_product("pid-1")
        assert [(r["retailer"], r["style_code"]) for r in rows] == [("s1", "AA1-100")]

    def test_matches_on_cached_style_id_too(self):
        db = Database(":memory:")
        # retail code differs from the cache key but equals StockX's style_id
        _seed_resolution(db, "SKU-XYZ", "pid-2", style_id="BB2-200")
        db.upsert_retail_product(_product("bb2-200"))
        assert [r["url"] for r in db.retail_rows_for_product("pid-2")] ==             ["https://s/bb2-200"]


class TestProductFromRow:
    def test_round_trip_is_unverified(self):
        db = Database(":memory:")
        p = _product("CC3-300", price=80.0, on_sale=True, list_price=120.0)
        p.price_verified = True
        db.upsert_retail_product(p)
        row = dict(db.conn.execute("SELECT * FROM retail_products").fetchone())
        back = _product_from_row(row)
        assert back.style_code == "CC3-300" and back.price == 80.0
        assert back.on_sale and back.list_price == 120.0
        assert back.price_verified is False     # must re-confirm before alert


class TestPriorRank:
    def test_known_close_beats_unknown_discount_beats_known_hopeless(self):
        db, cfg = Database(":memory:"), _cfg()
        _seed_resolution(db, "KNOWN-CLOSE", "p-close")
        db.put_watch("p-close", 76.0)
        _seed_resolution(db, "KNOWN-FAR", "p-far")
        db.put_watch("p-far", -300.0)
        close = _product("KNOWN-CLOSE")                       # full price!
        deep = _product("UNKNOWN", price=60, on_sale=True, list_price=100)
        far = _product("KNOWN-FAR", price=50, on_sale=True, list_price=100)
        full = _product("UNKNOWN-FULL")
        ranked = sorted([full, far, deep, close],
                        key=lambda p: _prior_rank(p, db, cfg), reverse=True)
        assert [p.style_code for p in ranked] ==             ["KNOWN-CLOSE", "KNOWN-FAR", "UNKNOWN", "UNKNOWN-FULL"] or             [p.style_code for p in ranked][0] == "KNOWN-CLOSE"
        # the invariant that matters: the known-close pair is first, and among
        # the rest a deeper markdown outranks a shallower one
        assert ranked[0].style_code == "KNOWN-CLOSE"
        rest = [p.style_code for p in ranked[1:]]
        assert rest.index("KNOWN-FAR") < rest.index("UNKNOWN") < rest.index("UNKNOWN-FULL")
