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
import sys

try:
    import truststore
    truststore.inject_into_ssl()      # this machine TLS-intercepts; trust OS store
except ImportError:
    pass

from .config import AppConfig, Secrets, load_config
from .db import Database
from .notify import ConsoleNotifier, DiscordNotifier, Notifier, TelegramNotifier

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
    # size 45 out of stock (skipped); Campus below min profit
    if len(all_opps) != 1:
        failures.append(f"expected exactly 1 opportunity, got {len(all_opps)}")
    else:
        opp = all_opps[0]
        if opp.size_label != "42":
            failures.append(f"wrong size alerted: {opp.size_label}")
        # sell_now: 145 - 13.775 - 4.35 - 7 = 119.875 -> profit 40.875
        expected_profit = 145 - 145 * 0.095 - 145 * 0.03 - 7 - 79
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
    """Live opportunities snapshot, ranked. (Phase 6 CLI report.)"""
    import json as _json
    cfg = load_config(args.config)
    db = Database(cfg.db_file)
    rows = db.conn.execute(
        "SELECT key, last_alerted, last_profit, was_in_stock, payload_json "
        "FROM opportunities ORDER BY last_profit DESC LIMIT ?",
        (args.limit,)).fetchall()
    if not rows:
        print("no opportunities recorded yet")
        db.close()
        return 0
    print(f"{'profit':>8}  {'stock':5} {'last alerted':19}  opportunity")
    for r in rows:
        try:
            p = _json.loads(r["payload_json"])
            label = (f"{p['stockx'].get('title') or p['retail']['name']} "
                     f"EU {p['size_label']} @ {p['retail']['retailer']} "
                     f"€{p['retail']['price']:.0f} -> {p['retail']['url']}")
        except Exception:
            label = r["key"]
        stock = "yes" if r["was_in_stock"] else "GONE"
        print(f"€{r['last_profit']:>7.2f}  {stock:5} {r['last_alerted'][:19]}  {label}")
    open_reviews = db.conn.execute(
        "SELECT COUNT(*) FROM review_queue WHERE resolved=0").fetchone()[0]
    if open_reviews:
        print(f"\nreview queue: {open_reviews} open items "
              f"(SELECT * FROM review_queue WHERE resolved=0)")
    db.close()
    return 0


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

    p_report = sub.add_parser("report", help="current live opportunities, ranked")
    p_report.add_argument("--limit", type=int, default=25)
    p_report.set_defaults(func=_cmd_report)

    p_dash = sub.add_parser("dashboard", help="live web dashboard (read-only)")
    p_dash.add_argument("--host", default="127.0.0.1")
    p_dash.add_argument("--port", type=int, default=8787)
    p_dash.set_defaults(func=_cmd_dashboard)

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    return asyncio.run(args.func(args))
