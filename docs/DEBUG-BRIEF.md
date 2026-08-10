# Debug brief for stockx-arb

Self-contained brief for an agent picking this up cold. Assumes no prior
conversation. Read `docs/STRUCTURE.md` first for architecture.

## Resolution — 2026-08-10

- **Issue 1:** the process-wide cap of 2 was loaded and live-tested; Weekend
  still returned the same Cloudflare challenge. A cap of 1, a one-second
  global gap, and a dedicated five-second quiet window also failed, while the
  exact `WeekendScraper` path continued to return products standalone. Those
  experiments only reduced volume—no identity/TLS/browser/proxy workaround
  was attempted. `weekend.enabled` is now `false`. Reede remains enabled with
  its rotation reduced from 400 to 100 pages. A production shared-process pass
  from 22:06:10 to 22:18:01 completed 70 fresh + 100 rotating pages as 71
  products / 44 candidates, with no 403, no failure, and no opportunity. The
  loop sleeps for the configured 15 minutes *after* that roughly 12-minute
  crawl, so its present start-to-start cadence is about 27 minutes; reaching a
  literal 15-minute cadence would require scheduler/coverage redesign rather
  than more aggressive requests. Safe 403 logs now identify Cloudflare
  challenges and include the request ray ID.
- **Issue 2:** the market is thin, but the pipeline also had a real broken
  gate. `_evaluate_product` passed every display label as EU even when
  `RetailSize.system == "US"`; correct US matches were downgraded to 0.5 and
  rejected. The pipeline now routes US/EU/letter labels by their declared
  system, with regression tests. Evidence trace: Salomon `L49229300` had a
  +EUR121.68 product-level maximum only on out-of-stock US 7, so it was
  correctly withheld. Sportland `DD1579-402` was live-verified end to end:
  EU 47.5 in stock at EUR59.99, EUR83 bid, EUR63.04 payout, EUR3.05 profit;
  it had already alerted and later scans correctly deduplicated it. Two SNS
  products also traversed the corrected US-size/barcode path successfully.
- **Issue 3:** `arb report` now records requests in the configured database;
  `arb review` lists/filters/closes queue rows; the unused `CachedProvider`
  was removed because scanner-level dynamic TTL logic is authoritative; and
  `.env`, `state.db`, plus active WAL/SHM files were restricted locally to the
  owner, SYSTEM and Administrators.
- Verification after each production change: `selftest OK`; final suite is
  **147 passed**.

---

## What this project is

A read-only retail-arbitrage scanner. Scrapes ~11 European sneaker/collectible
retailers, resolves each product to its StockX catalog entry, pulls live
bid/ask, computes net profit after fees, and alerts to Discord when a spread
clears a threshold. **It only finds and reports — it never buys, lists, or
transacts.** There is deliberately no code path anywhere that places an order.

Repo: `github.com/choppashxt/stockx-arb`. Single Python package `arb/`,
SQLite state in `state.db`, all tunables in `config.yaml`, secrets in `.env`.

## How to run and verify

```bash
python -m arb selftest        # offline, no creds, no network — must print "selftest OK"
python -m pytest tests/ -q    # 143 tests, ~2s
python -m arb status          # DB + API budget overview
python -m arb report          # live-verified current opportunities
python -m arb scan --once --dry-run --retailer NAME   # one retailer, prints instead of alerting
```

On Windows set `PYTHONIOENCODING=utf-8` first — alert formatting uses `€`, `→`
and box-drawing characters that cp1252 cannot encode.

**Always run `selftest` and the test suite before and after any change.** The
selftest exercises the whole pipeline against fixtures including the sizeless
path and the region gate.

**Never point a test run at the live `state.db`.** `--dry-run` still writes to
the `opportunities` table, which corrupts alert-dedup state. Copy the DB and
override `db_path` in a scratch config instead.

---

## ISSUE 1 (primary) — weekend.ee returns 403 inside the scanner but works standalone

### Symptom
`weekend` reports `0 products scraped` on every scan cycle. The log shows
`www.weekend.ee returned 403 — not retrying this run` exactly once per scan,
43 occurrences over ~a day. The retailer contributed nothing during that time.

### What has already been established (do not redo)

1. **Not a User-Agent problem.** Tested directly against
   `https://www.weekend.ee/graphql?query=...`:
   - our UA (`sneaker-arb-scanner/0.1 (... contact: kaarmamarkus@gmail.com)`) → **200 OK**
   - no UA → 403 Cloudflare "Just a moment..." challenge
   - `Mozilla/5.0` → 403 challenge
   Cloudflare is blocking *generic* clients and allowing our honest one.

2. **Not a rate problem for that host alone.** weekend's config was already
   backed off to `scan_interval_minutes: 60`, `request_delay_seconds: 5.0`,
   `max_pages: 6` (84 requests worst case). The 403s continued unchanged.

3. **The scraper is correct.** `WeekendScraper.scan()` run standalone in its
   own process returns 99 products across 2 pages, both requests 200 OK.

4. **Five of seven retailer hosts are behind Cloudflare**: weekend.ee,
   reede.ee, ballzy.eu, sportland.ee, teamsport.ee. (rademar.ee and klick.ee
   are not.)

