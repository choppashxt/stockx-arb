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

import json
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

# Magento product page layout: the product's own price box lives inside
# <div class="product-info-main">; "related"/"upsell" carousels come after it
# and carry OTHER products' data-price-amount values. Scoping to this block is
# what makes min() mean "this product's lowest price" rather than "the
# cheapest thing anywhere on the page".
_OWN_BLOCK_START = 'product-info-main'
_OWN_BLOCK_ENDS = ('block-related', 'block-upsell', 'block-crosssell',
                   'products-grid', 'product-info-detailed')
_PRICE_RE = re.compile(r'data-price-amount="([\d.]+)"')
_SIZE_OPT_RE = re.compile(
    r'\{"id":"\d+","label":"([\d,.]{1,5})","products":\[([^\]]*)\]')


def _jsonconfig(html: str) -> Optional[dict]:
    """Magento's configurable-product jsonConfig, parsed as JSON (balanced-brace
    scan from the key; the page embeds it inside a <script> initialiser)."""
    i = html.find('"jsonConfig":')
    if i < 0:
        return None
    j = html.find("{", i)
    if j < 0:
        return None
    depth = 0
    for k in range(j, len(html)):
        ch = html[k]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(html[j:k + 1])
                except ValueError:
                    return None
    return None


def _sizes_from_jsonconfig(html: str) -> Optional[list[RetailSize]]:
    """Per-size stock from jsonConfig, using the SALABLE map when present.

    Bug this fixes (2026-09-16): a Phantom 6 alerted in US 10, which the site
    would not sell. Magento with MSI ships two lists: `attributes[size]
    .options[].products` = child products that exist with stock somewhere,
    and `salable[attr_id][option_id]` = the children actually purchasable on
    THIS website/channel. The page had 8 sizes in products[] but only 5 in
    salable; the scraper read products[]. When `salable` is absent (Magento
    without MSI) products[] is the only signal and is used as before.
    Returns None when jsonConfig cannot be parsed, so the caller can fall back.
    """
    cfg = _jsonconfig(html)
    if not cfg:
        return None
    attrs = cfg.get("attributes") or {}
    size_attr_id, size_attr = None, None
    for aid, attr in attrs.items():
        code = str(attr.get("code") or "").lower()
        if code == "size" or "size" in code or "suurus" in code:
            size_attr_id, size_attr = str(aid), attr
            break
    if size_attr is None:
        return None
    salable_map = cfg.get("salable")
    salable_for_attr = None
    if isinstance(salable_map, dict) and size_attr_id in salable_map:
        salable_for_attr = salable_map[size_attr_id] or {}
    sizes: list[RetailSize] = []
    for opt in size_attr.get("options") or []:
        label = str(opt.get("label") or "").replace(",", ".").strip()
        if not label:
            continue
        products = [str(x) for x in (opt.get("products") or [])]
        if salable_for_attr is not None:
            in_stock = bool(salable_for_attr.get(str(opt.get("id"))))
        else:
            in_stock = bool(products)
        sizes.append(RetailSize(label=label, system="US", us_size=label,
                                in_stock=in_stock))
    return sizes


def _own_price_block(html: str) -> str:
    """The slice of a product page holding the product's OWN price markup.

    Bug this fixes (2026-09-11..13): enrich() took min() over every
    data-price-amount on the page. A Kobe 5 Protro at EUR 170 had a EUR 10.99
    accessory in its related-products carousel, so the "confirmed" price became
    10.99 and it alerted in 21 sizes at up to +EUR 180 apparent profit. Falls
    back to the whole page only when the layout marker is absent, so an
    unrecognised template degrades to the old behaviour rather than to no price.
    """
    start = html.find(_OWN_BLOCK_START)
    if start < 0:
        return html
    end = len(html)
    for marker in _OWN_BLOCK_ENDS:
        i = html.find(marker, start + len(_OWN_BLOCK_START))
        if 0 <= i < end:
            end = i
    return html[start:end]


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

        prices = [float(p) for p in _PRICE_RE.findall(_own_price_block(html))
                  if float(p) > 0]
        if prices:
            enriched.price = min(prices)

        sizes = _sizes_from_jsonconfig(html)
        if sizes is None:
            # jsonConfig not parseable as JSON: fall back to the regex view,
            # which sees products[] only (may overstate stock — see below)
            sizes = []
            for label, prods in _SIZE_OPT_RE.findall(html):
                label = label.replace(",", ".").strip()
                sizes.append(RetailSize(
                    label=label, system="US", us_size=label,
                    in_stock=bool(prods.strip()),
                ))
        if sizes:
            enriched.sizes = sizes
            enriched.in_stock = any(s.in_stock for s in sizes)
        enriched.price_verified = bool(prices)
        return enriched
