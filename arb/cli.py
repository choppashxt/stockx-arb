"""CLI entry point.

  python -m arb scan [--once] [--dry-run] [--retailer NAME] [--limit N]
                     [--provider official|fixture]
  python -m arb selftest          offline pipeline check, no network/creds
  python -m arb resolve CODE      one live style-code resolution (debug)
  python -m arb status            DB / budget overview
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

# The AV product on this machine injects SSLKEYLOGFILE=\\.\aswMonFltProxy\<handle>
# into every process it launches. ssl.create_default_context() honours that
# variable, cannot open the device, and every httpx client construction dies
# with PermissionError before a single request is made. We never want to dump
# TLS session keys anyway, so drop it before any SSL context exists.
os.environ.pop("SSLKEYLOGFILE", None)

try:
    import truststore
    truststore.inject_into_ssl()      # this machine TLS-intercepts; trust OS store
except ImportError:
    pass

from .config import AppConfig, Secrets, load_config
from .db import Database
from .notify import ConsoleNotifier, DiscordNotifier, Notifier, TelegramNotifier
from .stockx.auth import TokenManager
from .stockx.client import StockXAPIError, StockXClient
from .stockx.market import StockXOfficialProvider

log = logging.getLogger("arb")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S", stream=sys.stdout)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _build_stockx(cfg: AppConfig, secrets: Secrets, db: Database):
    from .stockx.auth import TokenManager
    from .stockx.catalog import CatalogResolver
    from .stockx.client import StockXClient
    from .stockx.market import StockXOfficialProvider

    client = StockXClient(TokenManager(secrets, db), db, cfg.stockx)
    resolver = CatalogResolver(client, db, cfg.stockx.negative_cache_days,
                               cfg.stockx.max_new_resolutions_per_scan,
                               cfg.stockx.gtin_lookups_per_scan)
    provider = StockXOfficialProvider(client, db, cfg.stockx)
    return client, resolver, provider


def _build_notifier(cfg: AppConfig, secrets: Secrets, dry_run: bool) -> Notifier:
    method = cfg.notifications.method
    if dry_run or method == "console":
        return ConsoleNotifier()
    if method == "discord":
        return DiscordNotifier(secrets)
    if method == "telegram":
        return TelegramNotifier(secrets)
    raise SystemExit(f"unknown notifications.method '{method}'")


async def _cmd_scan(args) -> int:
    cfg = load_config(args.config)
    if getattr(args, "backfill", False):
        # one-time full-catalog pass: lift the per-scan caps; the daily budget
        # guard and 1 req/s throttle still apply, and everything lands in cache
        cfg.stockx.max_new_resolutions_per_scan = 10**9
        cfg.stockx.market_calls_per_scan = 10**9
        args.once = True
    secrets = Secrets()
    if args.provider == "fixture":
        # fixture runs seed fake catalog data — keep them out of the real state
        db = Database(cfg.db_file.with_name("state-fixture.db"))
    else:
        db = Database(cfg.db_file)
    notifier = _build_notifier(cfg, secrets, args.dry_run)

    if args.provider == "fixture":
        from . import fixtures
        from .stockx.market import FixtureProvider
        fixtures.seed_catalog(db)
        provider = FixtureProvider(fixtures.market_data())
        client = None

        from .stockx.catalog import CatalogResolver
        resolver = CatalogResolver(None, db, cfg.stockx.negative_cache_days)  # type: ignore[arg-type]
    else:
        if not secrets.stockx_configured:
            log.error("STOCKX_* credentials missing in .env — run with "
                      "--provider fixture to test without them")
            return 2
        client, resolver, provider = _build_stockx(cfg, secrets, db)

    from .scanner import run_loop, run_scan
    try:
        if args.once:
            retailers = ([args.retailer] if args.retailer else
                         [n for n, rc in cfg.retailers.items() if rc.enabled])
            for name in retailers:
                await run_scan(name, cfg, db, resolver, provider, notifier,
                               limit=args.limit)
        else:
            await run_loop(cfg, db, resolver, provider, notifier,
                           retailer_filter=args.retailer)
    finally:
        if client is not None:
            await client.close()
        await notifier.close()
        db.close()
    return 0


async def _cmd_auth(args) -> int:
    from .stockx.authorize import run_auth_flow
    await run_auth_flow(manual=args.manual, port=args.port,
                        redirect_uri=args.redirect_uri)
    return 0


async def _cmd_selftest(args) -> int:
    """Offline end-to-end: fixtures through the real pipeline, in-memory DB."""
    from . import fixtures
    from .scanner import _evaluate_product, _maybe_alert, ScanStats
    from .stockx.catalog import CatalogResolver
    from .stockx.market import FixtureProvider

    cfg = load_config(args.config)
    db = Database(":memory:")
    fixtures.seed_catalog(db)
    provider = FixtureProvider(fixtures.market_data())
    resolver = CatalogResolver(None, db, 3)  # type: ignore[arg-type]
    notifier = ConsoleNotifier()

    failures: list[str] = []
    all_opps = []
    for product in fixtures.retail_products():
        opps, _ = await _evaluate_product(product, cfg, db, resolver, provider,
                                          scraper=None, allow_market_call=True)
        all_opps.extend(opps)

    # expectations: AJ1 size 42 profitable; size 44 null-market (skipped);
    # size 45 out of stock (skipped); Campus below min profit; sizeless Lego
    # profitable at product level; sizeless PS5 refused on its region marker
    # despite clearing the floor by a mile.
    #
    # The old assertion here was "exactly 1 opportunity, size 42" — it could
    # not observe a sizeless product at all, which is why the expansion plan's
    # "re-run the sneakers unchanged" acceptance test proved nothing.
    sized = [o for o in all_opps if not o.sizeless]
    sizeless = [o for o in all_opps if o.sizeless]
    if len(sized) != 1:
        failures.append(f"expected exactly 1 sized opportunity, got {len(sized)}")
    if len(sizeless) != 1:
        failures.append(f"expected exactly 1 sizeless opportunity, got "
                        f"{len(sizeless)}")
    elif sizeless[0].retail.style_code != "75192":
        failures.append(f"wrong sizeless item: {sizeless[0].retail.style_code}")
    if any(o.retail.style_code == "PS5PRO30TH" for o in all_opps):
        failures.append("region-marked product alerted without confirmation")

    if sized:
        opp = sized[0]
        if opp.size_label != "42":
            failures.append(f"wrong size alerted: {opp.size_label}")
        # Derived from the live config, not hardcoded: fee literals here went
        # stale the moment the Level 1 rate was corrected 9.5% -> 9% (audit 10.3).
        p = cfg.profit
        expected_profit = (145
                           - max(145 * p.transaction_fee_pct,
                                 p.min_transaction_fee_eur)
                           - 145 * p.processing_fee_pct
                           - p.shipping_to_stockx_eur
                           - 79)
        if abs(opp.sell_now.profit - expected_profit) > 0.01:
            failures.append(f"sell_now profit {opp.sell_now.profit:.2f} != "
                            f"{expected_profit:.2f}")
        if opp.market.sales_72h is not None:
            failures.append("official-style fixture should have None sales_72h")

        class _NoEnrich:
            async def enrich(self, p):  # price_verified fixtures never enrich
                raise AssertionError("enrich should not be called")

        stats = ScanStats()
        await _maybe_alert(opp, _NoEnrich(), cfg, db, notifier, stats)
        if stats.alerts_sent != 1:
            failures.append("first alert was not sent")
        await _maybe_alert(opp, _NoEnrich(), cfg, db, notifier, stats)
        if stats.alerts_sent != 1:
            failures.append("duplicate alert was not suppressed")

    print()
    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        return 1
    print("selftest OK: 1 opportunity found, profit math checks out, "
          "null market data skipped, duplicate suppressed")
    return 0


async def _cmd_resolve(args) -> int:
    cfg = load_config(args.config)
    secrets = Secrets()
    if not secrets.stockx_configured:
        log.error("STOCKX_* credentials missing in .env")
        return 2
    db = Database(cfg.db_file)
    client, resolver, provider = _build_stockx(cfg, secrets, db)
    try:
        product, confidence = await resolver.resolve([args.code])
        if product is None:
            print(f"no StockX match for {args.code}")
            return 1
        print(f"{product.title}  [{product.brand}]")
        print(f"  productId : {product.product_id}")
        print(f"  styleId   : {product.style_id}")
        print(f"  url       : {product.url}")
        print(f"  confidence: {confidence}")
        variants = await resolver.variants(product.product_id)
        print(f"  variants  : {len(variants)}")
        if args.market:
            from .stockx.client import StockXAPIError
            try:
                md = await provider.get_market_data(product.product_id)
            except StockXAPIError as e:
                print(f"  market data unavailable: {e.message}")
                return 1
            for v in variants:
                m = md.get(v.variant_id)
                if m:
                    print(f"    US {v.size or '?':>5}  ask {m.lowest_ask}  "
                          f"bid {m.highest_bid}")
    finally:
        await client.close()
        db.close()
    return 0


async def _cmd_report(args) -> int:
    """What can I buy and flip RIGHT NOW?

    Re-checks every recorded opportunity against live bids and the current
    retail price before printing. The stored figures are history: bids get
    taken and prices change, so printing them unverified would invite buying
    against a bid that no longer exists.
    """
    import json as _json
    import sqlite3

    from .models import Opportunity
    from .profit import scenarios

    cfg = load_config(args.config)
    # read-only: the scanner may be mid-write, and we must never block it
    ro = sqlite3.connect(f"file:{cfg.db_file}?mode=ro", uri=True, timeout=30)
    ro.row_factory = sqlite3.Row
    rows = ro.execute("SELECT key, last_alerted, payload_json "
                      "FROM opportunities").fetchall()
    if not rows:
        print("no opportunities recorded yet")
        ro.close()
        return 0

    secrets = Secrets()
    if args.no_verify or not secrets.stockx_configured:
        if not args.no_verify:
            print("(StockX not configured — showing stored figures UNVERIFIED)")
        _print_stored(rows, ro, args.limit)
        ro.close()
        return 0

    # provider caches into a scratch DB so we never contend for the write lock
    scratch = Database(":memory:")
    client = StockXClient(TokenManager(secrets, scratch), scratch, cfg.stockx)
    provider = StockXOfficialProvider(client, scratch, cfg.stockx)
    live, gone = [], []
    try:
        for r in rows:
            o = Opportunity.model_validate_json(r["payload_json"])
            cur = ro.execute("SELECT price, in_stock, sizes_json FROM "
                             "retail_products WHERE retailer=? AND url=?",
                             (o.retail.retailer, o.retail.url)).fetchone()
            if cur is None or not cur["in_stock"]:
                gone.append((o, "out of stock"))
                continue
            size_ok = None
            if (sizes := _json.loads(cur["sizes_json"] or "[]")):
                m = [s for s in sizes if s["label"] == o.size_label]
                if m and m[0].get("in_stock") is None:
                    size_ok = None      # per-size stock unverified (Sportland)
                else:
                    size_ok = bool(m and m[0].get("in_stock"))
            try:
                md = (await provider.get_market_data(o.stockx.product_id)).get(
                    o.variant.variant_id)
            except StockXAPIError as e:
                gone.append((o, f"StockX error: {e.message[:40]}"))
                continue
            if md is None or not md.highest_bid:
                gone.append((o, "no live bid"))
                continue
            # Same cost basis as the scanner: rebuild the stored Product with
            # the current price so discounts/extra costs flow through
            # landed_cost (audit 10.2 — report used price+extra only).
            basis = o.retail.model_copy(update={"price": cur["price"]}).landed_cost
            sn, _ = scenarios(basis, md, cfg.profit, cfg.vat)
            profit = sn.profit if sn else float("-inf")
            if profit < cfg.filters.min_profit_eur:
                gone.append((o, f"bid €{md.highest_bid:.0f} -> €{profit:+.2f}"))
                continue
            live.append((profit, o, cur["price"], md, size_ok))
    finally:
        await client.close()
        scratch.close()

    print(f"=== LIVE SELL-NOW OPPORTUNITIES: {len(live)} "
          f"(verified against live bids just now) ===")
    for profit, o, price, md, size_ok in sorted(live, key=lambda x: -x[0])[:args.limit]:
        if o.sizeless:
            flag = "no size (single variant)"
        else:
            flag = ("size in stock" if size_ok else
                    "SIZE NOT IN STOCK" if size_ok is False else
                    "size stock UNKNOWN")
        where = "—" if o.sizeless else f"EU {o.size_label}"
        print(f"\n  €{profit:+8.2f}  {o.stockx.title[:52]}")
        print(f"            {where} @ {o.retail.retailer} €{price:.2f}"
              f" | live bid €{md.highest_bid:.0f} | {flag}")
        print(f"            {o.retail.url}")
    if gone:
        print(f"\n=== no longer viable: {len(gone)} ===")
        for o, why in gone[:args.limit]:
            size = "—" if o.sizeless else f"EU {o.size_label}"
            print(f"  {o.stockx.title[:44]:46} {size:<9} {why}")
    open_reviews = ro.execute(
        "SELECT COUNT(*) FROM review_queue WHERE resolved=0").fetchone()[0]
    if open_reviews:
        print(f"\nreview queue: {open_reviews} open items")
    ro.close()
    return 0


def _print_stored(rows, ro, limit: int) -> None:
    import json as _json
    print("stored (HISTORICAL, not re-checked):")
    for r in rows[:limit]:
        try:
            p = _json.loads(r["payload_json"])
            sn = (p.get("sell_now") or {}).get("profit")
            size = f"EU {p['size_label']} " if p.get("size_label") else ""
            label = (f"{p['stockx'].get('title')} {size}@ "
                     f"{p['retail']['retailer']} €{p['retail']['price']:.0f}")
        except Exception:
            sn, label = None, r["key"]
        print(f"  sell-now {('€%.2f' % sn) if sn is not None else 'n/a':>10}  "
              f"{r['last_alerted'][:16]}  {label}")


async def _cmd_dashboard(args) -> int:
    import asyncio as _asyncio

    from .dashboard import run_dashboard
    cfg = load_config(args.config)
    # blocking server; run it off the event loop so Ctrl+C still lands
    await _asyncio.get_running_loop().run_in_executor(
        None, run_dashboard, cfg, args.host, args.port)
    return 0


async def _cmd_status(args) -> int:
    cfg = load_config(args.config)
    db = Database(cfg.db_file)
    c = db.conn
    n = lambda q: c.execute(q).fetchone()[0]  # noqa: E731
    print(f"db: {cfg.db_file}")
    print(f"retail products tracked : {n('SELECT COUNT(*) FROM retail_products')}")
    print(f"style codes resolved    : {n('SELECT COUNT(*) FROM stockx_products WHERE found=1')}")
    print(f"  negative (no match)   : {n('SELECT COUNT(*) FROM stockx_products WHERE found=0')}")
    print(f"market snapshots        : {n('SELECT COUNT(*) FROM market_snapshots')}")
    print(f"opportunities alerted   : {n('SELECT COUNT(*) FROM opportunities')}")
    print(f"review queue (open)     : {n('SELECT COUNT(*) FROM review_queue WHERE resolved=0')}")
    print(f"API requests last 24h   : {db.api_requests_last_24h()} / "
          f"{cfg.stockx.daily_request_budget}")
    rows = c.execute("SELECT * FROM scan_log ORDER BY started_at DESC LIMIT 5").fetchall()
    if rows:
        print("recent scans:")
        for r in rows:
            print(f"  {r['started_at'][:19]} {r['retailer']:8} "
                  f"seen={r['products_seen']} cand={r['candidates']} "
                  f"opp={r['opportunities']} alerts={r['alerts_sent']}")
    db.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="arb", description=__doc__)
    parser.add_argument("--config", default=None, help="path to config.yaml")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p_scan = sub.add_parser("scan", help="scan retailers for opportunities")
    p_scan.add_argument("--once", action="store_true",
                        help="single pass instead of the scheduler loop")
    p_scan.add_argument("--dry-run", action="store_true",
                        help="print alerts to console instead of Telegram")
    p_scan.add_argument("--retailer", default=None)
    p_scan.add_argument("--limit", type=int, default=None,
                        help="max candidates per scan (for testing)")
    p_scan.add_argument("--provider", choices=["official", "fixture"],
                        default="official")
    p_scan.add_argument("--backfill", action="store_true",
                        help="one-time full-catalog pass: resolve and price "
                             "EVERYTHING now (implies --once; ~1h for Ballzy, "
                             "resumes from cache if interrupted)")
    p_scan.set_defaults(func=_cmd_scan)

    p_auth = sub.add_parser("auth", help="one-time StockX OAuth login "
                                         "(fills STOCKX_REFRESH_TOKEN in .env)")
    p_auth.add_argument("--manual", action="store_true",
                        help="no local listener; paste the callback URL instead")
    p_auth.add_argument("--port", type=int, default=8123,
                        help="localhost callback port (default 8123)")
    p_auth.add_argument("--redirect-uri", default=None,
                        help="override the callback URL — must match the app's "
                             "registered Callback URI exactly (use with --manual "
                             "if it isn't a localhost URL)")
    p_auth.set_defaults(func=_cmd_auth)

    p_self = sub.add_parser("selftest", help="offline pipeline check")
    p_self.set_defaults(func=_cmd_selftest)

    p_res = sub.add_parser("resolve", help="resolve one style code (live)")
    p_res.add_argument("code")
    p_res.add_argument("--market", action="store_true",
                       help="also fetch market data")
    p_res.set_defaults(func=_cmd_resolve)

    p_status = sub.add_parser("status", help="DB / budget overview")
    p_status.set_defaults(func=_cmd_status)

    p_report = sub.add_parser(
        "report", help="what you can buy and flip right now (re-checks live bids)")
    p_report.add_argument("--limit", type=int, default=25)
    p_report.add_argument("--no-verify", action="store_true",
                          help="print stored history without re-checking StockX")
    p_report.set_defaults(func=_cmd_report)

    p_dash = sub.add_parser("dashboard", help="live web dashboard (read-only)")
    p_dash.add_argument("--host", default="127.0.0.1")
    p_dash.add_argument("--port", type=int, default=8787)
    p_dash.set_defaults(func=_cmd_dashboard)

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    return asyncio.run(args.func(args))
