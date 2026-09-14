"""Teamsport scraper regressions.

The live bug this pins: a EUR 209 Kobe 5 Protro alerted repeatedly at EUR 10.99.
Magento renders data-price-amount for every price on a product page, including
the related/upsell carousels, and enrich() took min() across the whole document
— so the shoe was priced at whatever the cheapest accessory in those carousels
cost, and stamped price_verified=True, which suppressed the "(grid price,
verify)" hedge. Because the carousels rotate between requests, each scan picked
a different wrong price and re-alerted on profit_change.
"""
import asyncio

import pytest

from arb.config import RetailerConfig
from arb.models import Product
from arb.retailers.teamsport import TeamsportScraper, _main_product_html

MAIN = '''<div class="product-info-main">
  <span class="price-wrapper" data-price-amount="209.99" data-price-type="finalPrice">
    <span class="price">209,99 &euro;</span></span></div>
<script type="text/x-magento-init">
 {"#product_addtocart_form":{"configurable":{"spConfig":{"attributes":{"173":{
   "options":[{"id":"101","label":"12,5","products":["58821"]},
              {"id":"102","label":"13","products":[]}]}}}}}}
</script>'''

CAROUSEL = '''<div class="block related"><div class="product-item-info">
    <a href="https://www.teamsport.ee/ee/nike-elite-sokid"></a>
    <span data-price-amount="10.99"></span></div>
  <div class="product-item-info"><span data-price-amount="7.50"></span></div></div>'''

GRID_PRICE = 209.99


def _enrich(html: str, grid_price: float = GRID_PRICE) -> Product:
    class _Fetcher:
        async def get(self, url):
            return html

    s = TeamsportScraper.__new__(TeamsportScraper)
    s.cfg = RetailerConfig()
    s.fetcher = _Fetcher()
    grid = Product(retailer="teamsport", name="Kobe V Protro",
                   style_code="IO6256-400", price=grid_price,
                   url="https://www.teamsport.ee/ee/kobe-v-protro-io6256-400",
                   price_verified=False)
    return asyncio.run(s.enrich(grid))


class TestPriceScoping:
    def test_related_carousel_price_is_not_the_product_price(self):
        # THE bug: EUR 10.99 socks became the price of a EUR 209 shoe
        out = _enrich(MAIN + CAROUSEL)
        assert out.price == pytest.approx(209.99)
        assert out.price_verified is True

    def test_works_without_the_standard_main_container(self):
        # themes vary; cutting at the carousel is the part that matters
        out = _enrich(MAIN.replace("product-info-main", "pdp-main") + CAROUSEL)
        assert out.price == pytest.approx(209.99)

    @pytest.mark.parametrize("marker", [
        "block related", "block upsell", "block crosssell",
        "products-related", "block-products-list",
    ])
    def test_each_carousel_flavour_is_cut(self, marker):
        out = _enrich(MAIN + CAROUSEL.replace("block related", marker))
        assert out.price == pytest.approx(209.99)

    def test_genuine_markdown_on_the_page_still_wins(self):
        # scoping must not freeze the price: a real special price inside the
        # main block is exactly what enrich() exists to pick up
        marked_down = MAIN.replace(
            '<span class="price-wrapper" data-price-amount="209.99"',
            '<span data-price-amount="209.99"></span>'
            '<span class="price-wrapper" data-price-amount="149.99"')
        out = _enrich(marked_down + CAROUSEL)
        assert out.price == pytest.approx(149.99)
        assert out.price_verified is True


class TestImplausiblePriceGuard:
    def test_unrecognised_carousel_markup_does_not_produce_a_bargain(self):
        # belt and braces: if the cut misses, the page price is still refused
        out = _enrich(MAIN + CAROUSEL.replace("block related", "mystery-widget"))
        assert out.price == pytest.approx(GRID_PRICE)

    def test_refused_page_price_is_never_marked_verified(self):
        # price_verified=True is what removed the "(grid price, verify)" hedge
        # from the bad alert and let it read as a confirmed bargain
        out = _enrich(MAIN + CAROUSEL.replace("block related", "mystery-widget"))
        assert out.price_verified is False


class TestSizes:
    def test_sizes_survive_price_scoping(self):
        # sizes come from jsonConfig, which can sit outside the main block —
        # scoping the PRICE must not scope them away
        out = _enrich(MAIN + CAROUSEL)
        assert [(s.label, s.in_stock) for s in out.sizes] == [
            ("12.5", True), ("13", False)]

    def test_sizes_are_labelled_us_not_eu(self):
        # teamsport lists Nike/Jordan in US; calling a US 12.5 "EU 12.5" is a
        # two-size buying error
        out = _enrich(MAIN + CAROUSEL)
        assert {s.system for s in out.sizes} == {"US"}

    def test_empty_products_list_means_out_of_stock(self):
        out = _enrich(MAIN + CAROUSEL)
        assert out.in_stock is True
        assert [s.label for s in out.sizes if not s.in_stock] == ["13"]


class TestMainProductHtml:
    def test_returns_something_when_no_markers_exist_at_all(self):
        assert "209.99" in _main_product_html(MAIN)

    def test_drops_the_carousel(self):
        scoped = _main_product_html(MAIN + CAROUSEL)
        assert "209.99" in scoped and "10.99" not in scoped
