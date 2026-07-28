"""Low-level StockX API client: auth headers, 1 req/s throttle, daily budget,
exponential backoff on 429/5xx, one automatic token refresh on 401."""
from __future__ import annotations

import asyncio
import logging
import os
import random
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Optional

import httpx

from ..config import PROJECT_ROOT, StockXConfig
from ..db import Database
from .auth import TokenManager

log = logging.getLogger(__name__)

BASE_URL = "https://api.stockx.com/v2"
MAX_RETRIES = 4
# transient: 429 rate limit, 408 request timeout (StockX returns this when its
# own upstream is slow) — both deserve a backoff-and-retry, not a hard failure
_RETRYABLE_4XX = {408, 429}
# Never sleep longer than this on a server-supplied Retry-After (a corrupt or
# hostile header must not park the scanner for hours).
RETRY_AFTER_CAP_S = 300.0


def _retry_after_seconds(value: Optional[str]) -> Optional[float]:
    """Parse a Retry-After header. RFC 9110 allows delay-seconds OR an
    HTTP-date; return seconds from now, or None if absent/unparseable."""
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt - datetime.now(timezone.utc)).total_seconds()


if os.name == "nt":
    import msvcrt

    def _lock_file(f) -> None:
        # msvcrt LK_LOCK gives up after ~10 s; spin on the non-blocking form
        while True:
            try:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                time.sleep(0.05)

    def _unlock_file(f) -> None:
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _lock_file(f) -> None:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)

    def _unlock_file(f) -> None:
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)


class CrossProcessThrottle:
    """StockX's 1 req/s limit is per API key, not per process — `arb report`
    or `arb resolve` running beside the 24/7 scanner used to double the rate
    (audit 1.1). All processes share a lease file: take an exclusive lock,
    sleep out the remainder of the interval while HOLDING the lock (so other
    processes queue behind us), stamp the wall clock, release."""

    def __init__(self, path: str, interval_s: float):
        self.path = path
        self.interval_s = interval_s

    def _wait_turn_blocking(self) -> None:
        with open(self.path, "a+b") as f:
            _lock_file(f)
            try:
                f.seek(0)
                raw = f.read(64)
                try:
                    last = float(raw)
                except ValueError:
                    last = 0.0
                wait = self.interval_s - (time.time() - last)
                if wait > 0:
                    time.sleep(wait)
                f.seek(0)
                f.truncate()
                f.write(f"{time.time():.6f}".encode())
                f.flush()
            finally:
                _unlock_file(f)

    async def wait_turn(self) -> None:
        # file locking + sleep happen in a worker thread so the event loop
        # (7 other retailer coroutines) keeps running
        await asyncio.to_thread(self._wait_turn_blocking)


class BudgetExhausted(RuntimeError):
    pass


class StockXAPIError(RuntimeError):
    """A 4xx StockX rejected with an explanation — carries their message."""

    def __init__(self, status: int, message: str, path: str):
        super().__init__(f"HTTP {status} on {path}: {message}")
        self.status = status
        self.message = message


class AccountNotReady(StockXAPIError):
    """StockX refuses market data until the account is set up for selling.
    Every call will fail identically, so callers should stop rather than retry."""


# StockX returns this for every market-data call until billing/shipping exist
_ACCOUNT_SETUP_HINT = "billing and shipping"


def _error_message(resp: httpx.Response) -> str:
    try:
        return resp.json().get("errorMessage") or resp.text[:200]
    except Exception:
        return resp.text[:200]


class StockXClient:
    def __init__(self, tokens: TokenManager, db: Database, cfg: StockXConfig):
        self.tokens = tokens
        self.db = db
        self.cfg = cfg
        self._http = httpx.AsyncClient(base_url=BASE_URL, timeout=30)
        self._lock = asyncio.Lock()
        # machine-global pacing lease, shared with every other arb process
        self._throttle_lease = CrossProcessThrottle(
            str(PROJECT_ROOT / ".stockx_ratelimit"), cfg.min_request_interval_s)

    async def close(self) -> None:
        await self._http.aclose()

    async def _throttle(self) -> None:
        await self._throttle_lease.wait_turn()

    async def get(self, path: str, params: Optional[dict] = None) -> Any:
        """GET with throttle/budget/retry. Returns parsed JSON.
        Raises BudgetExhausted when the self-imposed daily cap is reached."""
        if self.db.api_requests_last_24h() >= self.cfg.daily_request_budget:
            raise BudgetExhausted(
                f"daily StockX request budget ({self.cfg.daily_request_budget}) reached")

        token = await self.tokens.get_access_token()
        refreshed = False
        backoff = 2.0
        async with self._lock:      # serialize: the rate limit is per client
            for attempt in range(MAX_RETRIES + 1):
                await self._throttle()
                self.db.record_api_request()
                resp = await self._http.get(path, params=params, headers={
                    "Authorization": f"Bearer {token}",
                    "x-api-key": self.tokens.secrets.stockx_api_key,
                    "Accept": "application/json",
                })
                if resp.status_code == 200:
                    return resp.json()
                if resp.status_code == 404:
                    return None
                if resp.status_code == 401 and not refreshed:
                    log.info("401 from StockX — refreshing access token")
                    token = await self.tokens.get_access_token(
                        force_refresh=True, stale_token=token)
                    refreshed = True
                    continue
                if (400 <= resp.status_code < 500
                        and resp.status_code not in _RETRYABLE_4XX):
                    message = _error_message(resp)
                    if _ACCOUNT_SETUP_HINT in message.lower():
                        raise AccountNotReady(resp.status_code, message, path)
                    raise StockXAPIError(resp.status_code, message, path)
                if resp.status_code in _RETRYABLE_4XX or resp.status_code >= 500:
                    if attempt == MAX_RETRIES:
                        break
                    # server's Retry-After (seconds OR HTTP-date) is a FLOOR:
                    # never retry sooner than asked, capped so a bad header
                    # can't stall us forever (audit 1.3)
                    server_floor = _retry_after_seconds(
                        resp.headers.get("Retry-After"))
                    delay = backoff * (1 + random.random() * 0.25)
                    if server_floor is not None:
                        delay = max(delay, server_floor)
                    delay = min(delay, RETRY_AFTER_CAP_S)
                    log.warning("StockX %s on %s — backing off %.1fs",
                                resp.status_code, path, delay)
                    await asyncio.sleep(delay)
                    backoff *= 2
                    continue
                resp.raise_for_status()
        raise httpx.HTTPStatusError(
            f"giving up on {path} after {MAX_RETRIES} retries (last {resp.status_code})",
            request=resp.request, response=resp)

    # -- typed endpoint wrappers --------------------------------------------
    async def search(self, query: str, page_number: int = 1,
                     page_size: int = 20) -> dict:
        return await self.get("/catalog/search", {
            "query": query, "pageNumber": page_number, "pageSize": page_size}) or {}

    async def get_product(self, product_id: str) -> Optional[dict]:
        return await self.get(f"/catalog/products/{product_id}")

    async def get_variants(self, product_id: str) -> list[dict]:
        return await self.get(f"/catalog/products/{product_id}/variants") or []

    async def get_variant_by_gtin(self, gtin: str) -> Optional[dict]:
        """Barcode -> the exact product+size variant. 404 (unknown barcode)
        comes back as None from .get()."""
        return await self.get(f"/catalog/products/variants/gtins/{gtin}")

    async def get_product_market_data(self, product_id: str,
                                      currency: str) -> list[dict]:
        """All variants of a product in ONE request — the budget-friendly call."""
        return await self.get(f"/catalog/products/{product_id}/market-data",
                              {"currencyCode": currency}) or []
