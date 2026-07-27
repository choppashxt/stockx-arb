"""Low-level StockX API client: auth headers, 1 req/s throttle, daily budget,
exponential backoff on 429/5xx, one automatic token refresh on 401."""
from __future__ import annotations

import asyncio
import logging
import random
from typing import Any, Optional

import httpx

from ..config import StockXConfig
from ..db import Database
from .auth import TokenManager

log = logging.getLogger(__name__)

BASE_URL = "https://api.stockx.com/v2"
MAX_RETRIES = 4
# transient: 429 rate limit, 408 request timeout (StockX returns this when its
# own upstream is slow) — both deserve a backoff-and-retry, not a hard failure
_RETRYABLE_4XX = {408, 429}


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
        self._last_request_at = 0.0

    async def close(self) -> None:
        await self._http.aclose()

    async def _throttle(self) -> None:
        loop = asyncio.get_running_loop()
        wait = self.cfg.min_request_interval_s - (loop.time() - self._last_request_at)
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_request_at = loop.time()

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
                    token = await self.tokens.get_access_token(force_refresh=True)
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
                    retry_after = resp.headers.get("Retry-After")
                    delay = float(retry_after) if retry_after and retry_after.isdigit() \
                        else backoff * (1 + random.random() * 0.25)
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

    async def get_product_market_data(self, product_id: str,
                                      currency: str) -> list[dict]:
        """All variants of a product in ONE request — the budget-friendly call."""
        return await self.get(f"/catalog/products/{product_id}/market-data",
                              {"currencyCode": currency}) or []
