"""Regression tests for matching — the highest-risk module (a wrong match
means buying the wrong product). Covers the audit's confirmed bugs:
- 10.1: 2XL matched 2XS/"2" at confidence 1.0 (numeric branch ran first)
- 3.1: SET styleIds (one code per garment) matched a single garment's code
"""
from arb.matching import (
    _sizes_equal,
    normalize_style_code,
    size_match_confidence,
    style_code_candidates_from_sku,
    style_ids_match,
)


class TestNormalize:
    def test_strips_separators_and_uppercases(self):
        assert normalize_style_code("dd1391-100") == "DD1391100"
        assert normalize_style_code("DD1391 100") == "DD1391100"
        assert normalize_style_code("hq2010_005") == "HQ2010005"


class TestSkuCandidates:
    def test_ballzy_shoe_sku(self):
        cands = style_code_candidates_from_sku("HQ2010_005_8.5_CNF")
        assert "HQ2010-005" in cands

    def test_waist_length_apparel(self):
        cands = style_code_candidates_from_sku("IJ6884_30_32")
        assert "IJ6884" in cands

    def test_apparel_letter_size(self):
        cands = style_code_candidates_from_sku("HZ0728_L")
        assert "HZ0728" in cands

    def test_empty(self):
        assert style_code_candidates_from_sku("") == []


class TestStyleIdsMatch:
    def test_exact(self):
        assert style_ids_match("DD1391-100", "DD1391-100")

    def test_formatting_variants_of_one_code(self):
        # StockX packs several FORMS of the same code into one field
        assert style_ids_match("DD1391-100", "DD1391-100/DD1391 100")

    SET_TITLE = ("Nike Sportswear Tech Fleece Full-Zip Hoodie & Joggers Set "
                 "Light University Blue")

    def test_set_component_must_not_match(self):
        # audit 3.1: 'FB7921-473/FB8002-473' is a hoodie+joggers SET, one code
        # per garment. The joggers alone must not be priced against the set bid.
        assert not style_ids_match("FB7921-473", "FB7921-473/FB8002-473",
                                   self.SET_TITLE)
        assert not style_ids_match("FB8002-473", "FB7921-473/FB8002-473",
                                   self.SET_TITLE)

    def test_real_alert_that_slipped_through(self):
        """Fired 2026-08-02: Rademar sold the joggers alone, StockX wants the
        pair. The guard was right — the cached match predated it."""
        assert not style_ids_match(
            "BV2671-410", "BV2645-410/BV2671-410",
            "Nike Sportswear Club Fleece Full-Zip Hoodie & Joggers Set "
            "Midnight Navy/Midnight Navy/White")

    def test_full_set_code_still_matches(self):
        # a retailer selling the actual set may match the combined code
        assert style_ids_match("FB7921-473/FB8002-473",
                               "FB7921-473/FB8002-473", self.SET_TITLE)

    def test_dual_coded_single_products_DO_match(self):
        """The first version of this guard keyed on code shape, which describes
        a set AND a product carrying two codes — and so refused 170 legitimate
        products. These are all one item, and a component must match."""
        assert style_ids_match("CW2288-111", "315122-111/CW2288-111",
                               "Nike Air Force 1 Low '07 White")
        assert style_ids_match("1013255", "1013255/1013256",
                               "Birkenstock Boston Soft Footbed Iron Grey")
        assert style_ids_match("IO8116-600", "IO8116-600 / IO8117-600",
                               "Nike Air Zoom GT Cut 4 Kay Yow")

    def test_other_multi_item_phrasings(self):
        for title in ["Nike Tech Fleece Hoodie/Pant Set Royal Blue",
                      "adidas Tiro 2-Piece Tracksuit",
                      "Nike Two-Piece Set Black"]:
            assert not style_ids_match("AA1111-100", "AA1111-100/BB2222-100",
                                       title), title

    def test_no_title_refuses_a_component(self):
        """Without a title there is no way to tell a set from a dual code, and
        the expensive mistake is the set — so decline."""
        assert not style_ids_match("FB7921-473", "FB7921-473/FB8002-473")

    def test_none_and_empty(self):
        assert not style_ids_match("DD1391-100", None)
        assert not style_ids_match("DD1391-100", "")
        assert not style_ids_match("", "DD1391-100")


class TestSizesEqual:
    # audit 10.1 regression: numeric-prefixed apparel sizes must not leak
    # through the numeric branch
    def test_2xl_is_not_2xs(self):
        assert not _sizes_equal("2XL", "2XS")

    def test_2xl_is_not_bare_2(self):
        assert not _sizes_equal("2XL", "2")

    def test_3xl_is_not_bare_3(self):
        assert not _sizes_equal("3XL", "3")

    def test_apparel_synonyms(self):
        assert _sizes_equal("Large", "L")
        assert _sizes_equal("2XL", "XXL")

    def test_different_letters_never_equal(self):
        assert not _sizes_equal("L", "M")
        assert not _sizes_equal("XL", "XXL")
        assert not _sizes_equal("S", "XL")

    def test_apparel_vs_numeric_never_equal(self):
        assert not _sizes_equal("2XL", "44")
        assert not _sizes_equal("44", "2XL")

    def test_numeric(self):
        assert _sizes_equal("EU 44", "44")
        assert _sizes_equal("44,5", "44.5")
        assert not _sizes_equal("44", "44.5")

    def test_adidas_fractions(self):
        # "44 2/3" read as 44 would be a different pair in the box
        assert not _sizes_equal("44 2/3", "44")
        assert _sizes_equal("EU 44 2/3", "44 2/3")
        assert _sizes_equal("44⅔", "44 2/3")

    def test_unrecognised_is_never_a_match(self):
        assert not _sizes_equal("FOO", "BAR")
        assert not _sizes_equal("", "")


class TestSizeMatchConfidence:
    def test_us_agrees_eu_silent(self):
        assert size_match_confidence("8.5", None, "8.5", None) == 1.0

    def test_us_agrees_eu_contradicts_is_review_not_alert(self):
        assert size_match_confidence("8.5", "42", "8.5", "42.5") == 0.5

    def test_us_disagrees(self):
        assert size_match_confidence("8.5", None, "9", None) == 0.0

    def test_eu_only(self):
        assert size_match_confidence(None, "42.5", None, "42.5") == 1.0
        assert size_match_confidence(None, "42.5", None, "43") == 0.0

    def test_nothing_known_never_guesses(self):
        assert size_match_confidence(None, None, None, None) == 0.0
