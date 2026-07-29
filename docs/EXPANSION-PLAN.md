# Expansion beyond sneakers — research + plan

Status: **researched, not started.** Queued behind finishing the sneaker side.

Revised 2026-07-29 against `docs/AUDIT-2026-07-28.md` §8. The original draft was
written from live API probes without checking what the code could actually
consume, and it got two things badly wrong (see *Corrections* at the bottom).
Retailer and liquidity measurements from 2026-07-27 are unchanged and still
stand; the engineering claims have been rewritten.

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

## The finding that matters most: no sizes — and we cannot handle that

Every non-sneaker product probed returned **exactly one variant**. Sneakers
returned 25.

The first draft called this a gift: no EU/US conversion, no per-brand men's/
women's/GS differences, no ambiguity guard. That reasoning was about the
*problem*, not about *this codebase*, and the codebase disagrees.

A single-variant product currently yields **zero** opportunities, by construction:

- `arb/scanner.py:229` — `for size in sizes:` is the only place an opportunity
  is ever built. No sizes, no iterations, no output.
- `arb/models.py:126-127` — `Opportunity.size_label` and `us_size` are
  non-optional `str`. A sizeless opportunity cannot even be instantiated.
- `arb/models.py:146` — the dedup key is `retailer|variant_id|size_label`.
  Sizeless rows would collide or need a different key.

This is not theory. **85 single-variant products are already resolved and cached
in `state.db`, and they have produced zero opportunities between them** — not
zero alerts, zero *candidates*. They enter the pipeline and fall out of the size
loop silently.

So the sizeless path is not one of four peer tasks in Phase A. **It is the sole
unblocker for Phases B, C and D, and the largest single item in this plan.**
Nothing downstream can be trialled until it lands.

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
| Collectibles | Bearbrick 1000% Batman | 31% |
| Electronics | Nintendo Switch 2 | 31% |

**Read this carefully.** These are *hyped top hits*, not category averages — the
same trap that made ten sneaker "opportunities" look real before sell-now
gating exposed them as list-ask spreads. Our actual scanned sneaker catalog has
a median bid/ask of ~13%. The honest conclusion is narrower than the table
looks: **cards and watches deserve a real trial; general electronics probably
do not.**

One further caveat added by the audit: the "which retailers actually produce"
signal that shaped this plan was contaminated by the Sportland per-size stock
bug (audit §0.1, fixed 2026-07-28). Category conclusions drawn from
producer-mix before that fix should be re-checked once clean data accumulates.

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
  electronics retailers publish it routinely. **We do not currently have a
  barcode-first path**, contrary to the first draft:
  - `CatalogResolver.resolve_gtin()` exists and is cached, but it is called only
    at `arb/scanner.py:238`, *inside* the size loop and only after a style-code
    resolve has already succeeded. It confirms a size; it cannot find a product.
  - `ean` lives on `RetailSize` (`models.py:21`), not on `Product`. A sizeless
    product has nowhere to carry a barcode.
  - `resolve_gtin` returns a variant with no title or URL, while `Opportunity`
    needs a full `StockXProduct`.
  - `StockXClient.get_product` (`client.py:222`), which would fill that gap, has
    never been called by anything.

  Building barcode-first resolution is real work, not a wiring change. It is a
  prerequisite for Phase D, not an existing asset.
- **Lego set number** (e.g. 75192) — clean and unambiguous *in retailer
  listings*. Whether StockX exposes it as the `styleId` we match on is
  **unverified** — see Phase B.
- **MPN** — Apple `MWW43AM/A`, Dyson `310731-01`. Reliable when published.
- **Card products** — set name + product form (booster box / ETB / bundle) +
  language. Fuzzy; would need the review queue rather than direct alerts — and
  the review queue is not yet readable (see Phase C).

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

Cloudflare-blocked sites stay blocked. Per the project guardrail, we do not add
anti-detection, proxy rotation or CAPTCHA handling to get past them.

## Proposed phases (after sneakers)

