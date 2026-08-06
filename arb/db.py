"""SQLite state: retail snapshots, StockX catalog cache, market snapshots,
opportunity dedup, review queue, API request budget, scan history."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from .models import MarketData, Product, StockXProduct, StockXVariant

SCHEMA = """
CREATE TABLE IF NOT EXISTS retail_products (
    retailer     TEXT NOT NULL,
    url          TEXT NOT NULL,
    style_code   TEXT,
    name         TEXT,
    brand        TEXT,
    price        REAL,
    currency     TEXT,
    sizes_json   TEXT,
    in_stock     INTEGER,
    first_seen   TEXT,
    last_seen    TEXT,
    PRIMARY KEY (retailer, url)
);
CREATE TABLE IF NOT EXISTS stockx_products (
    cache_key    TEXT PRIMARY KEY,          -- normalized style code
    found        INTEGER NOT NULL,          -- 0 = negative cache
    product_json TEXT,
    confidence   REAL,
    resolved_at  TEXT
);
CREATE TABLE IF NOT EXISTS stockx_variants (
    product_id   TEXT NOT NULL,
    variant_id   TEXT NOT NULL,
    variant_json TEXT,
    PRIMARY KEY (product_id, variant_id)
);
-- How close each matched product last came to being profitable. Drives how
-- often we spend an API call re-checking its bids: near-misses are watched
-- closely, hopeless ones rarely. Without this, a flat refresh of everything
-- would burn the whole daily budget on shoes whose bid is 13% of retail.
CREATE TABLE IF NOT EXISTS sku_watch (
    product_id  TEXT PRIMARY KEY,
    best_profit REAL,          -- best sell-now profit at last evaluation
    checked_at  TEXT
);
CREATE TABLE IF NOT EXISTS stockx_gtins (
    gtin         TEXT PRIMARY KEY,          -- EAN/UPC printed on the shoe box
    found        INTEGER NOT NULL,          -- 0 = StockX has no such barcode
    variant_json TEXT,
    resolved_at  TEXT
);
CREATE TABLE IF NOT EXISTS market_snapshots (
    variant_id   TEXT PRIMARY KEY,
    product_id   TEXT,
    fetched_at   TEXT,
    data_json    TEXT
);
CREATE TABLE IF NOT EXISTS market_history (
    variant_id   TEXT,
    fetched_at   TEXT,
    lowest_ask   REAL,
    highest_bid  REAL
);
CREATE TABLE IF NOT EXISTS opportunities (
    key           TEXT PRIMARY KEY,
    first_alerted TEXT,
    last_alerted  TEXT,
    last_profit   REAL,
    was_in_stock  INTEGER DEFAULT 1,
    payload_json  TEXT
);
CREATE TABLE IF NOT EXISTS review_queue (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    TEXT,
    retailer      TEXT,
    url           TEXT,
    style_code    TEXT,
    reason        TEXT,
    detail_json   TEXT,
    resolved      INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS api_requests (
    ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scan_log (
    started_at    TEXT,
    retailer      TEXT,
    products_seen INTEGER,
    candidates    INTEGER,
    opportunities INTEGER,
    alerts_sent   INTEGER
);
-- Completed sales, entered by hand after money actually lands. This is the
-- only place REAL outcomes live: everything else in this DB is a prediction.
-- Comparing predicted_payout against payout_eur is what tells us whether the
-- fee model matches reality (the first logged sale showed the model EUR 3.80
-- optimistic, because shipping_to_stockx_eur was set below the true label cost).
CREATE TABLE IF NOT EXISTS sales (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    sold_at           TEXT,
    retailer          TEXT,
    style_code        TEXT,
    title             TEXT,
    size_label        TEXT,
    variant_id        TEXT,
    paid_eur          REAL,      -- landed cost you actually paid
    bid_eur           REAL,      -- the bid you sold into
    payout_eur        REAL,      -- what StockX actually paid you
    predicted_payout  REAL,      -- what the fee model said you would get
    note              TEXT
);
CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, path: Path | str):
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # -- kv ------------------------------------------------------------------
    def kv_get(self, key: str) -> Optional[str]:
        row = self.conn.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
        return row["v"] if row else None

    def kv_set(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO kv (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (key, value))
        self.conn.commit()

    # -- retail products -----------------------------------------------------
    def upsert_retail_product(self, p: Product) -> dict:
        """Store latest sighting; returns {is_new, price_dropped, restocked, old_price}."""
        row = self.conn.execute(
            "SELECT price, in_stock FROM retail_products WHERE retailer=? AND url=?",
            (p.retailer, p.url)).fetchone()
        flags = {"is_new": row is None, "price_dropped": False, "restocked": False,
                 "old_price": row["price"] if row else None}
        if row is not None:
            if row["price"] is not None and p.price < row["price"] - 0.01:
                flags["price_dropped"] = True
            if not row["in_stock"] and p.in_stock:
                flags["restocked"] = True
        sizes_json = json.dumps([s.model_dump() for s in p.sizes])
        self.conn.execute(
            """INSERT INTO retail_products
               (retailer, url, style_code, name, brand, price, currency, sizes_json,
                in_stock, first_seen, last_seen)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(retailer, url) DO UPDATE SET
                 style_code=excluded.style_code, name=excluded.name, brand=excluded.brand,
                 price=excluded.price, currency=excluded.currency,
                 sizes_json=excluded.sizes_json, in_stock=excluded.in_stock,
                 last_seen=excluded.last_seen""",
            (p.retailer, p.url, p.style_code, p.name, p.brand, p.price, p.currency,
             sizes_json, int(p.in_stock), _now(), _now()))
        self.conn.commit()
        return flags

    def find_by_style_code(self, retailer: str, style_code: str) -> Optional[dict]:
        """Same shoe at another storefront, if we've seen it there."""
        row = self.conn.execute(
            "SELECT name, price, currency, in_stock, url, sizes_json "
            "FROM retail_products WHERE retailer=? AND style_code=? LIMIT 1",
            (retailer, style_code)).fetchone()
        return dict(row) if row else None

    # -- stockx catalog cache ------------------------------------------------
    def get_resolution(self, cache_key: str, negative_ttl_days: int
                       ) -> Optional[tuple[Optional[StockXProduct], float]]:
        """None = never resolved (or negative cache expired).
        (None, 0.0) = cached 'not on StockX'. (product, confidence) = hit."""
        row = self.conn.execute(
            "SELECT * FROM stockx_products WHERE cache_key=?", (cache_key,)).fetchone()
        if row is None:
            return None
        if not row["found"]:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(row["resolved_at"])
            if age > timedelta(days=negative_ttl_days):
                return None
            return (None, 0.0)
        return (StockXProduct.model_validate_json(row["product_json"]), row["confidence"])

    def record_sale(self, *, retailer: str, style_code: Optional[str],
                    title: Optional[str], size_label: Optional[str],
                    variant_id: Optional[str], paid_eur: float,
                    bid_eur: Optional[float], payout_eur: float,
                    predicted_payout: Optional[float],
                    note: str = "") -> int:
        """Log a completed sale. Returns the new row id."""
        cur = self.conn.execute(
            """INSERT INTO sales (sold_at, retailer, style_code, title,
                                  size_label, variant_id, paid_eur, bid_eur,
                                  payout_eur, predicted_payout, note)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (_now(), retailer, style_code, title, size_label, variant_id,
             paid_eur, bid_eur, payout_eur, predicted_payout, note))
        self.conn.commit()
        return int(cur.lastrowid or 0)

    def sales(self) -> list:
        return self.conn.execute(
            "SELECT * FROM sales ORDER BY sold_at").fetchall()

    def drop_resolution(self, cache_key: str) -> None:
        """Forget a cached match. Used when a cached hit no longer satisfies the
        current matching rules, so a logic fix can heal already-poisoned rows
        instead of only applying to codes nobody has looked up yet."""
        self.conn.execute("DELETE FROM stockx_products WHERE cache_key=?",
                          (cache_key,))
        self.conn.commit()

    def put_resolution(self, cache_key: str, product: Optional[StockXProduct],
                       confidence: float) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO stockx_products VALUES (?,?,?,?,?)",
            (cache_key, int(product is not None),
             product.model_dump_json() if product else None, confidence, _now()))
        self.conn.commit()

    def get_watch(self, product_id: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT best_profit, checked_at FROM sku_watch WHERE product_id=?",
            (product_id,)).fetchone()
        return dict(row) if row else None

    def put_watch(self, product_id: str, best_profit: Optional[float]) -> None:
        self.conn.execute("INSERT OR REPLACE INTO sku_watch VALUES (?,?,?)",
                          (product_id, best_profit, _now()))
        self.conn.commit()

    def get_gtin(self, gtin: str) -> Optional[tuple[Optional[StockXVariant]]]:
        """None = never looked up. (None,) = cached 'StockX has no such
        barcode'. (variant,) = hit. Barcodes are immutable, so no TTL."""
        row = self.conn.execute(
            "SELECT found, variant_json FROM stockx_gtins WHERE gtin=?",
            (gtin,)).fetchone()
        if row is None:
            return None
        if not row["found"]:
            return (None,)
        return (StockXVariant.model_validate_json(row["variant_json"]),)

    def put_gtin(self, gtin: str, variant: Optional[StockXVariant]) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO stockx_gtins VALUES (?,?,?,?)",
            (gtin, int(variant is not None),
             variant.model_dump_json() if variant else None, _now()))
        self.conn.commit()

    def get_variants(self, product_id: str) -> list[StockXVariant]:
        rows = self.conn.execute(
            "SELECT variant_json FROM stockx_variants WHERE product_id=?",
            (product_id,)).fetchall()
        return [StockXVariant.model_validate_json(r["variant_json"]) for r in rows]

    def put_variants(self, product_id: str, variants: list[StockXVariant]) -> None:
        self.conn.executemany(
            "INSERT OR REPLACE INTO stockx_variants VALUES (?,?,?)",
            [(product_id, v.variant_id, v.model_dump_json()) for v in variants])
        self.conn.commit()

    # -- market data ---------------------------------------------------------
    def get_market_snapshot(self, variant_id: str, max_age_minutes: int
                            ) -> Optional[MarketData]:
        row = self.conn.execute(
            "SELECT * FROM market_snapshots WHERE variant_id=?", (variant_id,)).fetchone()
        if row is None:
            return None
        age = datetime.now(timezone.utc) - datetime.fromisoformat(row["fetched_at"])
        if age > timedelta(minutes=max_age_minutes):
            return None
        return MarketData.model_validate_json(row["data_json"])

    def put_market_data(self, product_id: str, md: MarketData) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO market_snapshots VALUES (?,?,?,?)",
            (md.variant_id, product_id, md.fetched_at.isoformat(), md.model_dump_json()))
        self.conn.execute(
            "INSERT INTO market_history (variant_id, fetched_at, lowest_ask, highest_bid) "
            "VALUES (?,?,?,?)",
            (md.variant_id, md.fetched_at.isoformat(), md.lowest_ask, md.highest_bid))
        self.conn.commit()

    # -- opportunity dedup ---------------------------------------------------
    def should_alert(self, key: str, profit: float, in_stock: bool,
                     re_alert_delta: float) -> tuple[bool, str]:
        """Alert on: never seen, material profit change, or restock."""
        row = self.conn.execute(
            "SELECT last_profit, was_in_stock FROM opportunities WHERE key=?",
            (key,)).fetchone()
        if row is None:
            return True, "new"
        if not row["was_in_stock"] and in_stock:
            return True, "restock"
        if abs(profit - row["last_profit"]) >= re_alert_delta:
            return True, "profit_change"
        return False, "duplicate"

    def record_alert(self, key: str, profit: float, in_stock: bool,
                     payload_json: str) -> None:
        self.conn.execute(
            """INSERT INTO opportunities (key, first_alerted, last_alerted, last_profit,
                                          was_in_stock, payload_json)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(key) DO UPDATE SET
                 last_alerted=excluded.last_alerted, last_profit=excluded.last_profit,
                 was_in_stock=excluded.was_in_stock, payload_json=excluded.payload_json""",
            (key, _now(), _now(), profit, int(in_stock), payload_json))
        self.conn.commit()

    def mark_out_of_stock(self, key: str) -> None:
        self.conn.execute(
            "UPDATE opportunities SET was_in_stock=0 WHERE key=?", (key,))
        self.conn.commit()

    # -- review queue --------------------------------------------------------
    def add_review(self, retailer: str, url: str, style_code: Optional[str],
                   reason: str, detail: dict) -> None:
        # one open entry per url+reason is enough
        row = self.conn.execute(
            "SELECT id FROM review_queue WHERE url=? AND reason=? AND resolved=0",
            (url, reason)).fetchone()
        if row:
            return
        self.conn.execute(
            "INSERT INTO review_queue (created_at, retailer, url, style_code, reason, "
            "detail_json) VALUES (?,?,?,?,?,?)",
            (_now(), retailer, url, style_code, reason, json.dumps(detail, default=str)))
        self.conn.commit()

    # -- api budget ----------------------------------------------------------
    def record_api_request(self) -> None:
        self.conn.execute("INSERT INTO api_requests VALUES (?)", (_now(),))
        self.conn.commit()

    def api_requests_last_24h(self) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        # prune old rows while we're here
        self.conn.execute("DELETE FROM api_requests WHERE ts < ?", (cutoff,))
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM api_requests WHERE ts >= ?", (cutoff,)).fetchone()
        return row["n"]

    # -- scan log ------------------------------------------------------------
    def log_scan(self, retailer: str, started_at: str, products_seen: int,
                 candidates: int, opportunities: int, alerts_sent: int) -> None:
        self.conn.execute(
            "INSERT INTO scan_log VALUES (?,?,?,?,?,?)",
            (started_at, retailer, products_seen, candidates, opportunities, alerts_sent))
        self.conn.commit()
