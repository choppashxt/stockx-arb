from arb.models import RetailSize, StockXVariant
from arb.scanner import _match_retail_size


def _variant(us: str, eu: str) -> StockXVariant:
    return StockXVariant(
        product_id="p", variant_id=f"v-{us}", size=us,
        conversions={"eu": eu},
    )


def test_us_label_is_not_also_treated_as_eu() -> None:
    variant, confidence = _match_retail_size(
        RetailSize(label="8", system="US", us_size="8"),
        [_variant("8", "41")],
    )
    assert variant is not None and variant.variant_id == "v-8"
    assert confidence == 1.0


def test_eu_label_can_cross_check_explicit_us_size() -> None:
    variant, confidence = _match_retail_size(
        RetailSize(label="42", system="EU", us_size="8.5"),
        [_variant("8.5", "42")],
    )
    assert variant is not None and variant.variant_id == "v-8.5"
    assert confidence == 1.0


def test_unknown_system_without_explicit_conversion_is_not_guessed() -> None:
    variant, confidence = _match_retail_size(
        RetailSize(label="8", system="UK", us_size=None),
        [_variant("9", "42")],
    )
    assert variant is None
    assert confidence == 0.0
