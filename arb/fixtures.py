"""Synthetic data for the offline selftest and the fixture market provider.

Lets the whole pipeline run with zero network access: a known-profitable shoe,
a break-even shoe, and a variant with null market data (the official API's
favorite trick) to prove graceful degradation.
"""
from __future__ import annotations

from .db import Database
from .models import MarketData, Product, RetailSize, StockXProduct, StockXVariant

FIX_PRODUCT_ID = "fixture-prod-aj1low"
FIX_PRODUCT_ID_2 = "fixture-prod-campus"
# Sizeless products: exactly one variant, no size value, empty styleId — the
# real shape of every collectible/watch/electronics item on StockX (verified
# 2026-07-29). Before the sizeless path existed these produced NOTHING.
FIX_PRODUCT_ID_3 = "fixture-prod-lego-75192"
FIX_PRODUCT_ID_4 = "fixture-prod-ps5-us-plug"


def retail_products() -> list[Product]:
    return [
        Product(
            retailer="ballzy", name="Air Jordan 1 Low Se", brand="Jordan",
            style_code="HQ2010-005", colorway="Multicolor",
            url="https://ballzy.eu/en/product/air-jordan-1-low-se-hq2010-005-8-5",
            price=79.0, currency="EUR",
            sizes=[
                RetailSize(label="42", system="EU", us_size="8.5", in_stock=True),
                RetailSize(label="44", system="EU", us_size="10", in_stock=True),
                RetailSize(label="45", system="EU", us_size="11", in_stock=False),
            ],
            in_stock=True, price_verified=True,
        ),
        Product(
            retailer="ballzy", name="Campus St", brand="adidas",
            style_code="KJ1033", colorway="Grey",
            url="https://ballzy.eu/en/product/campus-st-kj1033-4",
            price=110.0, currency="EUR",
            sizes=[RetailSize(label="42", system="EU", us_size="8.5", in_stock=True)],
            in_stock=True, price_verified=True,
        ),
        # sizeless + profitable: must produce exactly one product-level signal
        Product(
            retailer="apollo", name="LEGO Star Wars Millennium Falcon 75192",
            brand="LEGO", style_code="75192", category="collectibles",
            url="https://www.apollo.ee/lego-star-wars-millennium-falcon-75192",
            price=599.0, currency="EUR", sizes=[],
            in_stock=True, price_verified=True,
        ),
        # sizeless + hugely profitable on paper, but StockX pins "(US Plug)"
        # and the retail listing does not confirm the region: must NOT alert
        Product(
            retailer="klick", name="Sony PlayStation 5 Pro 30th Anniversary",
            brand="Sony", style_code="PS5PRO30TH", category="electronics",
            url="https://www.klick.ee/sony-playstation-5-pro-30th",
            price=700.0, currency="EUR", sizes=[],
            in_stock=True, price_verified=True,
        ),
    ]


def seed_catalog(db: Database) -> None:
    """Pre-fill the resolution + variant caches as if the API had answered."""
    aj1 = StockXProduct(
        product_id=FIX_PRODUCT_ID, style_id="HQ2010-005",
        title="Jordan 1 Low SE Multicolor", brand="Jordan",
        url_key="jordan-1-low-se-multicolor", product_type="sneakers",
        gender="men", retail_price=140.0)
    campus = StockXProduct(
        product_id=FIX_PRODUCT_ID_2, style_id="KJ1033",
        title="adidas Campus ST Grey", brand="adidas",
        url_key="adidas-campus-st-grey", product_type="sneakers",
        gender="men", retail_price=120.0)
    # Note the EMPTY style_id on both: that is what StockX actually returns for
    # collectibles, which is why these resolve on the code in the TITLE.
    lego = StockXProduct(
        product_id=FIX_PRODUCT_ID_3, style_id="",
        title=("LEGO Star Wars Millennium Falcon Ultimate Collector Series "
               "Set 75192"),
        brand="LEGO", url_key="lego-star-wars-millennium-falcon-75192",
        product_type="collectibles", retail_price=849.99)
    ps5 = StockXProduct(
        product_id=FIX_PRODUCT_ID_4, style_id="",
        title=("Sony PlayStation 5 PS5 Pro 30th Anniversary Limited Edition "
               "Bundle (US Plug)"),
        brand="Sony", url_key="sony-ps5-pro-30th-anniversary-us-plug",
        product_type="collectibles", retail_price=799.99)
    db.put_resolution("HQ2010-005", aj1, 1.0)
    db.put_resolution("KJ1033", campus, 1.0)
    db.put_resolution("75192", lego, 1.0)
    db.put_resolution("PS5PRO30TH", ps5, 1.0)
    db.put_variants(FIX_PRODUCT_ID, [
        StockXVariant(product_id=FIX_PRODUCT_ID, variant_id="fix-aj1-8.5",
                      size="8.5", conversions={"eu": "42", "us m": "8.5"}),
        StockXVariant(product_id=FIX_PRODUCT_ID, variant_id="fix-aj1-10",
                      size="10", conversions={"eu": "44", "us m": "10"}),
        StockXVariant(product_id=FIX_PRODUCT_ID, variant_id="fix-aj1-11",
                      size="11", conversions={"eu": "45", "us m": "11"}),
    ])
    db.put_variants(FIX_PRODUCT_ID_2, [
        StockXVariant(product_id=FIX_PRODUCT_ID_2, variant_id="fix-campus-8.5",
                      size="8.5", conversions={"eu": "42", "us m": "8.5"}),
    ])
    # one variant, no size — exactly what the live API returns here
    db.put_variants(FIX_PRODUCT_ID_3, [
        StockXVariant(product_id=FIX_PRODUCT_ID_3, variant_id="fix-lego-one",
                      size=None, conversions={}),
    ])
    db.put_variants(FIX_PRODUCT_ID_4, [
        StockXVariant(product_id=FIX_PRODUCT_ID_4, variant_id="fix-ps5-one",
                      size=None, conversions={}),
    ])


def market_data() -> dict[str, dict[str, MarketData]]:
    return {
        FIX_PRODUCT_ID: {
            # profitable: bid 145 -> payout ~118.66 vs retail 79
            "fix-aj1-8.5": MarketData(variant_id="fix-aj1-8.5", currency="EUR",
                                      lowest_ask=165.0, highest_bid=145.0),
            # null market data — must be skipped, never crash
            "fix-aj1-10": MarketData(variant_id="fix-aj1-10", currency="EUR",
                                     lowest_ask=None, highest_bid=None),
            "fix-aj1-11": MarketData(variant_id="fix-aj1-11", currency="EUR",
                                     lowest_ask=150.0, highest_bid=120.0),
        },
        FIX_PRODUCT_ID_2: {
            # not profitable: bid 95 on a 110 retail
            "fix-campus-8.5": MarketData(variant_id="fix-campus-8.5",
                                         currency="EUR", lowest_ask=120.0,
                                         highest_bid=95.0),
        },
        FIX_PRODUCT_ID_3: {
            # profitable: bid 780 -> payout ~679 vs retail 599
            "fix-lego-one": MarketData(variant_id="fix-lego-one",
                                       currency="EUR", lowest_ask=900.0,
                                       highest_bid=780.0),
        },
        FIX_PRODUCT_ID_4: {
            # would clear the floor by a mile — the region gate must stop it
            "fix-ps5-one": MarketData(variant_id="fix-ps5-one",
                                      currency="EUR", lowest_ask=3301.0,
                                      highest_bid=1984.0),
        },
    }
