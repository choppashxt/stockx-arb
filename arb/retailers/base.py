"""Shared scraper interface + a polite HTTP fetcher.

Anti-block hygiene (deliberate, keep it):
- robots.txt is fetched, parsed, and obeyed; a robots.txt that answers
  401/403 counts as disallow-everything for that host.
- per-domain delay = max(config delay, robots Crawl-delay) with jitter.
- honest User-Agent with contact address; no fingerprinting tricks.
- 403 is a final no for the run; 429 backs off once then gives up politely.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
import urllib.robotparser
from abc import ABC, abstractmethod
from typing import Optional, TYPE_CHECKING
from urllib.parse import urlparse

import httpx

from ..config import RetailerConfig
from ..models import Product

if TYPE_CHECKING:
    from ..db import Database

log = logging.getLogger(__name__)

USER_AGENT = ("sneaker-arb-scanner/0.1 (personal hobby price comparison; "
              "contact: kaarmamarkus@gmail.com)")

# Five of our retailers (weekend, reede, ballzy, sportland, teamsport) sit
# behind Cloudflare, which scores bot reputation PER IP ACROSS ITS WHOLE
# NETWORK. Per-host politeness is therefore not sufficient: each scraper paced
# itself correctly while the machine as a whole fired 4+ requests at once
# (run_loop gathers every retailer, and some crawl categories in parallel).
# Cloudflare read that aggregate burst as a bot and challenged the strictest
# sites — weekend.ee and reede.ee 403'd on the FIRST request of every scan and
# contributed nothing for a day, while working perfectly standalone.
#
# This caps simultaneous outbound requests for the entire process. It is
# deliberately low: the work is IO-bound and already delay-paced, so the cost
# is a slightly longer cycle, and the benefit is not looking like a bot.
MAX_CONCURRENT_REQUESTS = 2
_REQUEST_SLOT = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)


class PoliteFetcher:
    # stop pestering a host that is refusing us: after this many consecutive
    # failed responses the host is dropped for the rest of the run
    MAX_CONSECUTIVE_FAILURES = 5

    def __init__(self, base_delay_s: float):
        self.base_delay_s = base_delay_s
        self._http = httpx.AsyncClient(
            timeout=30, follow_redirects=True, headers={"User-Agent": USER_AGENT})
        self._robots: dict[str, Optional[urllib.robotparser.RobotFileParser]] = {}
        self._last_request_at: dict[str, float] = {}
        self._consecutive_failures: dict[str, int] = {}
        self._given_up: set[str] = set()

    def _note_failure(self, host: str, why: str) -> None:
        n = self._consecutive_failures.get(host, 0) + 1
        self._consecutive_failures[host] = n
        if n >= self.MAX_CONSECUTIVE_FAILURES and host not in self._given_up:
            self._given_up.add(host)
            log.warning("%s failed %d requests in a row (%s) — giving up on it "
                        "for this run", host, n, why)

    def _note_success(self, host: str) -> None:
        self._consecutive_failures[host] = 0

    async def close(self) -> None:
        await self._http.aclose()

    async def _robots_for(self, host: str
                          ) -> Optional[urllib.robotparser.RobotFileParser]:
        """RobotFileParser for host; None means 'everything disallowed'."""
        if host in self._robots:
            return self._robots[host]
        rp = urllib.robotparser.RobotFileParser()
        try:
            async with _REQUEST_SLOT:
                resp = await self._http.get(f"https://{host}/robots.txt")
            if resp.status_code in (401, 403):
                log.warning("%s robots.txt answered %s — treating as disallow-all",
                            host, resp.status_code)
                self._robots[host] = None
                return None
            if resp.status_code >= 400:      # 404 etc.: nothing is disallowed
                rp.parse([])
            else:
                rp.parse(resp.text.splitlines())
        except httpx.HTTPError as e:
            log.warning("%s robots.txt unreachable (%s) — treating as disallow-all",
                        host, e)
            self._robots[host] = None
            return None
        self._robots[host] = rp
        return rp

    async def _wait_turn(self, host: str, rp) -> None:
        delay = self.base_delay_s
        if rp is not None:
            crawl_delay = rp.crawl_delay(USER_AGENT) or rp.crawl_delay("*")
            if crawl_delay:
                delay = max(delay, float(crawl_delay))
        delay *= 1 + random.random() * 0.3      # jitter, always slower not faster
        loop = asyncio.get_running_loop()
        wait = delay - (loop.time() - self._last_request_at.get(host, 0.0))
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_request_at[host] = loop.time()

    async def get(self, url: str) -> Optional[str]:
        """Fetch a page politely. None on any refusal — caller skips, never retries
        harder."""
        host = urlparse(url).netloc
        if host in self._given_up:
            return None
        rp = await self._robots_for(host)
        if rp is None:
            return None
        path = url[url.find(host) + len(host):] or "/"
        if not rp.can_fetch(USER_AGENT, url):
            log.info("robots.txt disallows %s — skipping", path)
            return None
        await self._wait_turn(host, rp)
        try:
            async with _REQUEST_SLOT:
                resp = await self._http.get(url)
        except httpx.HTTPError as e:
            log.warning("request failed for %s: %s", url, e)
            self._note_failure(host, str(e)[:60])
            return None
        if resp.status_code == 403:
            log.warning("%s returned 403 — not retrying this run", host)
            self._given_up.add(host)
            return None
        if resp.status_code == 429:
            log.warning("%s rate-limited us (429) — backing off 60s once", host)
            await asyncio.sleep(60)
            async with _REQUEST_SLOT:
                resp = await self._http.get(url)
            if resp.status_code != 200:
                self._note_failure(host, "429")
                return None
        if resp.status_code != 200:
            log.info("%s returned HTTP %s", url, resp.status_code)
            self._note_failure(host, f"HTTP {resp.status_code}")
            return None
        self._note_success(host)
        return resp.text


class RetailerScraper(ABC):
    """One instance per retailer per scan cycle."""

    name: str = "unnamed"

    def __init__(self, cfg: RetailerConfig, db: "Optional[Database]" = None):
        self.cfg = cfg
        self.db = db          # for sitemap caches / rotation state; may be None
        self.fetcher = PoliteFetcher(cfg.request_delay_seconds)

    # -- sitemap helpers (for retailers whose catalog comes from sitemaps) ----
    async def cached_sitemap_slugs(self, sitemap_urls: list[str],
                                   loc_pattern: str,
                                   max_sitemaps: int = 20) -> list[str]:
        """Product slugs from sitemaps, cached in SQLite for
        cfg.sitemap_refresh_hours so big XML files aren't refetched per scan.
        Capped: some stores paginate into dozens of sitemap files."""
        key = f"sitemap:{self.name}"
        if self.db is not None and (raw := self.db.kv_get(key)):
            data = json.loads(raw)
            if time.time() - data["fetched_at"] < self.cfg.sitemap_refresh_hours * 3600:
                return data["slugs"]
        slugs: list[str] = []
        for url in sitemap_urls[:max_sitemaps]:
            xml = await self.fetcher.get(url)
            if not xml:
                continue
            for loc in re.findall(r"<loc>([^<]+)</loc>", xml):
                m = re.search(loc_pattern, loc)
                if m:
                    slugs.append(m.group(1))
        slugs = list(dict.fromkeys(slugs))
        if slugs and self.db is not None:
            self.db.kv_set(key, json.dumps(
                {"fetched_at": time.time(), "slugs": slugs}))
        return slugs

    def rotation_slice(self, slugs: list[str], per_scan: int) -> list[str]:
        """Rotating window over the backlog so the whole catalog gets revisited
        across scans without hammering it in one go."""
        if not slugs or per_scan <= 0:
            return []
        # Never ask for more than exists: with per_scan=100 over an 8-item list
        # the wrap-around below returned all 8 twice, doubling page fetches for
        # every small catalog.
        per_scan = min(per_scan, len(slugs))
        offset = 0
        key = f"rotation:{self.name}"
        if self.db is not None and (raw := self.db.kv_get(key)):
            offset = int(raw) % len(slugs)
        window = (slugs[offset:offset + per_scan]
                  + slugs[:max(0, offset + per_scan - len(slugs))])
        if self.db is not None:
            self.db.kv_set(key, str((offset + per_scan) % len(slugs)))
        return window

    def slug_wanted(self, slug: str) -> bool:
        if not self.cfg.slug_filters:
            return True
        return any(f in slug for f in self.cfg.slug_filters)

    async def close(self) -> None:
        await self.fetcher.close()

    @abstractmethod
    async def scan(self) -> list[Product]:
        """Sweep the catalog (category grids). Cheap per product; prices may be
        grid-level estimates (price_verified=False)."""

    @abstractmethod
    async def enrich(self, product: Product) -> Optional[Product]:
        """Fetch the product page for an alert candidate: authoritative price,
        per-size stock, size mappings. None if the page refuses or the product
        is gone. Sets price_verified=True on success."""