5. **Concurrency correlates with the failure.** In the 6-second window around
   a 403, four retailers were fetching simultaneously — Ballzy alone had 4
   concurrent category crawls in flight — plus StockX API traffic.

### Working hypothesis (UNVERIFIED — your job to confirm or refute)

Cloudflare scores bot reputation **per IP across its entire network**, not per
site. `run_loop` gathers every enabled retailer concurrently and some scrapers
crawl categories in parallel, so the machine emits a burst across several
CF-fronted sites at once. Per-host politeness cannot see this. The strictest
configs (weekend, reede) then challenge.

### Fix already applied, NOT yet verified

`arb/retailers/base.py` now has a process-wide semaphore:

```python
MAX_CONCURRENT_REQUESTS = 2
_REQUEST_SLOT = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
```

wrapped around all three outbound call sites (robots fetch, main fetch, 429
retry). Tests pass. **It has not been validated against a full live scan
cycle.**

### What to do

1. Run a full `python -m arb scan` cycle and confirm weekend.ee returns
   products rather than 403.
2. If it still 403s, the hypothesis is wrong — investigate further. Consider:
   TLS/JA3 fingerprinting, HTTP/2 vs 1.1, connection reuse, whether the 403
   body differs from the "Just a moment..." challenge seen with generic UAs.
3. If it works, check whether `MAX_CONCURRENT_REQUESTS` can go to 3 without
   regressing, and whether weekend's config can be restored to
   `scan_interval_minutes: 30` / `request_delay_seconds: 2.5` / `max_pages: 12`
   (it was only backed off to chase this bug).
4. **Also fix reede.ee** — 175 × 403 in the same log, almost certainly the same
   root cause, currently only partially scraping.

### HARD CONSTRAINT

Do **not** solve this by evading bot protection. No spoofed browser
User-Agents, no TLS/JA3 fingerprint mimicry, no CAPTCHA solving, no proxy
rotation, no headless-browser automation to look human. If the only way past
is evasion, the correct outcome is to **disable the retailer** and say so —
that is what was already done for sportsdirect.ee and stadium.fi. Reducing our
own request volume is the sanctioned lever.

---

## ISSUE 2 — verify the pipeline is not over-filtering

`min_profit_eur` is currently **0.0** (alert on any positive profit). A full
pass over all retailers — 7,277 products, ~7,870 with confident StockX matches
— produced **zero opportunities**.

This may be correct (the market genuinely is that thin) or may indicate a gate
that is silently rejecting everything. Establish which, with evidence.

Trace one product end to end through `_evaluate_product` in `arb/scanner.py`
and confirm each gate is behaving:

- `_cheap_screen` — in stock, has style_code or ean, price ≤ max, EUR
- resolution confidence ≥ `alert_min_confidence` (0.90)
- `_market_ttl_minutes` — is cached market data being reused when it should be refreshed?
- per-size stock (`RetailSize.in_stock is True`) and `size_match_confidence >= 1.0`
- `_gate_profit` under `require_live_bid: true` — sell-now profit only
- `_liquidity_ok`

Note `market_calls_per_scan` and the daily budget truncate the candidate list,
and candidates are sorted by `_discount_rank` (deepest markdown first), so
partial coverage per scan is expected and not itself a bug.

Useful known-good reference: the one completed profitable trade was
`DR0453-005` (Nike Air Max Pulse) at Sportland, €75.99 against a €176 bid.

---

## ISSUE 3 — known open items (lower priority)

- **`arb report` builds `Database(":memory:")`** (`arb/cli.py`), so its StockX
  requests are never counted against the daily budget. Rate *pacing* is correct
  via the cross-process lease file; only accounting is wrong.
- **No `arb review` subcommand.** `db.add_review()` is written in several
  places and nothing ever reads it — 2,500+ unresolved rows. This blocks any
  category needing fuzzy matching (trading cards).
- **`CachedProvider`** in `arb/stockx/market.py` is defined but never
  instantiated — the snapshot cache is consulted directly in `scanner.py`
  instead. Either wire it up or delete it.
- **`.env` and `state.db` file permissions** were never tightened on the
  original Windows machine.

---

## Invariants — do not change these

1. **Alert-only.** No auto-buy, no listing, no checkout automation.
2. **No anti-detection.** robots.txt is fetched and obeyed; honest User-Agent
   with contact address; per-host crawl delays with jitter. Sites that block
   plain clients get disabled, not circumvented.
3. **Never guess a size or a match.** Below full confidence → review queue,
   never an alert. A wrong match means a real wrong purchase.
4. **Sell-now gating.** Profit is judged on the live highest bid, not the ask.
5. **A failed alert send must never be recorded as sent** — otherwise dedup
   suppresses that opportunity forever.
6. **`Product.landed_cost` is the single source of truth for cost.**

## Recent context worth knowing

Two pricing bugs were fixed on 2026-08-09 after a real sale exposed them:
`shipping_to_stockx_eur` was 7.00 but is actually 10.00 (proved by an exact
€79.76 payout on a €102 bid), and a Ballzy 15% promo was still configured after
it had ended. Together they turned a €9.24 loss into an apparent €7.11 profit.
If you touch profit maths, verify against `arb sold` records in the `sales`
table — that is the only place real outcomes are stored.