### Phase 0-E — the one probe that should happen first (one API call)

Search `/catalog/search` for a known Lego set number (e.g. `75192`) and read
back the `styleId` of the top hit. `arb/stockx/catalog.py:121` compares
`styleId` and nothing else, so this single call decides whether Phase B has a
usable key at all — and therefore whether the whole non-sneaker branch starts
with Lego or with something else. Cheapest de-risking action in this document;
do it before committing to Phase A's shape.

### Phase A — make the core category-agnostic

In dependency order, not in parallel:

1. **Regression suite first.** Landed 2026-07-28 (`tests/`, 68 tests). Note the
   original acceptance criterion — "re-run the sneaker retailers unchanged" —
   is not sufficient on its own: `arb selftest` asserts a size-bearing sneaker
   fixture (`cli.py:146,151`) and would keep passing while the sizeless path
   remained entirely broken. **Phase A needs a sizeless fixture that fails
   today**, added before the refactor.
2. **Sizeless opportunity path.** The real work: opportunity generation outside
   the size loop, `size_label`/`us_size` optional on `Opportunity`, a dedup key
   that is stable without a size, and alert formatting that omits the size line.
3. **`category` on `Product`, `match_key` + `key_type`** (style_code / gtin /
   lego_set / mpn) — added *with* their consumers, not as write-only fields.
   The codebase already carries dead fields of exactly this kind
   (`product_type`, `pickup_available`); do not add more.
4. **Per-category profit settings — dropped from the plan entirely.** Resolved
   2026-07-29: StockX fees do **not** vary by category (see *Fees*). One fee
   model covers sneakers, streetwear, collectibles, cards and electronics. This
   was a whole workstream that turned out not to exist.

No new retailers in this phase.

### Phase B — Lego via Apollo + Rahva Raamat

Gated on Phase 0-E returning a usable key. If set numbers do resolve, this is
the cleanest possible start: single variant, no region trap, no authentication
subtlety beyond "sealed", both retailers Estonian with local returns. Validates
the category-agnostic core at the lowest risk of a wrong match.

### Phase C — watches

Promoted into the sequence: **88% bid/ask is the best number measured anywhere
in this research**, and the first draft endorsed a trial in prose while giving
it no phase. Single variant, sealed/new condition, and G-Shock-class items sit
at a capital level closer to sneakers than to a PS5. Retail sourcing in Estonia
needs a probe — this is the main open question, not the liquidity.

### Phase D — trading cards

Best-but-one measured liquidity (85%) with the messiest matching. Gate hard on
language and product form.

**Hard prerequisite:** the plan routes ambiguous matches to the review queue,
but `db.add_review` is write-only — nothing reads it, there is no `arb review`
subcommand, no code path sets `resolved=1`, and **931 rows now sit untouched**.
Routing to it today is routing to `/dev/null`, and auto-alerting instead would
break the README's "never fuzzy-alert" rule. **Build `arb review` first, or
accept that Phase C is permanently no-alert.**

### Phase E — electronics, only if D proves out

Weakest liquidity outside hype items, plus the plug/region trap. Would use
klick.ee and euronics.ee via EAN matching — which requires the barcode-first
path built first (see *Matching keys*). Must refuse to alert whenever the
StockX title carries a region marker the retail listing cannot confirm.

Capital per unit is **no longer a gate** (decided 2026-07-29: any unit price is
acceptable if the flip is profitable). The remaining objections are unchanged
and are about *probability*, not size: electronics depreciate while listed, and
a region mismatch is invisible in the product title.

### Streetwear — in scope

Decided 2026-07-29: **streetwear is in.** The first draft listed it as "not
planned" because Supreme has no Estonian retail presence, while 876 streetwear
products were already resolved and cached anyway — nothing filters on
`product_type` (set at `catalog.py:53`, never read).

That accidental state is now the intended one, so no filter is added and no
budget is reclaimed. Two consequences worth tracking:

