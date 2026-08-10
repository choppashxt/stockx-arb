# stockx-arb — structure reference

Written for another LLM (or a human) picking this project up cold. Describes
what exists and how it fits together as of commit `ebe70fd` (2026-08-03).
For history/rationale behind specific decisions, `git log` is authoritative —
this file describes current shape, not how it got here.

---

## 1. What this is, in one paragraph

A read-only price scanner. It scrapes ~13 European retail sites, resolves each
product to its StockX catalog entry, pulls StockX's live bid/ask, computes
what you'd actually net after fees if you bought the item at retail and sold
it immediately on StockX, and sends a Discord alert when that net profit
clears a threshold. **It never buys, lists, or transacts anything — every
trade is executed by a human.** That constraint is enforced by omission: there
is no code path anywhere that places an order.

---

## 2. Directory map

```
arb/
  cli.py            entry point: scan / auth / selftest / resolve / status /
                     report / review / watch / sold / dashboard
  config.py         AppConfig (config.yaml) + Secrets (.env), both pydantic
  models.py         Product, RetailSize, StockXProduct, StockXVariant,
                     MarketData, ProfitBreakdown, Opportunity — everything
                     downstream of a scraper speaks these types
  scanner.py        the pipeline: scrape -> resolve -> market data -> profit
                     -> filter/rank -> enrich-confirm -> dedup -> alert
  matching.py       style-code normalization, style-code vs styleId matching,
                     title-code matching, region-marker matching, size matching
  profit.py         fee math, StockX + Alias breakeven, opportunity scoring
  notify.py         alert formatting + Discord/Telegram/Console senders
  db.py             SQLite schema + all queries (one file, no ORM)
  fixtures.py       synthetic data for `selftest` and the fixture market provider
  dashboard.py      stdlib http.server dashboard at 127.0.0.1:8787 (read-only)

  stockx/
    client.py       StockXClient — thin typed wrapper over the v2 REST API,
                     cross-process rate limiting, retry/backoff, budget tracking
    auth.py         TokenManager — OAuth refresh, mutex-guarded
    authorize.py    one-time interactive OAuth authorization-code flow (arb auth)
    catalog.py      CatalogResolver — retail code/barcode -> StockX product,
                     with an on-disk cache and cache self-healing (see §7)
    market.py       MarketDataProvider interface: StockXOfficialProvider
                     (live), FixtureProvider (offline/selftest); dynamic
                     snapshot caching lives in scanner.py

  retailers/
    base.py         RetailerScraper ABC + PoliteFetcher (robots.txt, per-host
                     delay+jitter, circuit breaker, honest User-Agent)
    ldjson.py       shared schema.org ld+json extraction (apollo, klick)
    __init__.py     the retailer registry (name -> scraper class)
    ballzy.py, reede.py, teamsport.py, rademar.py, sportland.py, sns.py,
    overkill.py, footshop.py, weekend.py, apollo.py, klick.py
                     one module per retailer, see §5

tests/              147 pytest tests, no network, run in ~2s
docs/
  AUDIT-2026-07-28.md      full audit: findings, severity, fixes, roadmap
  EXPANSION-PLAN.md        non-sneaker category research + corrected phasing
  STRUCTURE.md             this file

config.yaml         all tunables (fees, filters, per-retailer settings)
.env / .env.example credentials (never committed; .env is gitignored)
state.db            SQLite (gitignored) — the only stateful artifact
run_scanner.bat, run_dashboard.bat   Windows supervisor loops (restart on exit)
```

---

## 3. Data flow, end to end

