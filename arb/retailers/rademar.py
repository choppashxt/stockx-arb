"""Rademar (rademar.ee) — Tier 1, Estonia. React SPA storefront over an open
JSON API at api.rademar.ee.

Verified 2026-07-27:
- api.rademar.ee/robots.txt allows everything except list-filter params
  (page=, brands=, sizes=, ...) and advertises product + category sitemaps.
- GET /api/products/{slug}?variations=true returns the full product: title,
  product_code (the manufacturer style code, e.g. M9160C), brands[],
  categories[] (meeste-/naiste- prefix = gender), prices[] with types
  default / client_extra (loyalty, ignore) / sale, and variations[] with the
  EU size in `value`, an `in_stock` flag, and the EAN barcode per size.
- GET /api/products/{slug}/quantities gives per-shop stock counts per
  variation — used at alert time to confirm the size is really available.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from ..models import Product, RetailSize
from .base import RetailerScraper

log = logging.getLogger(__name__)

API = "https://api.rademar.ee"
SITE = "https://www.rademar.ee"
SITEMAPS = [f"{API}/google/product_sitemap.xml"]
_SLUG_RE = r"https://www\.rademar\.ee/toode/([a-z0-9\-]{3,80})$"


def _guest_price(prices: list[dict]) -> Optional[float]:
    """Lowest price a walk-in customer actually pays: sale beats default;
    client_extra is loyalty-card pricing and is ignored."""
    values = [p.get("value") for p in prices
              if p.get("type") in ("default", "sale") and p.get("value")]
    return min(values) if values else None


class RademarScraper(RetailerScraper):
    name = "rademar"

    async def scan(self) -> list[Product]:
        backlog = await self.cached_sitemap_slugs(SITEMAPS, _SLUG_RE)
        backlog = [s for s in backlog if self.slug_wanted(s)]
        rotation = self.rotation_slice(backlog, self.cfg.sitemap_slice_per_scan)
        products = []
        for slug in rotation:
            product = await self._fetch_product(slug)
            if product is not None:
                products.append(product)
        log.info("rademar: %d/%d backlog slugs this scan -> %d products",
                 len(rotation), len(backlog), len(products))
        return products

    async def _fetch_product(self, slug: str) -> Optional[Product]:
        raw = await self.fetcher.get(
            f"{API}/api/products/{slug}?variations=true&lang=et")
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict) or data.get("hidden") or data.get("archived_at"):
            return None

        price = _guest_price(data.get("prices") or [])
        if price is None or price <= 0:
            return None

        style = (data.get("product_code") or "").strip().upper() or None
        brands = data.get("brands") or []
        categories = [c.get("slug", "") for c in data.get("categories") or []]

        sizes = []
        for v in data.get("variations") or []:
            if v.get("type") != "size" or not v.get("enabled"):
                continue
            sizes.append(RetailSize(
                label=str(v.get("value") or "").strip(),
                system="EU",               # rademar lists EU sizes
                ean=str(v.get("ean") or "").strip() or None,
                in_stock=bool(v.get("in_stock")),
            ))

        return Product(
            retailer=self.name,
            name=(data.get("title") or slug).strip(),
            brand=brands[0]["title"] if brands else None,
            style_code=style,
            colorway=data.get("colour_name"),
            url=f"{SITE}/toode/{slug}",
            price=round(float(price), 2),
            currency="EUR",
            sizes=sizes,
            in_stock=bool(data.get("in_stock")) and any(s.in_stock for s in sizes),
            pickup_available=True,          # physical shops in Tallinn/Tartu
            price_verified=False,           # enrich() re-checks + quantities
        )

    async def enrich(self, product: Product) -> Optional[Product]:
        """Re-fetch the product and require real quantity > 0 for each size —
        the in_stock flag alone can lag the per-shop counts."""
        slug = product.url.rsplit("/", 1)[-1]
        fresh = await self._fetch_product(slug)
        if fresh is None:
            return None
        raw = await self.fetcher.get(f"{API}/api/products/{slug}/quantities?lang=et")
        if raw:
            try:
                rows = json.loads(raw)
                qty_by_size: dict[str, int] = {}
                for row in rows:
                    var = row.get("variation") or {}
                    label = str(var.get("value") or "").strip()
                    qty_by_size[label] = qty_by_size.get(label, 0) + int(row.get("quantity") or 0)
                for s in fresh.sizes:
                    if s.label in qty_by_size:
                        s.in_stock = s.in_stock and qty_by_size[s.label] > 0
                fresh.in_stock = any(s.in_stock for s in fresh.sizes)
            except (json.JSONDecodeError, TypeError, ValueError):
                log.info("rademar: quantities unparseable for %s — keeping flags", slug)
        fresh.price_verified = True
        return fresh
