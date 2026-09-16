"""Regressions for the 2026-09-11 false-alert burst.

A Kobe 5 Protro (StockX MSRP EUR 170) alerted in 21 sizes at up to +EUR 180
because teamsport's enrich() took min() over EVERY data-price-amount on the
product page — including a EUR 10.99 accessory in the related-products
carousel. Two independent guards:

  1. the scraper only reads the product's own price block
  2. _evaluate_product refuses any listing under a fraction of StockX MSRP,
     before spending a market-data call on it
"""
import asyncio

from arb.config import AppConfig
from arb.db import Database
from arb.models import Product, StockXProduct
from arb.retailers.teamsport import _own_price_block, _PRICE_RE
from arb.scanner import _evaluate_product

PAGE = (
    '<html><div class="page-header">...</div>'
    '<div class="product-info-main">'
    '  <span data-price-type="oldPrice" data-price-amount="369.99">369,99</span>'
    '  <span data-price-type="finalPrice" data-price-amount="220">220</span>'
    '  <div class="product-add-form">...</div>'
    '</div>'
    '<div class="block block-related"><div class="products-grid">'
    '  <span data-price-amount="10.99">10,99</span>'   # socks
    '  <span data-price-amount="329.99">329,99</span>'
    '</div></div></html>'
)


class TestOwnPriceBlock:
    def test_related_carousel_prices_are_excluded(self):
        prices = [float(p) for p in _PRICE_RE.findall(_own_price_block(PAGE))]
        assert prices == [369.99, 220.0]
        assert min(prices) == 220.0           # not the EUR 10.99 socks

    def test_whole_page_min_was_the_bug(self):
        assert min(float(p) for p in _PRICE_RE.findall(PAGE)) == 10.99

    def test_unknown_layout_degrades_to_whole_page(self):
        html = '<div><span data-price-amount="99">99</span></div>'
        assert _own_price_block(html) == html


class _Resolver:
    """Resolves everything to one product with a known MSRP; counts calls."""
    def __init__(self, msrp):
        self.msrp = msrp
        self.variants_calls = 0

    async def resolve(self, candidates, brand=None, name=None):
        return StockXProduct(product_id="p1", style_id=candidates[0],
                             title="Nike Kobe 5 Protro", brand="Nike",
                             retail_price=self.msrp), 1.0

    async def variants(self, product_id):
        self.variants_calls += 1
        return []


class _Provider:
    def __init__(self):
        self.calls = 0

    async def get_market_data(self, product_id):
        self.calls += 1
        return {}


def _run(price, msrp, floor=0.20):
    cfg = AppConfig()
    cfg.filters.min_price_vs_stockx_retail_pct = floor
    db = Database(":memory:")
    resolver, provider = _Resolver(msrp), _Provider()
    product = Product(retailer="teamsport", name="Kobe 5 Protro",
                      style_code="IO6256-400", url="https://t/kobe",
                      price=price, in_stock=True)
    opps, used = asyncio.run(_evaluate_product(
        product, cfg, db, resolver, provider, scraper=None,
        allow_market_call=True))
    reviews = db.conn.execute(
        "SELECT reason FROM review_queue").fetchall()
    return opps, used, provider.calls, resolver.variants_calls, \
        [r["reason"] for r in reviews]


class TestMsrpSanityGate:
    def test_parse_error_price_is_refused_before_any_api_call(self):
        opps, used, market_calls, variant_calls, reasons = _run(10.99, 170.0)
        assert opps == [] and used is False
        assert market_calls == 0 and variant_calls == 0
        assert reasons == ["implausible_price"]

    def test_genuine_clearance_passes(self):
        # 35% of MSRP — the deepest real alert so far was 37%
        _, _, _, variant_calls, reasons = _run(59.99, 170.0)
        assert variant_calls == 1          # got past the gate
        assert reasons == []

    def test_no_msrp_known_means_no_gate(self):
        _, _, _, variant_calls, reasons = _run(10.99, None)
        assert variant_calls == 1 and reasons == []

    def test_zero_disables(self):
        _, _, _, variant_calls, reasons = _run(10.99, 170.0, floor=0)
        assert variant_calls == 1 and reasons == []
