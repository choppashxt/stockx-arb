"""Reede (reede.ee) — Tier 1, Estonia. Sneaker/streetwear boutique on Magento.

Verified 2026-07-27:
- robots.txt allows category and product pages but disallows ?p= pagination
  and /catalogsearch/ — so discovery is category page 1 (fresh stock, incl.
  /soodus sale) plus a rotating slice of the advertised product sitemaps.
- Product pages carry: data-price-amount with data-price-type
  oldPrice/finalPrice; the manufacturer style code as plain text (e.g.
  IF4396-103); and a Magento jsonConfig size attribute whose options give the
  size label, per-size availability (non-empty products[]), the brand, gender,
  and default_size_type (US for Jordan etc.) — retailer-authoritative sizing.
- Every scanned product is fetched at page level, so records leave here
  price_verified=True and the pipeline skips its own enrich round-trip.
"""
from __future__ import annotations

import html as html_mod
import json
import logging
import re
from typing import Optional

from ..models import Product, RetailSize
from .base import RetailerScraper

log = logging.getLogger(__name__)

BASE = "https://reede.ee"
SITEMAPS = [f"{BASE}/pub/media/google_sitemap_3.xml",
            f"{BASE}/pub/media/google_sitemap_4.xml"]
_SLUG_RE = r"https://reede\.ee/([a-z0-9\-]{3,80})$"
_NON_PRODUCT = {"aksessuaarid", "eesti-disain", "kkk", "kohaletoimetamine",
                "mehed", "naised", "ostutingimused", "pretensioonid",
                "privaatsustingimused-reede", "soodus", "tagastamine",
                "checkout", "customer", "brands", "info", "kontakt"}

_PRICE_RE = re.compile(
    r'data-price-amount="([\d.]+)"[^>]*data-price-type="finalPrice"|'
    r'data-price-type="finalPrice"[^>]*data-price-amount="([\d.]+)"')
# separator may not eat alphanumerics — 'Mudel: IF4396-103' must keep the 'IF'
_STYLE_RE = re.compile(
    r"(?:Tootekood|Mudel|Style|Artikkel)"
    r"[^A-Z0-9]{0,30}([A-Z]{0,4}\d{3,6}[A-Z]{0,4}(?:[- ]?\d{2,3})?)")
_JSONCFG_RE = re.compile(r'"jsonConfig":\s*(\{.*?\})\s*[,}]\s*"[a-z]', re.S)


class ReedeScraper(RetailerScraper):
    name = "reede"

    async def scan(self) -> list[Product]:
        slugs: list[str] = []
        # fresh stock first: category page 1 of each configured listing
        for category in self.cfg.categories:
            html = await self.fetcher.get(f"{BASE}{category}")
            if not html:
                continue
            for m in re.finditer(r'href="https://reede\.ee/([a-z0-9\-]{8,80})"', html):
                slug = m.group(1)
                if slug not in _NON_PRODUCT:
                    slugs.append(slug)
        fresh = list(dict.fromkeys(slugs))

        backlog = await self.cached_sitemap_slugs(SITEMAPS, _SLUG_RE)
        backlog = [s for s in backlog if s not in _NON_PRODUCT and self.slug_wanted(s)]
        rotation = self.rotation_slice(backlog, self.cfg.sitemap_slice_per_scan)

        products = []
        seen: set[str] = set()
        for slug in fresh + rotation:
            if slug in seen:
                continue
            seen.add(slug)
            product = await self._fetch_product(slug)
            if product is not None:
                products.append(product)
        log.info("reede: %d fresh + %d rotation slugs -> %d products",
                 len(fresh), len(rotation), len(products))
        return products

    async def enrich(self, product: Product) -> Optional[Product]:
        # scan() already reads the product page; refresh it for confirmation
        slug = product.url.rsplit("/", 1)[-1]
        return await self._fetch_product(slug)

    async def _fetch_product(self, slug: str) -> Optional[Product]:
        html = await self.fetcher.get(f"{BASE}/{slug}")
        if not html:
            return None

        m = _PRICE_RE.search(html)
        price = float(m.group(1) or m.group(2)) if m else None
        if price is None or price <= 0:
            return None                      # not a purchasable product page

        style = None
        if (sm := _STYLE_RE.search(html)):
            style = sm.group(1).replace(" ", "-").upper()

        name, brand, gender, sizes = self._parse_size_config(html)
        title = re.search(r"<title>([^<|]{3,80})", html)
        display_name = (name or (title.group(1).strip() if title else slug))
        display_name = html_mod.unescape(html_mod.unescape(display_name))
        display_name = re.sub(r'["“”]', "", display_name).title()

        return Product(
            retailer=self.name,
            name=display_name,
            brand=brand,
            style_code=style,
            url=f"{BASE}/{slug}",
            price=price,
            currency="EUR",
            sizes=sizes,
            in_stock=any(s.in_stock for s in sizes) if sizes else False,
            price_verified=True,
        )

    @staticmethod
    def _parse_size_config(html: str):
        """Sizes from the Magento swatch jsonConfig: label + availability +
        brand/gender/size-system metadata reede helpfully attaches per option."""
        name = brand = gender = None
        sizes: list[RetailSize] = []
        m = re.search(r'"jsonConfig":\s*\{', html)
        if not m:
            return name, brand, gender, sizes
        decoder = json.JSONDecoder()
        try:
            cfg, _ = decoder.raw_decode(html, m.end() - 1)
        except json.JSONDecodeError:
            return name, brand, gender, sizes
        for attr in (cfg.get("attributes") or {}).values():
            if attr.get("code") != "size":
                continue
            for opt in attr.get("options") or []:
                label = str(opt.get("label") or "").replace(",", ".").strip()
                if not label:
                    continue
                system = str(opt.get("default_size_type") or "EU").upper()
                brand = opt.get("brand") or brand
                gender = opt.get("gender") or gender
                sizes.append(RetailSize(
                    label=label,
                    system=system,
                    us_size=label if system == "US" else None,
                    in_stock=bool(opt.get("products")),
                ))
        return name, brand, gender, sizes
