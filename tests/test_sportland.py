"""Sportland per-size stock resolution.

Sportland's GraphQL returns `variants { attributes }` as null, which an earlier
read took to mean per-size stock was unknowable. It is not: each CHILD product
carries `footwear_size` as a raw option value_index, and
`configurable_options.values` maps value_index -> label. Joining them yields an
exact size -> stock table.

The payload below is the real shape returned for the Nike Lunar Force 1
Duckboot on 2026-08-03 — the product that alerted three times for EU 45.5 while
only EU 42 was actually buyable.
"""
from arb.retailers.sportland import SportlandScraper

LUNAR_FORCE = {
    "sku": "805899_202",
    "name": "NIKE LUNAR FORCE 1 DUCKBOOTS",
    "stock_status": "IN_STOCK",
    "configurable_options": [{
        "attribute_code": "footwear_size",
        "values": [
            {"label": "45", "value_index": 6378},
            {"label": "45.5", "value_index": 6379},
            {"label": "46", "value_index": 6387},
            {"label": "43", "value_index": 6374},
            {"label": "42", "value_index": 6372},
            {"label": "48.5", "value_index": 6391},
            {"label": "42.5", "value_index": 6373},
            {"label": "47", "value_index": 6390},
            {"label": "47.5", "value_index": 6370},
            {"label": "44", "value_index": 6375},
            {"label": "44.5", "value_index": 6377},
        ],
    }],
    "variants": [
        {"product": {"sku": "11207805285", "footwear_size": "6374", "stock_status": "OUT_OF_STOCK"}},
        {"product": {"sku": "11207805290", "footwear_size": "6375", "stock_status": "OUT_OF_STOCK"}},
        {"product": {"sku": "11207805310", "footwear_size": "6387", "stock_status": "OUT_OF_STOCK"}},
        {"product": {"sku": "11207805280", "footwear_size": "6373", "stock_status": "OUT_OF_STOCK"}},
        {"product": {"sku": "11207805300", "footwear_size": "6378", "stock_status": "OUT_OF_STOCK"}},
        {"product": {"sku": "11207805305", "footwear_size": "6379", "stock_status": "OUT_OF_STOCK"}},
        {"product": {"sku": "11207805315", "footwear_size": "6390", "stock_status": "OUT_OF_STOCK"}},
        {"product": {"sku": "11207805330", "footwear_size": "6391", "stock_status": "OUT_OF_STOCK"}},
        {"product": {"sku": "11207805320", "footwear_size": "6370", "stock_status": "OUT_OF_STOCK"}},
        {"product": {"sku": "11207805275", "footwear_size": "6372", "stock_status": "IN_STOCK"}},
        {"product": {"sku": "11207805295", "footwear_size": "6377", "stock_status": "OUT_OF_STOCK"}},
    ],
}


def _sizes(item):
    return SportlandScraper._sizes_with_stock(item)


class TestPerSizeStock:
    def test_only_the_real_in_stock_size_is_marked_buyable(self):
        sizes, unresolved = _sizes(LUNAR_FORCE)
        assert unresolved is False
        buyable = [s.label for s in sizes if s.in_stock]
        assert buyable == ["42"]

    def test_the_size_that_alerted_three_times_is_out_of_stock(self):
        """EU 45.5 carried the best StockX bid (EUR 150-179 vs EUR 10-68 for
        every other size), so it was always the one nominated — while being
        one of the ten sizes that were never purchasable."""
        sizes, _ = _sizes(LUNAR_FORCE)
        row = next(s for s in sizes if s.label == "45.5")
        assert row.in_stock is False

    def test_every_offered_size_is_represented(self):
        sizes, _ = _sizes(LUNAR_FORCE)
        assert len(sizes) == 11
        assert {s.label for s in sizes} == {
            "42", "42.5", "43", "44", "44.5", "45",
            "45.5", "46", "47", "47.5", "48.5"}

    def test_no_size_is_left_unknown_when_the_join_succeeds(self):
        sizes, _ = _sizes(LUNAR_FORCE)
        assert all(s.in_stock is not None for s in sizes)


class TestFallbacks:
    def test_unmappable_variant_flags_unresolved(self):
        """A footwear_size absent from the option map must NOT be guessed at —
        it flags the product back to the product-level caveat instead."""
        item = {
            "configurable_options": LUNAR_FORCE["configurable_options"],
            "variants": [
                {"product": {"sku": "a", "footwear_size": "6372", "stock_status": "IN_STOCK"}},
                {"product": {"sku": "b", "footwear_size": "9999", "stock_status": "IN_STOCK"}},
            ],
        }
        sizes, unresolved = _sizes(item)
        assert unresolved is True
        assert [s.label for s in sizes if s.in_stock] == ["42"]

    def test_no_variants_at_all_falls_back_to_unknown_stock(self):
        item = {"configurable_options": LUNAR_FORCE["configurable_options"],
                "variants": []}
        sizes, unresolved = _sizes(item)
        assert unresolved is True
        assert len(sizes) == 11
        assert all(s.in_stock is None for s in sizes)

    def test_duplicate_label_is_in_stock_if_any_variant_is(self):
        item = {
            "configurable_options": [{
                "attribute_code": "footwear_size",
                "values": [{"label": "42", "value_index": 6372}],
            }],
            "variants": [
                {"product": {"sku": "a", "footwear_size": "6372", "stock_status": "OUT_OF_STOCK"}},
                {"product": {"sku": "b", "footwear_size": "6372", "stock_status": "IN_STOCK"}},
            ],
        }
        sizes, unresolved = _sizes(item)
        assert unresolved is False
        assert sizes[0].in_stock is True

    def test_comma_decimals_are_normalised(self):
        item = {
            "configurable_options": [{
                "attribute_code": "footwear_size",
                "values": [{"label": "44,5", "value_index": 1}],
            }],
            "variants": [
                {"product": {"sku": "a", "footwear_size": "1", "stock_status": "IN_STOCK"}},
            ],
        }
        sizes, _ = _sizes(item)
        assert sizes[0].label == "44.5"