```
RetailerScraper.scan()
    -> list[Product]                              (grid-level; price_verified=False)
        |
        v
run_scan(): stamp per-retailer policy (discount_pct, sale_discount_pct,
            extra_cost_eur, buy_note) onto each Product
        |
        v
db.upsert_retail_product() for every product       (detects new/restocked/price-dropped)
        |
        v
_cheap_screen(): in_stock, has style_code OR ean, price <= max_retail_price_eur, EUR
        |
        v
_evaluate_product()  (per candidate product)
    1. resolve style_code -> StockXProduct        (CatalogResolver.resolve)
       fallback: resolve by EAN if no style_code match (resolve_by_gtin)
    2. fetch/​cache StockX variants for that product
    3. get market data for all variants (cached snapshot, or one live API call,
       TTL decided by _market_ttl_minutes — see §6)
    4. compute best possible profit across all variants (_best_upside);
       if it clears min_profit_eur but came from a CACHED snapshot, force one
       live re-check before trusting it (never alert off a stale bid)
    5. if nothing clears the floor at product level, stop here (cheap; most
       products die at this line before any size-matching work happens)
    6. if not price_verified: call scraper.enrich() to get real per-size
       stock / retailer's own US sizes / true page price BEFORE size matching
       (the category grid usually only has EU labels)
    7a. SIZELESS branch: if StockX has exactly one variant with no size value
        (Lego/cards/electronics/watches), emit ONE product-level Opportunity.
        Barcode cross-check + region-marker gate apply here (see §7).
    7b. SIZED branch: for each RetailSize, match to a StockX variant (barcode
        first if the retailer publishes an EAN per size, else via
        size_match_confidence), build an Opportunity if profit clears the
        floor and liquidity floors pass
    8. if size_stock_unverified (retailer doesn't expose real per-size stock,
       e.g. Sportland), collapse all per-size Opportunities into ONE
       product-level Opportunity keyed "...|unverified", scored on the best one
        |
        v
_maybe_alert()  (per Opportunity)
    - attach a "buy here instead" note if a preferred sibling store also has it
    - db.should_alert(): new / restocked / profit moved >= re_alert_profit_delta_eur
      / else "duplicate" -> skip
    - re-confirm on the product page if not already price_verified
    - notifier.send() — ONLY on a confirmed-delivered send does record_alert()
      run, so a failed send never permanently suppresses a real opportunity
        |
        v
Discord (or Telegram, or console in --dry-run)
```

`run_loop()` runs every enabled retailer as its own asyncio task on its own
`scan_interval_minutes`; retailers do not block each other.

---

## 4. Core types (`models.py`)

- **`Product`** — one retail listing. Key fields: `retailer`, `style_code`,
  `retailer_sku` (only needed when it differs from `style_code` — see weekend),
  `category`, `ean` (product-level barcode for sizeless items), `price`,
  `sizes: list[RetailSize]`, `in_stock`, `price_verified`,
  `size_stock_unverified`, `discount_pct`/`sale_discount_pct`/`extra_cost_eur`
  (landed-cost inputs). `.landed_cost` is the ONE place cost math happens —
  every profit calculation goes through it, so a promo can never silently
  apply to full-price stock.
