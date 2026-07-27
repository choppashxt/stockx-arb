"""Teamsport (teamsport.ee) — Tier 1, Estonia. Basketball specialist (Nike/
Jordan heavy) on classic Magento.

Verified 2026-07-27:
- robots.txt blocks only backend paths and /catalogsearch/; category pages and
  their pagination are allowed. /ee/category?p=N lists 48 tiles per page.
- Tiles carry the product URL (slug ends with the style code, e.g.
  book-2-m-ir6443-100), the style code again in the image filename
  (IR6443-100.jpg), the name in the img alt, and data-price-amount values
  (regular + discounted; the lowest is the buyable price).
- Product pages have a Magento jsonConfig size attribute: US labels with
  comma decimals ("8,5"), non-empty products[] = that size is in stock.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from ..models import Product, RetailSize
from .base import RetailerScraper

log = logging.getLogger(__name__)

BASE = "https://www.teamsport.ee"
PAGE_SIZE = 48

_TILE_MARKER = 'class="product-item-info'
_URL_RE = re.compile(r'href="(https://www\.teamsport\.ee/ee/[a-z0-9\-]+)"')
_STYLE_URL_RE = re.compile(r"([a-z]{1,3}\d{4,6}-\d{3})(?:-cnf)?$")
_STYLE_IMG_RE = re.compile(r"/([A-Z]{1,3}\d{4,6}-\d{3})(?:_\d+)?\.jpg")
_ALT_RE = re.compile(r'alt="([^"]{3,80})"')
_PRICE_RE = re.compile(r'data-price-amount="([\d.]+)"')
_SIZE_OPT_RE = re.compile(
    r'\{"id":"\d+","label":"([\d,.]{1,5})","products":\[([^\]]*)\]')


class TeamsportScraper(RetailerScraper):
    name = "teamsport"

    async def scan(self) -> list[Product]:
        products: dict[str, Product] = {}
        categories = self.cfg.categories or ["/ee/category"]
        for category in categories:
            for page in range(1, self.cfg.max_pages + 1):
                url = f"{BASE}{category}" + (f"?p={page}" if page > 1 else "")
                html = await self.fetcher.get(url)
                if not html:
                    break
                # split on tile markers: each segment holds one tile's markup
                # (URL, image, name, prices) in document order
                tiles = html.split(_TILE_MARKER)[1:]
                if not tiles:
                    break
                new_on_page = 0
                for tile in tiles:
                    p = self._product_from_tile(tile)
                    if p is not None and p.url not in products:
                        products[p.url] = p
                        new_on_page += 1
                log.info("teamsport %s page %d: %d tiles, %d new",
                         category, page, len(tiles), new_on_page)
                if len(tiles) < PAGE_SIZE or new_on_page == 0:
                    break
        return list(products.values())

    def _product_from_tile(self, tile: str) -> Optional[Product]:
        um = _URL_RE.search(tile)
        if not um:
            return None
        url = um.group(1)
        style = None
        if (sm := _STYLE_URL_RE.search(url.rsplit("/", 1)[-1])):
            style = sm.group(1).upper()
        elif (im := _STYLE_IMG_RE.search(tile)):
            style = im.group(1).upper()
        prices = [float(p) for p in _PRICE_RE.findall(tile) if float(p) > 0]
        if not prices:
            return None
        alt = _ALT_RE.search(tile)
        return Product(
            retailer=self.name,
            name=(alt.group(1).strip().title() if alt else url.rsplit("/", 1)[-1]),
            brand=None,
            style_code=style,
            url=url,
            price=min(prices),
            currency="EUR",
            sizes=[],
            in_stock=True,          # Magento grids list purchasable items
            price_verified=False,
        )

    async def enrich(self, product: Product) -> Optional[Product]:
        html = await self.fetcher.get(product.url)
        if not html:
            return None
        enriched = product.model_copy(deep=True)

        prices = [float(p) for p in _PRICE_RE.findall(html) if float(p) > 0]
        if prices:
            enriched.price = min(prices)

        sizes: list[RetailSize] = []
        for label, prods in _SIZE_OPT_RE.findall(html):
            label = label.replace(",", ".").strip()
            sizes.append(RetailSize(
                label=label,
                system="US",        # teamsport lists Nike/Jordan in US sizes
                us_size=label,
                in_stock=bool(prods.strip()),
            ))
        if sizes:
            enriched.sizes = sizes
            enriched.in_stock = any(s.in_stock for s in sizes)
        enriched.price_verified = bool(prices)
        return enriched