- Apparel carries letter sizes (S/M/L/XL/2XL), which have their own matching
  hazards — the `2XL` vs `2XS` collision fixed on 2026-07-28 was found in
  exactly this path. Apparel matching deserves stricter scrutiny than sneakers,
  not less.
- Multi-garment sets ("Hoodie & Joggers Set") share a `styleId` with their
  components and were mispricing single garments against set-level bids. Guarded
  2026-07-28; keep the guard in mind when adding apparel retailers.

**Still not planned:** handbags — authentication risk, no Estonian retail source
at a discount.

## Fees — resolved 2026-07-29

Looked up against `stockx.com/help` (Verified Marketplace, seller account is
**Level 1**):

| Level | Transaction fee | Quarterly requirement |
|---|---|---|
| **1** | **9.0%** | none |
| 2 | 8.5% | 12 sales or $1,500 |
| 3 | 8.0% | 40 sales or $5,000 |
| 4 | 7.5% | 200 sales or $25,000 |
| 5 | 7.0% | 800 sales or $100,000 |

Plus **3% payment processing** at every level, and a **minimum seller fee of
€5.00** in the EU.

**Fees do not vary by category.** Sneakers, streetwear, collectibles, trading
cards and electronics all pay the same. This kills the per-category fee
workstream in Phase A outright.

Two corrections to the scanner followed from this lookup:

1. `transaction_fee_pct` was **9.5%**, not 9.0% — the model was overcharging
   itself 0.5% of every sale price and hiding marginal opportunities.
2. The **€5.00 minimum was not modelled at all.** Below a ~€55.56 sale price the
   percentage fee is smaller than the floor, so cheap flips were scored too
   favourably — the direction that invents opportunities rather than hiding
   them. Now applied to the transaction fee.

Net effect: above a €52.63 sale price profit estimates rise slightly (+€1.00 on
a €200 bid); below it they fall (−€1.20 on a €40 bid).

Levels are assessed quarterly and the benefit persists for the current and
following quarter, so `transaction_fee_pct` needs revisiting if volume ever
reaches 12 sales / $1,500 in a quarter.

## Open questions for the human

- Do you want cards at all, given they need sealed storage, have the fuzziest
  matching, and require `arb review` to exist first?

*Answered 2026-07-29:* unit price is not a constraint provided the flip is
profitable (Phase E capital gate removed); streetwear is in scope; StockX fee
tier confirmed uniform across categories at Level 1 = 9% + 3%.

## Corrections applied 2026-07-29

Against `docs/AUDIT-2026-07-28.md` §8, re-verified by hand before rewriting:

| Original claim | Status |
|---|---|
| "Single-variant products delete the hardest part" | **Inverted.** Sizeless yields zero opportunities; 85 cached, none produced. Now the headline blocker |
| "We already have barcode-first resolution working" | **False.** `resolve_gtin` is size-loop-only; `Product` has no `ean`; `get_product` never called |
| Phase A acceptance = "re-run sneakers unchanged" | **Insufficient.** Selftest asserts a sneaker fixture and would pass with sizeless still broken; needs a failing sizeless fixture |
| Phase B "set numbers are unambiguous keys" | **Unverified** for StockX `styleId`. Promoted to a one-call probe, Phase 0-E |
| Phase C "route ambiguous to review queue" | **Dead end.** 931 unread rows, no `arb review`. Now a hard prerequisite |
| Phase A per-category fees | **Dropped.** Fee lookup 2026-07-29 shows fees are uniform across categories — the workstream did not exist |
| Watches (88%, best measured) | **Added as a phase**; previously endorsed in prose but sequenced nowhere |
| "Not planned: streetwear" | **876 already cached and unfiltered.** Now in scope by decision |
| Level 1 fee assumed 9.5% | **9.0%.** Config corrected; the scanner was understating every profit by 0.5% of sale price |
| EU €5.00 minimum seller fee | **Was not modelled.** Cheap flips (sale < €55.56) were scored too favourably; now floored |
