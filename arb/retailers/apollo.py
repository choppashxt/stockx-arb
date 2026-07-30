"""apollo.ee — Lego sets and other sealed collectibles.

First non-sneaker retailer. What makes it workable:
- robots.txt allows product pages (`/*.html`), with no Crawl-delay. It DOES
  disallow `/*/catalogsearch`, `/*/cart`, `/*/customer/` and friends, so the
  catalog is enumerated from the sitemaps only. urllib's robotparser would
  actually permit catalogsearch here (their `Allow: /` comes first and it
  returns the first matching rule), so this restriction is enforced in code
  rather than left to the parser.
- every product page carries a schema.org `Product` ld+json block with name,
  price, availability, `sku` and `gtin` — no HTML scraping guesswork.

Matching: StockX carries collectibles with an EMPTY styleId and the identifying
code in the title ('... Set 75192'), so the Lego SET NUMBER is the match key
and `code_in_title` does the comparison. Requiring a 4-5 digit set number in
the product name is also what separates Lego SETS from the many Lego
picture-books Apollo sells, which share the 'lego-' slug but have no set number
and no StockX market.

Sizeless: a sealed box has no size. `sizes` stays empty and the scanner's
sizeless path emits one product-level opportunity.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Optional

from ..models import Product
from .base import RetailerScraper

log = logging.getLogger(__name__)

BASE = "https://www.apollo.ee"
SITEMAP_INDEX = "https://api.apollo.ee/sitemap_en.xml"

# Paths robots.txt disallows. Enforced here because urllib's robotparser returns
# the FIRST matching rule and Apollo puts `Allow: /` above its Disallow list.
_ROBOTS_DENY = ("/catalogsearch", "/cart", "/checkout", "/customer/",
                "/my-account/", "/wishlist", "/reviews/")

# Lego set numbers are 4-6 digits. Bounded by non-digits so a piece count inside
# a longer number cannot be mistaken for one.
_SET_NUMBER = re.compile(r"(?<!\d)(\d{4,6})(?!\d)")

# Years are NOT set numbers, and treating one as a code is actively dangerous:
# 'lego-advendikalender-disney-animation-2025' yielded code '2025', which then
# matched the StockX title '2025 Pokemon Mega Evolution Charizard X ex
# Ultra-Premium Collection' — pricing a EUR 31 advent calendar against a EUR 211
# card-box bid. Caught on the first live Apollo run, 2026-07-29.
_YEAR_LIKE = re.compile(r"^(19|20)\d{2}$")

# Categories with no StockX resale market — skip before spending a page fetch.
_SKIP_CATEGORY = re.compile(r"\b(books?|raamat|literature|magazine|stationery)\b",
                            re.I)


def _ld_products(html: str) -> list[dict]:
    """Every schema.org Product block on the page."""
    out = []
    for block in re.findall(
            r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
            html, re.S):
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        for item in (data if isinstance(data, list) else [data]):
            if isinstance(item, dict) and item.get("@type") == "Product":
                out.append(item)
    return out


def _offer(item: dict) -> dict:
    offers = item.get("offers") or {}
    if isinstance(offers, list):
        return offers[0] if offers and isinstance(offers[0], dict) else {}
    return offers if isinstance(offers, dict) else {}


def set_number_from_name(name: str) -> Optional[str]:
    """The Lego set number in a product name, or None.

    Takes the FIRST 4-6 digit run. Both name orders put the set number ahead of
    any descriptive numbers — 'LEGO Technic 42115 Lamborghini Sian FKP 37' and
    'LEGO Star Wars Millennium Falcon 75192' — whereas piece counts and years
    trail at the end, so reading from the front avoids picking up '3696 pieces'.

    Year-like runs are skipped: a year is not an identifying code, and one that
    slips through matches any StockX title that happens to start with it.

    Returns None when there is no such run at all, which is what filters out the
    Lego picture-books Apollo sells under the same 'lego-' slug.
    """
    for candidate in _SET_NUMBER.findall(name or ""):
        if not _YEAR_LIKE.match(candidate):
            return candidate
    return None


class ApolloScraper(RetailerScraper):
    name = "apollo"

    def _allowed(self, url: str) -> bool:
        return not any(seg in url for seg in _ROBOTS_DENY)

    async def scan(self) -> list[Product]:
        # sitemap_en.xml is an INDEX of ~20 child sitemaps on another host, so
        # it has to be expanded one level before the product URLs appear. The
        # index itself is ~2KB; the children total tens of MB, which is why the
        # expanded result is what gets cached.
        index = await self.fetcher.get(SITEMAP_INDEX)
        children = re.findall(r"<loc>(https://\S+?\.xml)</loc>", index or "")
        if not children:
            log.warning("%s: sitemap index gave no child sitemaps", self.name)
            return []
        slugs = await self.cached_sitemap_slugs(
            children,
            loc_pattern=r"(https://www\.apollo\.ee/en/[^\"<]+\.html)",
            max_sitemaps=self.cfg.max_pages)

        # Apollo is primarily a bookshop: of ~190k catalog URLs, 243 mention
        # lego and only a handful are actual SETS — the rest are Lego
        # picture-books and sticker albums with no resale market. The set number
        # appears in the slug, so filtering on it here means we spend page
        # fetches on sets instead of discovering the same books every scan.
        wanted = [u for u in slugs
                  if self._allowed(u)
                  and self.slug_wanted(slug := u.rsplit("/", 1)[-1])
                  and set_number_from_name(slug)]
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
        items = _ld_products(html)
        if not items:
            return None
        item = items[0]
        name = (item.get("name") or "").strip()
        category = str(item.get("category") or "")
        if _SKIP_CATEGORY.search(category):
            return None

        set_number = set_number_from_name(name)
        if set_number is None:
            # no set number => not a boxed set (Lego books, accessories). Without
            # a code there is nothing to match on, so don't manufacture one.
            return None

        offer = _offer(item)
        try:
            price = float(offer.get("price"))
        except (TypeError, ValueError):
            return None
        if price <= 0:
            return None
        availability = str(offer.get("availability") or "")
        in_stock = "InStock" in availability or "PreOrder" in availability

        gtin = str(item.get("gtin") or item.get("gtin13")
                   or item.get("gtin12") or "").strip() or None

        return Product(
            retailer=self.name, name=name,
            brand=_brand(name), style_code=set_number,
            category="collectibles",
            url=item.get("url") or url,
            price=price,
            currency=str(offer.get("priceCurrency") or "EUR"),
            sizes=[],                       # sealed box: no size, ever
            in_stock=in_stock,
            price_verified=True,            # ld+json IS the product page price
            ean=gtin,
        )

    async def enrich(self, product: Product) -> Optional[Product]:
        """Re-read the product page. scan() already reads authoritative
        ld+json, so this mainly re-confirms price and stock before an alert."""
        return await self._product_from_page(product.url)


def _brand(name: str) -> Optional[str]:
    first = (name or "").split()
    return first[0] if first else None
