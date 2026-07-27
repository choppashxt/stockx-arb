# Expansion beyond sneakers — research + plan

Status: **researched, not started.** Queued behind finishing the sneaker side.
All figures below were measured against the live StockX API and live retailer
sites on 2026-07-27, not assumed.

## What StockX actually carries

Probing `/catalog/search` returns these `productType` values:

| productType | What lands in it |
|---|---|
| `sneakers` | current scope |
| `streetwear` | Supreme, hyped apparel |
| `collectibles` | **electronics, trading cards, Lego, Bearbrick, Funko — all of it** |
| `handbags` | Goyard, LV, etc. |
| `watches` | G-Shock, Seiko, luxury |

Note `collectibles` is a catch-all: a PlayStation 5, a Pokémon booster box and a
Lego UCS set all report the same productType. Category has to be inferred from
the title/brand, not from this field.

## The finding that matters most: no sizes

Every non-sneaker product probed returned **exactly one variant**. Sneakers
returned 25.

That deletes the hardest and riskiest part of the existing bot. No EU/US
conversion tables, no per-brand men's/women's/GS differences, no ambiguity
guard, no barcode-for-size lookups. Matching collapses from *"which of 25
variants is this shoe?"* to *"is this the same product?"*.

## Bid liquidity by category

Sell-now needs a **live bid**, so this is the deciding metric. Median highest
bid as a share of lowest ask, for the top search hit in each category:

| Category | Example | Bid / ask |
|---|---|---|
| Watches | Casio G-Shock x Pokémon | **88%** |
| Trading cards | Pokémon Mega Evolution booster box | **85%** |
| Sneakers (hyped) | Travis Scott Jordan 1 Low | 82% |
| Electronics | PS5 Pro 30th Anniversary | 60% |
| Collectibles | Lego UCS Millennium Falcon | 56% |
| Streetwear | Supreme Box Logo hoodie | 48% |
| Handbags | Goyard Cisalpin backpack | 48% |
| Electronics | AirPods Max (2024) | 36% |
| Electronics | Nintendo Switch 2 | 31% |
| Collectibles | Bearbrick 1000% Batman | 31% |

**Read this carefully.** These are *hyped top hits*, not category averages — the
same trap that made ten sneaker "opportunities" look real before sell-now
gating exposed them as list-ask spreads. Our actual scanned sneaker catalog has
a median bid/ask of ~13%. The honest conclusion is narrower than the table
looks: **cards and watches deserve a real trial; general electronics probably
do not.**

## Risks specific to these categories

1. **Region variants are the new "wrong size".** StockX titles include
   `(US Plug)` and `(220-240V US Plug)` — Nintendo Switch 2 and Dyson Airwrap
   both did. A EU-plug unit bought in Estonia is **not** the variant with the
   bid on it. This is harder to catch than a wrong shoe size because the
   product name looks identical. Any electronics scraper must treat plug/region
   as part of the match key or refuse to alert.
2. **Sealed-condition requirements.** StockX verifies collectibles as sealed and
   unopened; a returned/opened retail unit fails authentication.
3. **Language/region editions** for trading cards (EN vs JP vs KR print runs)
   are separate products with separate markets.
4. **Higher capital per unit.** A PS5 or a UCS Lego set ties up far more cash
   per flip than a €70 sneaker, so a bad match costs proportionally more.
5. **Electronics depreciate** while listed; cards and sealed Lego generally do
   not.

## Matching keys (per category)

The style-code equivalent, in descending reliability:

- **EAN/UPC barcode** — the natural key for electronics and Lego, and Estonian
  electronics retailers publish it routinely. **We already have this working**:
  `CatalogResolver.resolve_gtin()` hits
  `/catalog/products/variants/gtins/{gtin}` and is cached. For single-variant
  products a barcode resolves the entire match in one call.
- **Lego set number** (e.g. 75192) — clean, unambiguous, in every listing.
- **MPN** — Apple `MWW43AM/A`, Dyson `310731-01`. Reliable when published.
- **Card products** — set name + product form (booster box / ETB / bundle) +
  language. Fuzzy; would need the review queue rather than direct alerts.

## Estonian retailer landscape (probed)

| Retailer | Category | Technical state |
|---|---|---|
| klick.ee | electronics | Magento, robots Crawl-delay 2, 2 sitemaps — **best electronics candidate** |
| euronics.ee | electronics | plain HTML, 1 sitemap — viable |
| apollo.ee | Lego, toys, books | Magento + Next, 4 sitemaps — **best Lego/collectibles candidate** |
| rahvaraamat.ee | Lego, toys, books | Next.js, 1 sitemap — viable |
| photopoint.ee | electronics/photo | robots Crawl-delay 10 — viable but slow |
| lauamangud.ee | board games / TCG | no robots.txt, small plain site — likely viable, check ToS |
| arvutitark.ee | electronics | Cloudflare block |
| 1a.ee (Kaup24) | electronics | Cloudflare block |
| hansapost.ee | electronics | Cloudflare block |
| power.ee | electronics | Cloudflare block |

All the viable ones are Estonian-domiciled, so shipping and returns are local —
no reshipping cost to model, unlike Overkill or Sportland LT.

## Proposed phases (after sneakers)

**Phase A — make the core category-agnostic.**
The pipeline currently assumes a shoe. Needed: a `category` field on `Product`;
make `sizes[]` optional so single-variant products skip size matching entirely;
generalise `style_code` to a `match_key` with a `key_type`
(style_code / gtin / lego_set / mpn); per-category profit settings, since
StockX fee tiers and shipping costs differ by category (a Lego UCS box is not a
€7 shoe label). No new retailers in this phase — prove it by re-running the
sneaker retailers unchanged.

**Phase B — Lego via Apollo + Rahva Raamat.**
The cleanest possible start: set numbers are unambiguous keys, single variant,
no region trap, no authentication subtlety beyond "sealed", and both retailers
are Estonian with local returns. This validates the category-agnostic core with
the lowest risk of a wrong match.

**Phase C — trading cards.**
Best measured liquidity (85%) but the messiest matching. Gate hard on language
and product form; route anything ambiguous to the review queue. Start with
lauamangud.ee / Apollo, plus a check on whether Estonian TCG specialists are
worth adding.

**Phase D — electronics, only if C proves out.**
Highest capital, weakest liquidity outside hype items, and the plug/region trap.
Would use klick.ee and euronics.ee via **EAN matching**, and must refuse to
alert whenever the StockX title carries a region marker that the retail listing
cannot confirm.

**Not planned:** handbags (authentication risk, no Estonian retail source at a
discount) and general streetwear (Supreme has no Estonian retail presence).

## Open questions for the human

- Are you willing to hold higher-value single units (a €400 Lego set, a €600
  console) versus €70 sneakers? That decides whether D is worth attempting.
- StockX seller fee tier for collectibles — confirm whether it differs from the
  9.5% + 3% used for sneakers, since profit maths depends on it.
- Do you want cards at all, given they need sealed storage and have the
  fuzziest matching?
