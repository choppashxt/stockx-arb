"""Notifier delivery-semantics regressions (audit 9.1 — THE critical one):
a failed send must return False so the caller never records the alert as
sent; a False-return alert survives to the next scan. Also covers chunking
and the audit-0.1 unverified-size wording."""
import asyncio

import httpx

from arb.models import (
    MarketData,
    Opportunity,
    Product,
    ProfitBreakdown,
    RetailSize,
    StockXProduct,
    StockXVariant,
)
from arb.notify import (
    DISCORD_MSG_LIMIT,
    ConsoleNotifier,
    DiscordNotifier,
    _chunk,
    format_opportunity,
)


def _discord_with_transport(handler) -> DiscordNotifier:
    """Build a DiscordNotifier without touching Secrets/.env, with a mocked
    HTTP transport."""
    n = object.__new__(DiscordNotifier)
    n.webhook_url = "https://discord.test/webhook"
    n._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return n


class TestDeliverySemantics:
    def test_204_returns_true(self):
        n = _discord_with_transport(lambda req: httpx.Response(204))
        assert asyncio.run(n.send("hello")) is True

    def test_500_returns_false(self):
        # pre-fix behavior: logged and returned success-shaped, so
        # record_alert fired and the alert was suppressed forever
        n = _discord_with_transport(lambda req: httpx.Response(500))
        assert asyncio.run(n.send("hello")) is False

    def test_404_deleted_webhook_returns_false(self):
        n = _discord_with_transport(lambda req: httpx.Response(404))
        assert asyncio.run(n.send("hello")) is False

    def test_network_error_returns_false(self):
        def boom(req):
            raise httpx.ConnectError("nope")
        n = _discord_with_transport(boom)
        assert asyncio.run(n.send("hello")) is False

    def test_429_retries_then_succeeds(self):
        calls = []

        def handler(req):
            calls.append(1)
            if len(calls) == 1:
                return httpx.Response(429, json={"retry_after": 0.01})
            return httpx.Response(204)

        n = _discord_with_transport(handler)
        assert asyncio.run(n.send("hello")) is True
        assert len(calls) == 2

    def test_console_always_true(self, capsys):
        assert asyncio.run(ConsoleNotifier().send("hi")) is True


class TestChunking:
    def test_short_text_single_chunk(self):
        assert _chunk("hello", DISCORD_MSG_LIMIT) == ["hello"]

    def test_long_text_splits_under_limit(self):
        text = "\n".join(f"line {i} " + "x" * 80 for i in range(60))
        chunks = _chunk(text, DISCORD_MSG_LIMIT)
        assert len(chunks) > 1
        assert all(len(c) <= DISCORD_MSG_LIMIT for c in chunks)


def _opp(size_stock_unverified: bool) -> Opportunity:
    retail = Product(retailer="testshop", name="Test Shoe", url="https://x/y",
                     price=100.0, style_code="HQ2010-005",
                     size_stock_unverified=size_stock_unverified,
                     sizes=[RetailSize(label="42",
                                       in_stock=None if size_stock_unverified
                                       else True)])
    sn = ProfitBreakdown(scenario="sell_now", sale_price=150.0,
                         transaction_fee=15.0, processing_fee=4.5,
                         shipping=7.0, vat_wedge=0.0, payout=123.5,
                         profit=23.5, margin_pct=0.235, roic=0.22)
    return Opportunity(
        retail=retail, size_label="42", us_size="8.5",
        stockx=StockXProduct(product_id="p1", title="Test Shoe"),
        variant=StockXVariant(product_id="p1", variant_id="v1", size="8.5"),
        market=MarketData(variant_id="v1", highest_bid=150.0,
                          lowest_ask=170.0),
        sell_now=sn, match_confidence=1.0,
    )


class TestUnverifiedSizeWording:
    def test_unverified_alert_never_asserts_the_size_exists(self):
        # audit 0.1: 5 of 7 real alerts named a size with ~9% chance of
        # existing, while claiming it in stock
        text = format_opportunity(_opp(True), "new")
        assert "IN-STOCK STATUS UNKNOWN" in text
        assert "best-bid candidate" in text

    def test_verified_alert_states_the_size_plainly(self):
        text = format_opportunity(_opp(False), "new")
        assert "size EU 42" in text
        assert "UNKNOWN" not in text