- **`RetailSize`** — one size row: `label`, `us_size` (only if derivable
  without guessing), `ean` (per-size barcode), `in_stock: Optional[bool]`
  (`None` = retailer doesn't expose it — **never fabricate `True`**).
- **`StockXProduct`** / **`StockXVariant`** — as returned by the API,
  normalized. `StockXProduct.style_id` can be empty string (non-sneaker
  categories), multi-code (`"A/B"`), or a single code.
- **`MarketData`** — one variant's bid/ask snapshot. Every field the official
  API can't supply stays `None`, never `0` — consumers must treat `None` as
  "unknown."
- **`Opportunity`** — the alertable unit. `size_label`/`us_size` are
  `Optional[str]`, both `None` for a sizeless product (`.sizeless` property).
  `.key` is the dedup identity (`retailer|variant_id[|size_label]`, or
  `key_override` for the unverified-stock collapse). `.landed_cost` proxies to
  `retail.landed_cost`.

---

## 5. Retailers (`arb/retailers/`)

Every scraper implements `RetailerScraper`:

```python
class RetailerScraper(ABC):
    async def scan(self) -> list[Product]: ...      # cheap grid sweep
    async def enrich(self, product: Product) -> Optional[Product]: ...  # authoritative page
```

`PoliteFetcher` (in `base.py`) is shared infrastructure: fetches and honors
`robots.txt` per host (a robots.txt that 401/403s counts as disallow-all), a
per-host delay = `max(configured delay, robots Crawl-delay)` with jitter, an
honest User-Agent with a contact address, and a circuit breaker (5 consecutive
failures -> give up on that host for the run). **No anti-detection of any
kind** — no CAPTCHA solving, no proxy rotation, no header spoofing to evade
bot walls. Sites that block plain clients are simply left out.

| Retailer | Mechanism | Notes |
|---|---|---|
| `ballzy` | category grid HTML + size chart | serves EE/LV/LT/FI/SE storefronts |
| `reede` | category grid | |
| `teamsport` | category grid | registered-customer discount modeled via `discount_pct` |
| `rademar` | category grid | |
| `sportland` / `sportland_lt` | Magento GraphQL over `GET ?query=` | child-variant `footwear_size` joins provide verified per-size stock; unresolved joins fall back to unknown rather than guessed stock (`sportland_lv` is intentionally disabled) |
| `weekend` | Magento GraphQL over `GET ?query=` | **disabled**: exact honest requests return 200 standalone but receive a Cloudflare challenge in the shared scanner even after conservative concurrency/quiet-window experiments; no evasion attempted |
| `sns` | Shopify, ld+json + GTINs | |
| `overkill` | Shopify | ships to EE, `extra_cost_eur` models the forwarding cost |
| `footshop` | microdata | `size_stock_unverified=True` |
| `apollo` | Magento sitemaps + schema.org ld+json (`ldjson.py`) | **Lego sets**, sizeless; catalog is ~190k URLs and mostly a bookshop, filtered to slugs containing a real (non-year) set number before any page fetch |
| `klick` | Magento sitemaps + ld+json (`ldjson.py`) | **electronics**, sizeless, matched by **barcode only** (no MPN/styleId available for this category); most candidates get refused by the region-marker gate, which is correct |

Retailers not integrated, with the evidence for why:
- **euronics.ee** — sitemap has no product URLs, only Content/Group/SubGroup type pages
- **rahvaraamat.ee** — 880k URLs, a bookshop; "lego" hits are illustrator surnames (Katlego, Gallego)
- **lauamangud.ee** / trading cards generally — StockX card titles carry no code at all (verified via live probe), so matching would depend entirely on the review queue, which nothing currently reads (`arb review` doesn't exist — see §9)
- **sportsdirect.ee, stadium.fi** — block plain HTTP clients (bot wall); skipped per the no-anti-detection rule
- **GOAT/Alias** — no public API; every attempted workaround (paid reverse-engineered wrapper, direct Algolia frontend key, a "permission" email of dubious provenance) was declined as unauthorized access to their infrastructure. Only the alert-time breakeven line is implemented (see §8); real integration requires GOAT issuing actual API credentials.

---

## 6. StockX integration (`arb/stockx/`)

- **`client.py` — `StockXClient`**: typed wrappers over
  `/catalog/search`, `/catalog/products/{id}`, `/catalog/products/{id}/variants`,
  `/catalog/products/{id}/market-data`, `/catalog/products/variants/gtins/{gtin}`.
  - **`CrossProcessThrottle`**: a lease file (`.stockx_ratelimit`, exclusive
    lock, Windows `msvcrt` / POSIX `flock`) enforces the 1 req/s limit across
    *all* local processes — `arb scan` and `arb report` running simultaneously
    would otherwise double the effective rate.
  - Retries on 429/408/5xx with `_retry_after_seconds()` parsing both RFC 9110
    forms (seconds or HTTP-date), capped, used as a backoff floor.
  - `BudgetExhausted` / `AccountNotReady` / `StockXAPIError` — the scan loop
    treats budget exhaustion as "stop this scan," account-not-ready as "log
    and stop" (StockX 400s on market-data until billing/shipping are set on
    the account), and other API errors as "skip this one product."
- **`auth.py` — `TokenManager`**: OAuth token cache + refresh behind an
  `asyncio.Lock` with double-checked read, so N concurrent callers on token
  expiry produce exactly one refresh POST, not N. Refresh POSTs count against
  the daily request budget.
- **`catalog.py` — `CatalogResolver`**: see §7, the most subtle module in the
  project.
- **`market.py`**: `MarketDataProvider` interface. `StockXOfficialProvider` is
  the live one (one API call = every variant of a product). `FixtureProvider`
  backs `selftest`. Dynamic snapshot caching stays in `scanner.py`, where the
  hot/warm/cold TTL and mandatory live confirmation can be applied together.

**Tiered re-check budget** (`_market_ttl_minutes` in `scanner.py`, driven by
`sku_watch` table): a product that already clears the profit floor, or is
within `near_miss_eur` of it, gets checked every `refresh_minutes_hot`/`_warm`
minutes; everything else — including anything that has literally no live bid
on any variant — only once a day (`refresh_minutes_cold`). Without the
NULL-bid distinction, ~50% of the daily API budget was being spent
re-refreshing shoes that could structurally never alert (`require_live_bid`
with no bid = never).

---

## 7. Matching — the correctness-critical module (`matching.py` + `catalog.py`)

Matching bugs are the most dangerous class of bug here: a wrong match means a
real purchase decision on wrong information. Every rule below exists because
a *specific* wrong alert or wrong refusal happened first, and the fix is
documented inline in the code with the real example. Read the comments — the
docstrings are load-bearing.

**Resolution order in `CatalogResolver._resolve_uncached`:**
1. exact `style_ids_match(candidate, hit.styleId, hit.title)` — confidence
   1.0 (primary candidate) or 0.95 (a size-stripped secondary candidate)
2. if the hit's `styleId` is EMPTY (all non-sneaker categories): `code_in_title`
   + brand corroboration (`_brands_agree`) — same confidence tiers
3. fuzzy brand+name token-overlap fallback — capped at confidence 0.5, which
   is always below `alert_min_confidence` (0.90 default), so it can only ever
   reach the review queue, never an alert

**`style_ids_match(code, styleId, title=None)`** — StockX packs multiple codes
into one `styleId` field for two *opposite* reasons that must be told apart:
- one product with two codes (a reissue, a width variant, formatting noise) —
  a single component SHOULD match
- a multi-item SET, one code per garment — a single component must NOT match,
  or a lone pair of joggers gets priced against the whole set's bid

  The distinguishing signal is the **title** (`is_multi_item_title`: "Set",
  "N-Piece", "Two-Piece"), not the code shape — an earlier version keyed on
  code shape alone and silently refused ~150 legitimate dual-coded products
  as collateral damage from fixing the set case. Without a title, the rule
  falls back to requiring the full combined code (safe but conservative).

**`code_in_title(code, title)`** — used only when `styleId` is empty. Boundary
-checked (a bare substring test would let `"5192"` match `"715192"`), and
requires ≥4 characters (shorter codes collide with years/counts too easily).

**`region_mismatch(stockx_title, retail_name)`** — StockX sells `(US Plug)` /
`(EU Plug)` electronics as separate products with near-identical titles. If
StockX pins a region the retail listing doesn't confirm, refuse — silence from
an Estonian retailer is not evidence of agreement.

**Cache self-healing** (`CatalogResolver._cached_match_still_valid`): matching
*rules* change over time (the SET fix above is a real example), but
*resolutions* are cached indefinitely once positive. Without re-validation on
read, a rule fix only ever protects codes nobody had looked up yet — a stale
cached match from before the fix landed re-alerted for days. Every cached
exact match (confidence ≥0.95) is now re-checked against current
`style_ids_match`/`code_in_title` on every read; a row that fails is dropped
and re-resolved. Fuzzy (0.5) hits are exempt — they never satisfied the exact
check by definition, so re-validating them would just burn a resolution
re-checking them forever.

**Barcode matching** (strongest signal, used wherever available):
- `resolve_gtin(gtin) -> StockXVariant` — barcode to exact variant. Used
  per-size when a `RetailSize.ean` is present, cross-checking that the
  variant's `product_id` matches the style-code-resolved product (a mismatch
  blocks the alert and files a review row — a wrong barcode resolves to a
  *different real product*, not to nothing).
