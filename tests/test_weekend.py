"""weekend.ee SKU parsing.

weekend prefixes its SKUs with two brand letters in front of the manufacturer
style code, and puts the size after a slash with a comma decimal. Both are
verified against SKUs seen in a live scan on 2026-08-03.
"""
from arb.retailers.weekend import size_from_variant_sku, style_code_from_sku


class TestStyleCodeFromSku:
    def test_brand_prefix_is_stripped(self):
        # all three resolved on StockX at confidence 1.00 in the live run
        assert style_code_from_sku("NIDM0113-100") == "DM0113-100"
        assert style_code_from_sku("NIIQ0886-010") == "IQ0886-010"
        assert style_code_from_sku("AAHQ2010-005") == "HQ2010-005"

    def test_prefix_without_colour_suffix(self):
        assert style_code_from_sku("NIDM0113") == "DM0113"

    def test_unrecognised_shape_passes_through_unchanged(self):
        """A shape we do not understand is handed over as-is rather than
        mangled. Worst case it finds no StockX match, which is safe; a bad
        strip could match the WRONG product, which is not."""
        assert style_code_from_sku("WKMKINK") == "WKMKINK"
        assert style_code_from_sku("NS27023142-PIN") == "NS27023142-PIN"

    def test_lowercase_is_normalised(self):
        assert style_code_from_sku("nidm0113-100") == "DM0113-100"

    def test_empty(self):
        assert style_code_from_sku("") is None
        assert style_code_from_sku("   ") is None


class TestSizeFromVariantSku:
    def test_comma_decimal_becomes_a_dot(self):
        assert size_from_variant_sku("NIDM0113-100/38,5") == "38.5"

    def test_whole_size(self):
        assert size_from_variant_sku("NIDM0113-100/42") == "42"

    def test_no_slash_means_no_size(self):
        assert size_from_variant_sku("NIDM0113-100") is None
        assert size_from_variant_sku("") is None

    def test_non_size_suffix_is_returned_verbatim(self):
        """Gift cards use the same slash shape ('WKMKINK/10EUR'). Nothing here
        pretends it is a shoe size — size matching rejects it downstream."""
        assert size_from_variant_sku("WKMKINK/10EUR") == "10EUR"
