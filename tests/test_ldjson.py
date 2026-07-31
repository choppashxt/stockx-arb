"""Shared schema.org ld+json extraction.

Two real shapes: apollo.ee emits a bare Product object, klick.ee nests it inside
an `@graph`. A top-level `@type` check finds the first and silently misses the
second — which is why klick.ee first looked like it had no structured data at
all, despite carrying gtin13, price and availability.
"""
from arb.retailers.ldjson import (
    brand_of,
    gtin_of,
    in_stock,
    ld_products,
    offer,
    price_of,
)

BARE = """<script type="application/ld+json">
{"@type":"Product","name":"LEGO Technic Bugatti Bolide 42151",
 "gtin":"5702017424538","offers":{"@type":"Offer","price":54.99,
 "availability":"https://schema.org/InStock"}}</script>"""

GRAPH = """<script type="application/ld+json">
{"@context":"https://schema.org","@graph":[
  {"@type":"Organization","name":"Klick"},
  {"@type":"Product","name":"Sony PlayStation 5 Pro",
   "sku":"K6923172594099","gtin13":"6923172594099",
   "brand":{"@type":"Brand","name":"Sony"},
   "offers":{"@type":"Offer","price":"274.99","priceCurrency":"EUR",
             "availability":"https://schema.org/InStock"}}]}</script>"""


class TestExtraction:
    def test_bare_product(self):
        items = ld_products(BARE)
        assert len(items) == 1
        assert items[0]["name"].startswith("LEGO Technic")

    def test_product_nested_in_graph(self):
        items = ld_products(GRAPH)
        assert len(items) == 1
        assert items[0]["name"] == "Sony PlayStation 5 Pro"

    def test_non_product_graph_entries_ignored(self):
        assert all(i.get("@type") == "Product" for i in ld_products(GRAPH))

    def test_malformed_json_skipped(self):
        assert ld_products('<script type="application/ld+json">{oops</script>') == []

    def test_no_scripts(self):
        assert ld_products("<html><body>nothing</body></html>") == []


class TestFields:
    def test_gtin_prefers_the_specific_field(self):
        assert gtin_of(ld_products(GRAPH)[0]) == "6923172594099"
        assert gtin_of(ld_products(BARE)[0]) == "5702017424538"

    def test_gtin_strips_separators_and_rejects_short(self):
        assert gtin_of({"gtin13": "5702-0174-24538"}) == "5702017424538"
        assert gtin_of({"gtin": "123"}) is None
        assert gtin_of({}) is None

    def test_brand_from_object_or_string(self):
        assert brand_of(ld_products(GRAPH)[0]) == "Sony"
        assert brand_of({"brand": "LEGO"}) == "LEGO"
        assert brand_of({}) is None

    def test_price_parses_strings_and_commas(self):
        assert price_of(ld_products(GRAPH)[0]) == 274.99
        assert price_of({"offers": {"price": "12,50"}}) == 12.5
        assert price_of({"offers": {"price": "0"}}) is None
        assert price_of({"offers": {}}) is None

    def test_in_stock(self):
        assert in_stock(ld_products(GRAPH)[0]) is True
        assert in_stock({"offers": {"availability":
                                    "https://schema.org/OutOfStock"}}) is False
        assert in_stock({"offers": {"availability":
                                    "https://schema.org/PreOrder"}}) is True

    def test_offer_handles_list_and_junk(self):
        assert offer({"offers": [{"price": 1.0}]})["price"] == 1.0
        assert offer({"offers": "nonsense"}) == {}