- `resolve_by_gtin(gtin) -> (StockXProduct, confidence)` — barcode to a FULL
  product (two calls: gtin lookup + `get_product`). This is the fallback used
  when there's no style-code match at all (electronics, which have no
  recognizable code anywhere).

**Size matching** (`size_match_confidence`, `_sizes_equal`): never guesses.
Apparel letter sizes are checked BEFORE numeric parsing (`_num("2XL")` would
otherwise read as `2`, falsely equating 2XL/2XS/"2"). adidas fractional EU
sizes (`"44 2/3"`) and unicode fraction glyphs are parsed explicitly. Any
result below full confidence goes to the review queue, never an alert.

---

## 8. Profit math (`profit.py`)

`breakdown()` computes one scenario (sell-now on the bid, or list-at-ask):
`transaction_fee = max(price * 9%, €5 minimum)` + `processing_fee (3%)` +
`shipping_to_stockx_eur` + `vat_wedge` (no-op unless VAT is enabled) all
subtracted from sale price = `payout`; `payout - landed_cost = profit`.
StockX fees are Level 1 (9% transaction, verified against stockx.com/help
2026-07-29 — do not assume the old 9.5% figure) and are IDENTICAL across
every product category.

`_gate_profit()` (in `scanner.py`) decides which number the filter and the
score actually judge: under `require_live_bid` (default True), that's the
sell-now profit only — an ask-only spread never alerts, however wide.

