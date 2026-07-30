"""Notifiers: Discord webhook (default) or Telegram for real alerts,
console for --dry-run."""
from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import Optional
from urllib.parse import quote_plus

import httpx

from .config import Secrets
from .matching import region_marker
from .models import Opportunity, ProfitBreakdown
from .profit import alias_breakeven_price

log = logging.getLogger(__name__)

TELEGRAM_MSG_LIMIT = 4096
DISCORD_MSG_LIMIT = 2000


def _fmt_scenario(b: ProfitBreakdown | None, tag: str) -> str:
    if b is None:
        return f"{tag}: no market side available"
    return (f"{tag}: sale €{b.sale_price:.0f} → payout €{b.payout:.2f} → "
            f"profit €{b.profit:+.2f} ({b.margin_pct * 100:+.0f}%)")


def _alias_line(o: Opportunity, cfg) -> Optional[str]:
    """Alias has no API, so give the human the exact number to beat and a link
    to check it in seconds."""
    if cfg is None or not cfg.alias.enabled:
        return None
    best = o.sell_now or o.list_ask
    if best is None:
        return None
    need = alias_breakeven_price(best.payout, cfg.alias)
    if need is None:
        return None
    query = quote_plus(o.retail.style_code or o.stockx.title or "")
    return (f"Alias: needs ≥ €{need:.0f} to beat StockX's €{best.payout:.2f} "
            f"payout — check: https://www.goat.com/search?query={query}")


def format_opportunity(o: Opportunity, reason: str, cfg=None) -> str:
    """Skimmable act-now message (plain text; Telegram-safe after escaping)."""
    m = o.market
    liq_bits = [
        f"bid €{m.highest_bid:.0f}" if m.highest_bid is not None else "bid —",
        f"ask €{m.lowest_ask:.0f}" if m.lowest_ask is not None else "ask —",
    ]
    if m.last_sale is not None:
        liq_bits.append(f"last €{m.last_sale:.0f}")
    if m.sales_72h is not None:
        liq_bits.append(f"{m.sales_72h} sold/72h")
    if m.num_bids is not None:
        liq_bits.append(f"{m.num_bids} bids")

    flag = "⚡ SELL NOW" if (o.sell_now and o.sell_now.profit > 0) else "📈"
    header = f"{flag} {o.stockx.title or o.retail.name}"
    if reason != "new":
        header += f"  [{reason}]"

    if o.sizeless:
        # single-variant product (Lego, cards, electronics, watches): there is
        # no size, so the only thing to state is the code and the condition
        # StockX will authenticate against
        size_line = (f"{o.retail.style_code or '?'} · one variant, no size"
                     + (f" · {o.retail.category}" if o.retail.category else "")
                     + (f" · {marker}" if (marker := region_marker(o.stockx.title))
                        else "")
                     + " · StockX authenticates these SEALED")
    elif o.retail.size_stock_unverified:
        # never assert a size we can't verify: the size named is only the best
        # live-bid candidate among the labels the retailer lists — whether THAT
        # size is in stock is unknown until the human checks (audit 0.1)
        size_line = (f"{o.retail.style_code or '?'} · best-bid candidate: "
                     f"EU {o.size_label} / US {o.us_size} — "
                     "⚠ IN-STOCK STATUS UNKNOWN, verify size on page")
    else:
        size_line = f"{o.retail.style_code or '?'} · size EU {o.size_label} / US {o.us_size}"
    lines = [
        header,
        size_line,
        f"Retail: {o.retail.retailer} €{o.retail.price:.2f}"
        + (f" −{o.retail.discount_pct*100:.0f}%" if o.retail.discount_pct else "")
        + (f" −{o.retail.sale_discount_pct*100:.0f}% sale promo"
           if (o.retail.on_sale and o.retail.sale_discount_pct) else "")
        + (f" +€{o.retail.extra_cost_eur:.2f} ship" if o.retail.extra_cost_eur else "")
        + (f" = €{o.landed_cost:.2f} to you"
           if (o.retail.discount_pct or o.retail.extra_cost_eur) else "")
        + ("" if o.retail.price_verified else " (grid price, verify)"),
    ]
    if o.prefer_note:
        lines.append(f"⭐ {o.prefer_note}")
    if o.retail.buy_note:
        lines.append(f"📦 {o.retail.buy_note}")
    if o.retail.stock_note:
        lines.append(f"⚠ {o.retail.stock_note}")
    lines += [
        _fmt_scenario(o.sell_now, "Sell now"),
        _fmt_scenario(o.list_ask, "List @ ask"),
        "Liquidity: " + " / ".join(liq_bits),
        f"Confidence {o.match_confidence:.2f}"
        + ("" if o.sizeless
           else " · size ✅ barcode-exact" if o.size_match_method == "barcode"
           else " · size via size chart")
        + f" · score {o.score:.0f}"
        + (f" · ~{o.est_days_to_clear}d to clear" if o.est_days_to_clear else ""),
        f"BUY: {o.retail.url}",
        f"StockX: {o.stockx.url}",
    ]
    if (alias := _alias_line(o, cfg)):
        lines.append(alias)
    return "\n".join(lines)


