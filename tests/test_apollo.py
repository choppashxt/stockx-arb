"""apollo.ee scraper + the guards its first live run exposed.

Apollo is primarily a bookshop: ~190k catalog URLs, 243 mentioning lego, of
which only a handful are actual boxed SETS. Everything here defends the line
between a set (has a StockX market) and a Lego picture-book (does not).
"""
from arb.config import RetailerConfig
from arb.retailers.apollo import (
    ApolloScraper,
    _ld_products,
    _offer,
    set_number_from_name,
)
from arb.retailers.base import RetailerScraper
from arb.stockx.catalog import _brands_agree


class TestSetNumber:
    def test_real_set_names(self):
        assert set_number_from_name("LEGO Technic Bugatti Bolide 42151") == "42151"
        assert set_number_from_name(
            "LEGO Star Wars Millennium Falcon 75192") == "75192"
        assert set_number_from_name("lego-botanicals-roosa-lillekimp-10342") == "10342"

    def test_books_have_no_set_number(self):
        """These are real Apollo products — Lego books, no resale market."""
        assert set_number_from_name("Lego City. Päästemeeskonna seiklus") is None
        assert set_number_from_name("LEGO Chima. Ehitusmeister") is None
        assert set_number_from_name("lego-friends-kleepida-on-vahva") is None

    def test_year_is_not_a_set_number(self):
        """The advent calendar below produced code '2025', which matched the
        StockX title '2025 Pokemon Mega Evolution Charizard X ex Ultra-Premium
        Collection' — a EUR 31 calendar priced against a EUR 211 card-box bid.
        Caught on the first live Apollo run, 2026-07-29."""
        assert set_number_from_name(
            "lego-advendikalender-disney-animation-2025") is None
        assert set_number_from_name("LEGO Icons 1998 Retro") is None

    def test_a_real_set_number_still_wins_past_a_year(self):
        assert set_number_from_name("lego-icons-1998-retro-42151") == "42151"

    def test_piece_counts_do_not_displace_the_set_number(self):
        """Set number leads, descriptive numbers trail."""
        assert set_number_from_name(
            "LEGO Technic 42115 Lamborghini Sian 3696 pieces") == "42115"


class TestBrandCorroboration:
    def test_same_brand_agrees(self):
        assert _brands_agree("LEGO", "LEGO") is True
        assert _brands_agree("lego", "LEGO Star Wars") is True

    def test_different_brand_is_the_guard_that_matters(self):
        assert _brands_agree("LEGO", "Pokemon") is False

    def test_missing_brand_declines(self):
        """The title-code path is weaker than a styleId match, so absent
        corroboration it must decline rather than proceed."""
        assert _brands_agree(None, "LEGO") is False
        assert _brands_agree("LEGO", None) is False
        assert _brands_agree("", "LEGO") is False


class TestRobotsEnforcedInCode:
    """Apollo's robots.txt disallows /catalogsearch and friends, but puts
    `Allow: /` first — and urllib's robotparser returns the FIRST matching
    rule, so it would happily permit them. Enforced here instead."""

    def _scraper(self):
        return ApolloScraper(RetailerConfig())

    def test_disallowed_paths_refused(self):
        s = self._scraper()
        for path in ["/en/catalogsearch/result/?q=lego", "/en/cart",
                     "/en/checkout", "/en/customer/account", "/en/wishlist"]:
            assert s._allowed(f"https://www.apollo.ee{path}") is False, path

    def test_product_pages_allowed(self):
        s = self._scraper()
        assert s._allowed(
            "https://www.apollo.ee/en/lego-technic-bugatti-bolide-42151.html")


class TestLdJson:
    HTML = """
    <script type="application/ld+json">{"@type":"BreadcrumbList"}</script>
    <script type="application/ld+json">
    {"@type":"Product","name":"LEGO Technic Bugatti Bolide 42151",
     "category":"Toys > Building sets","sku":"0612345","gtin":"5702017424538",
     "offers":{"@type":"Offer","price":54.99,"priceCurrency":"EUR",
               "availability":"https://schema.org/InStock"}}
    </script>
    """

    def test_extracts_only_product_blocks(self):
        items = _ld_products(self.HTML)
        assert len(items) == 1
        assert items[0]["name"] == "LEGO Technic Bugatti Bolide 42151"

    def test_offer_fields(self):
        offer = _offer(_ld_products(self.HTML)[0])
        assert offer["price"] == 54.99
        assert "InStock" in offer["availability"]

    def test_offer_handles_a_list(self):
        assert _offer({"offers": [{"price": 1.0}]})["price"] == 1.0
        assert _offer({"offers": "nonsense"}) == {}
        assert _offer({}) == {}

    def test_malformed_json_is_skipped_not_raised(self):
        assert _ld_products(
            '<script type="application/ld+json">{not json</script>') == []


class TestRotationSlice:
    """A slice larger than the catalog used to wrap around and return every
    item twice, doubling page fetches for every small catalog."""

    class _Stub(RetailerScraper):
        name = "stub"

        async def scan(self):        # pragma: no cover - interface only
            ...

        async def enrich(self, product):   # pragma: no cover
            ...

    def _stub(self):
        return self._Stub(RetailerConfig())

    def test_slice_bigger_than_catalog_has_no_duplicates(self):
        window = self._stub().rotation_slice(list("abcdefgh"), 100)
        assert window == list("abcdefgh")
        assert len(window) == len(set(window))

    def test_normal_slice_unchanged(self):
        assert self._stub().rotation_slice(list("abcdefgh"), 3) == list("abc")

    def test_empty_catalog(self):
        assert self._stub().rotation_slice([], 100) == []
