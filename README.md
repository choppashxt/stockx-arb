# stockx-arb — StockX retail-arbitrage scanner

Scans European sneaker retailers for shoes whose retail price, after StockX
fees and shipping, nets a profit above your threshold — then sends a Discord
alert (Telegram also supported) with a direct BUY link so you can act in
seconds.

**The bot only finds and notifies. It never buys, lists, or transacts.**
Execution is manual, by you.

## Status

All phases live (2026-07-27). Alerting is **sell-now gated**: profit is judged
on the payout from the live highest bid (`filters.require_live_bid`) — spreads
that only work if a listing eventually sells never alert.

| Retailer | Status | How |
|---|---|---|
| Ballzy (.eu, serves EE/LV/LT/FI/SE) | live | RSC grid + product pages; US sizes from variant SKUs |
| Reede (reede.ee) | live | sale/category pages + sitemap rotation; US sizes + brand/gender from jsonConfig |
| Teamsport (teamsport.ee) | live | category grid crawl; style codes in URLs; US sizes from jsonConfig |
| Rademar (rademar.ee) | live | open JSON API; style code + EU sizes + per-size EANs; quantities check at alert time |
| Sportland (.ee/.lv/.lt) | live | sitemap + storefront GraphQL; **size-level stock unverified** → one labeled product-level alert |
| SportsDirect (.ee) | skipped | drops non-browser connections (Frasers bot wall) |
| Sneakersnstuff | live | Shopify JSON-LD: per-size US sizes, prices, stock, **GTINs**; 10s crawl-delay honored |
| Footshop (.eu) | live | microdata (style code/price/stock); size-level stock client-side only → labeled product-level alerts |
| Stadium.fi | skipped | homepage answers but catalog paths 403 non-browser clients |
| END. / BSTN | candidates | Akamai-fronted; answered politely, fragile |
| Zalando / AboutYou | skipped | partner-API/affiliate only |

## Setup

```bash
cd stockx-arb
pip install -r requirements.txt        # on this machine add:
                                       # --trusted-host pypi.org --trusted-host files.pythonhosted.org
cp .env.example .env                   # fill in StockX creds + Discord webhook
```

Alert channel is `notifications.method` in `config.yaml`: `discord` (default,
needs `DISCORD_WEBHOOK_URL`), `telegram` (needs bot token + chat id), or
`console`. `--dry-run` always prints to console regardless.

Tunables (fees, thresholds, retailer intervals) live in `config.yaml`;
secrets live in `.env`. Nothing is hard-coded.

### First-time StockX auth

1. On developer.stockx.com → **Applications** → *Create an App*. Any name;
   set the **Callback URI** to exactly `http://localhost:8123/callback`.
2. Copy the app's **Client ID** and **Client Secret** plus your **API key**
   into `.env`.
3. Run `python -m arb auth` — it opens the StockX login in your browser,
   catches the redirect locally, and writes `STOCKX_REFRESH_TOKEN` into
   `.env`.

Troubleshooting `auth`:

- *"port 8123 is already in use"* — an earlier `auth` run is still waiting for
  a login. Close that window, or use `--manual`.
- *Browser doesn't open, or the redirect can't reach localhost* — run
  `python -m arb auth --manual` and paste the URL the browser landed on.
  `--manual` needs a real interactive terminal (it prompts for the paste).
- *Different port* — `--port 8124` works, but the app's Callback URI on
  developer.stockx.com must match it exactly.

## Running

```bash
python -m arb selftest                       # offline pipeline check, no creds needed
python -m arb scan --backfill --dry-run      # FIRST RUN: resolve + price the whole
                                             # catalog now (~1h, resumable)
python -m arb scan --once --dry-run          # one pass, alerts to console
python -m arb scan --once                    # one pass, alerts to Discord
python -m arb scan                           # continuous loop (per-retailer intervals)
python -m arb resolve DZ5485-612 --market    # debug one style code against StockX
python -m arb status                         # DB, budget, recent scans
```

`--provider fixture` runs the real retailer scrape against canned StockX data
(no credentials, no API quota) — useful for testing scraper changes.
Fixture runs use a separate `state-fixture.db` so they never pollute real state.

