"""OAuth2 access-token management for the StockX API.

The one-time authorization-code dance is done by the human (the refresh token
in .env is its result). Here we only exchange refresh -> access tokens and
cache the access token in SQLite until shortly before it expires.
"""
from __future__ import annotations

import base64
import json
import logging
import time

import httpx

from ..config import Secrets
from ..db import Database

log = logging.getLogger(__name__)

TOKEN_URL = "https://accounts.stockx.com/oauth/token"
AUDIENCE = "gateway.stockx.com"
_KV_KEY = "stockx_access_token"
_EXPIRY_MARGIN_S = 300


class AuthError(RuntimeError):
    pass


def _jwt_exp(token: str) -> float:
    """Expiry from the JWT payload; falls back to now+11h if unparseable."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return float(json.loads(base64.urlsafe_b64decode(payload))["exp"])
    except Exception:
        return time.time() + 11 * 3600


class TokenManager:
    def __init__(self, secrets: Secrets, db: Database):
        self.secrets = secrets
        self.db = db

    async def get_access_token(self, force_refresh: bool = False) -> str:
        if not force_refresh:
            cached = self.db.kv_get(_KV_KEY)
            if cached:
                data = json.loads(cached)
                if data["exp"] - _EXPIRY_MARGIN_S > time.time():
                    return data["token"]
        return await self._refresh()

    async def _refresh(self) -> str:
        if not self.secrets.stockx_configured:
            raise AuthError(
                "StockX credentials missing — fill in STOCKX_* in .env "
                "(see .env.example)")
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(TOKEN_URL, data={
                "grant_type": "refresh_token",
                "client_id": self.secrets.stockx_client_id,
                "client_secret": self.secrets.stockx_client_secret,
                "refresh_token": self.secrets.stockx_refresh_token,
                "audience": AUDIENCE,
            })
        if resp.status_code != 200:
            raise AuthError(
                f"token refresh failed: HTTP {resp.status_code} — {resp.text[:300]}")
        token = resp.json().get("access_token")
        if not token:
            raise AuthError(f"token refresh returned no access_token: {resp.text[:300]}")
        self.db.kv_set(_KV_KEY, json.dumps({"token": token, "exp": _jwt_exp(token)}))
        log.info("refreshed StockX access token")
        return token