class Notifier(ABC):
    @abstractmethod
    async def send(self, text: str) -> bool:
        """Deliver the message. Returns True only if every chunk was
        delivered — a False return means the caller must NOT mark the
        alert as sent, so it survives to the next scan cycle."""

    async def close(self) -> None:
        pass


class ConsoleNotifier(Notifier):
    async def send(self, text: str) -> bool:
        print("\n" + "─" * 72)
        print(text)
        return True


def _retry_after_seconds(resp: httpx.Response) -> float:
    """429 backoff: prefer the JSON body's retry_after (Discord/Telegram both
    supply one), then the Retry-After header, then a conservative default."""
    try:
        body = resp.json()
        val = body.get("retry_after") or (
            body.get("parameters") or {}).get("retry_after")
        if val is not None:
            return min(float(val), 60.0)
    except Exception:
        pass
    header = resp.headers.get("retry-after")
    if header:
        try:
            return min(float(header), 60.0)
        except ValueError:
            pass
    return 5.0


class DiscordNotifier(Notifier):
    def __init__(self, secrets: Secrets):
        if not secrets.discord_configured:
            raise RuntimeError(
                "Discord not configured — set DISCORD_WEBHOOK_URL in .env, "
                "or run with --dry-run")
        self.webhook_url = secrets.discord_webhook_url
        self._http = httpx.AsyncClient(timeout=30)

    async def close(self) -> None:
        await self._http.aclose()

    async def send(self, text: str) -> bool:
        for chunk in _chunk(text, DISCORD_MSG_LIMIT):
            if not await self._post_chunk(chunk):
                return False
        return True

    async def _post_chunk(self, chunk: str, max_attempts: int = 3) -> bool:
        for attempt in range(max_attempts):
            try:
                resp = await self._http.post(self.webhook_url, json={
                    "content": chunk,
                    # never ping @everyone/@here even if the text contains it
                    "allowed_mentions": {"parse": []},
                })
            except httpx.HTTPError as e:
                log.error("Discord send failed (network): %s", e)
                return False
            if resp.status_code in (200, 204):
                return True
            if resp.status_code == 429 and attempt < max_attempts - 1:
                delay = _retry_after_seconds(resp)
                log.warning("Discord rate limited; retrying in %.1fs", delay)
                await asyncio.sleep(delay)
                continue
            log.error("Discord send failed: %s %s",
                      resp.status_code, resp.text[:300])
            return False
        return False


class TelegramNotifier(Notifier):
    def __init__(self, secrets: Secrets):
        if not secrets.telegram_configured:
            raise RuntimeError(
                "Telegram not configured — set TELEGRAM_BOT_TOKEN and "
                "TELEGRAM_CHAT_ID in .env, or run with --dry-run")
        self.chat_id = secrets.telegram_chat_id
        self._http = httpx.AsyncClient(
            base_url=f"https://api.telegram.org/bot{secrets.telegram_bot_token}",
            timeout=30)

    async def close(self) -> None:
        await self._http.aclose()

    async def send(self, text: str) -> bool:
        # plain text with explicit escaping; disable previews to keep it skimmable
        for chunk in _chunk(text, TELEGRAM_MSG_LIMIT):
            if not await self._post_chunk(chunk):
                return False
        return True

    async def _post_chunk(self, chunk: str, max_attempts: int = 3) -> bool:
        for attempt in range(max_attempts):
            try:
                resp = await self._http.post("/sendMessage", json={
                    "chat_id": self.chat_id,
                    "text": chunk,
                    "disable_web_page_preview": True,
                })
            except httpx.HTTPError as e:
                log.error("Telegram send failed (network): %s", e)
                return False
            if resp.status_code == 200:
                return True
            if resp.status_code == 429 and attempt < max_attempts - 1:
                delay = _retry_after_seconds(resp)
                log.warning("Telegram rate limited; retrying in %.1fs", delay)
                await asyncio.sleep(delay)
                continue
            log.error("Telegram send failed: %s %s",
                      resp.status_code, resp.text[:300])
            return False
        return False


def _chunk(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks, current = [], ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > limit:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return chunks