`opportunity_score()` must be called with this SAME gated figure, not
`best_profit` (which under `require_live_bid` is usually the untakeable
list-ask number) — an earlier version scored on `best_profit` and every
stored score turned out to be exactly `2 x list_ask.profit`.

Alias/GOAT has no data integration (see §5); `alias_breakeven_price()`
computes the sale price Alias would need to beat the StockX payout, shown on
every alert as a link to check by hand.

---

## 9. What's NOT implemented (known gaps)

- **Per-category fee differentiation** — deliberately NOT built. Fee lookup
  confirmed StockX charges identically across sneakers/streetwear/
  collectibles/electronics, so this was correctly descoped rather than left
  undone.
- **GOAT/Alias live market data** — no legitimate path found yet; needs GOAT
  to issue real API credentials (an email requesting this is drafted/sent;
  outcome pending as of this writing).
- **Review decisions do not create matching rules automatically.** `arb review`
  now lists, filters and closes rows, but promoting a reviewed match still
  requires an explicit code/data rule so low-confidence items never alert by
  accident.

`docs/AUDIT-2026-07-28.md` has the full original findings list (most Phase-0
items from it are now fixed; check git log for which). `docs/EXPANSION-PLAN.md`
has the measured (not guessed) liquidity/sourcing evidence behind every
non-sneaker category decision.

---

## 10. Design invariants (do not casually change these)

1. **Alert-only.** No code path may place an order, submit a listing, or
   automate checkout. This is a hobby scanner; the human decides and acts.
2. **No anti-detection.** robots.txt is authoritative. No CAPTCHA solving, no
   proxy rotation, no header spoofing to defeat bot protection, no reuse of a
   platform's private/internal API via extracted frontend credentials (this
   has been proposed and declined multiple times for GOAT specifically).
3. **Never guess a size or a match.** Anything below full confidence goes to
   the review queue, never an alert. A wrong match means a real wrong
   purchase.
4. **Sell-now gating.** `require_live_bid=True` is the default and the
   intended mode — profit is judged on what you could actually get paid
   *right now*, not on a list-and-wait spread that may never fill.
5. **A failed alert send must never be recorded as sent.** `record_alert()`
   only runs after `notifier.send()` returns `True`; otherwise the
   opportunity is free to re-fire next scan.
6. **`landed_cost` is the single source of truth for what something costs
   you**, so a discount config can't accidentally apply to the wrong item and
   a temporary sale promo can't leak into full-price stock.