## How a scan works

1. **Scrape** the retailer's category grids → normalized `Product` records
   (name, brand, style code, price, available sizes).
2. **Resolve** each style code against the StockX catalog (`/catalog/search`,
   exact `styleId` match required). Resolutions are cached in SQLite forever;
   "not found" is cached for `negative_cache_days`.
3. **Market data** via the `MarketDataProvider` interface — one API call per
   product covers all variants. Snapshots are reused for
   `market_refresh_minutes`, so steady-state scans are cheap.
4. **Upside gate**: if no variant could clear `min_profit_eur` at the retail
   price, stop here (no product-page fetch, no per-size work).
5. **Confirm** on the retailer product page: authoritative price, per-size
   stock, and the retailer's own EU↔US size mapping.
6. **Match sizes** — retail US size must equal the StockX variant size, and the
   EU label must not contradict StockX's own size-chart conversion. Anything
   ambiguous goes to the `review_queue` table instead of alerting.
7. **Profit** both ways: `sell_now` (into the highest bid) and `list_ask`
   (at/under the lowest ask). Fees, shipping, and the (default-off) VAT wedge
   come from config.
8. **Filter + rank**: `min_profit_eur`, liquidity floors where the provider
   supplies them, score = profit × liquidity factor (live-bid opportunities
   are flagged ⚡ and ranked first).
9. **Alert + dedup**: each opportunity is keyed by retailer+variant+size.
   You are alerted once — again only on a material profit change
   (`re_alert_profit_delta_eur`) or a restock.

## StockX API notes

- Auth: OAuth2 refresh-token flow against `accounts.stockx.com`; access token
  cached in SQLite until expiry. `x-api-key` + Bearer on every call.
- Rate limits: 25 000 requests/24 h, 1 req/s (verified from the developer
  portal, July 2026). The client throttles to `min_request_interval_s`,
  keeps a self-imposed `daily_request_budget`, and backs off exponentially
  on 429/5xx.
- The official market-data endpoint returns **only** lowest ask + highest bid,
  as nullable strings; there are **no** bid/ask counts, sales velocity, or
  last-sale fields. Null fields are logged and the item is skipped — never a
  crash. When a richer source (KicksDB, Apify) is added later, it becomes
  another `MarketDataProvider` implementation and the liquidity filters
  (`min_bids`, `min_sales_72h`) start biting automatically.

## VAT

`vat.enabled: false` — proof-of-concept mode, no VAT math applied.
All VAT logic is isolated in `arb/profit.py::vat_wedge()`. Once the company +
KMKR registration is real, flip `enabled: true` and set
`input_vat_reclaimable` / `output_vat_on_sale` / `rate` to model the actual
position. Nothing else changes.

## Adding a retailer

1. Create `arb/retailers/<name>.py` with a class implementing
   `RetailerScraper` (see `ballzy.py`):
   - `scan()` — sweep category/listing pages into `Product` records. Grid-level
     prices are fine (`price_verified=False`).
   - `enrich(product)` — fetch one product page; return authoritative price,
     per-size stock, and `us_size` per size **only if the retailer's own data
     provides the mapping** (SKU suffix, size table…). Never guess.
2. Register it in `arb/retailers/__init__.py::_SCRAPERS`.
3. Add a block under `retailers:` in `config.yaml` (enable flag, interval,
   category URLs, request delay).

The polite fetcher (robots.txt compliance, crawl-delay, jitter, honest UA,
back-off) is inherited — don't work around it. Check the target's robots.txt
first; if catalog pages are disallowed, the retailer needs a different design
(or should be skipped), not a stealthier scraper.

## Design guardrails — keep these

- **Alert-only.** No auto-buy, no checkout automation, no listing automation.
- **No anti-detection.** Respect robots.txt and rate limits; honest UA; a 403
  is an answer, not an obstacle.
- **Never guess a size, never fuzzy-alert.** Anything below firm confidence
  goes to the review queue for human eyes.
- Hobby tool, not financial advice. Spreads carry real risk: price movement,
  authentication rejections, thin liquidity, fills that never come.
