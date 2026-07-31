"""klick.ee — Estonian electronics. Matched by BARCODE only.

Electronics are the awkward category. StockX gives them an empty `styleId` and
a title with no MPN in it ('Sony PlayStation 5 PS5 Pro 30th Anniversary Limited
Edition Bundle (US Plug)'), so neither the style-code path nor the title-code
path has anything to bite on. The EAN is the only exact key, and klick.ee
publishes one for every product (`gtin13` in an ld+json `@graph`).

Two safety properties matter more here than anywhere else:

- **Region.** StockX sells the US/EU/UK plug variants as SEPARATE products with
  near-identical titles. The scanner's region gate refuses to alert when StockX
  pins a region the retail listing does not state, which for an Estonian shop
  is nearly always. That is deliberate: the failure mode is buying a EU unit
  against a US bid and failing authentication.
- **Sealed.** StockX authenticates electronics sealed and unopened.

robots.txt (verified 2026-07-29): sitemap at /sitemap.xml, no Crawl-delay for
`*`, disallows /checkout, /my-account/, /compare, /dist/, /ru/ and `?price=`
filters. Those are enforced here as well as by the robots parser.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from ..models import Product
from .base import RetailerScraper
from .ldjson import brand_of, gtin_of, in_stock, ld_products, price_of

log = logging.getLogger(__name__)

SITEMAP = "https://www.klick.ee/sitemap.xml"

_ROBOTS_DENY = ("/dist/", "/compare", "/checkout", "/my-account/", "/kiosk/",
                "/ru/", "?price=")


class KlickScraper(RetailerScraper):
    name = "klick"

    def _allowed(self, url: str) -> bool:
        return not any(seg in url for seg in _ROBOTS_DENY)

    async def scan(self) -> list[Product]:
        slugs = await self.cached_sitemap_slugs(
            [SITEMAP], loc_pattern=r"(https://www\.klick\.ee/[^\"<]+)",
            max_sitemaps=1)
        wanted = [u for u in slugs
                  if self._allowed(u) and not u.endswith(".xml")
                  and self.slug_wanted(u.rsplit("/", 1)[-1])]
        log.info("%s: %d candidate product URLs after slug filter",
                 self.name, len(wanted))

        products: list[Product] = []
        for url in self.rotation_slice(wanted, self.cfg.sitemap_slice_per_scan):
            product = await self._product_from_page(url)
            if product is not None:
                products.append(product)
        return products

    async def _product_from_page(self, url: str) -> Optional[Product]:
        if not self._allowed(url):
            return None
        html = await self.fetcher.get(url)
        if not html:
            return None
        items = ld_products(html)
        if not items:
            return None
        item = items[0]

        gtin = gtin_of(item)
        if gtin is None:
            # no barcode means no key at all for this category — an electronics
            # product without one is unmatchable, not a candidate
            return None
        price = price_of(item)
        if price is None:
            return None

        return Product(
            retailer=self.name,
            name=(item.get("name") or "").strip(),
            brand=brand_of(item),
            style_code=None,            # nothing StockX would recognise
            category="electronics",
            url=str(item.get("url") or url),
            price=price,
            currency=str((item.get("offers") or {}).get("priceCurrency")
                         or "EUR") if isinstance(item.get("offers"), dict)
            else "EUR",
            sizes=[],                   # sealed box: no size
            in_stock=in_stock(item),
            price_verified=True,        # ld+json IS the product page price
            ean=gtin,
        )

    async def enrich(self, product: Product) -> Optional[Product]:
        return await self._product_from_page(product.url)


_EAN_RE = re.compile(r"^\d{8,14}$")


def looks_like_ean(value: str) -> bool:
    return bool(_EAN_RE.match((value or "").strip()))
