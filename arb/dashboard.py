"""Live dashboard: a small local web page over the scanner's SQLite state.

Read-only by design — it opens state.db in read-only mode so it can never
interfere with the running scanner. Serves one HTML page plus a /api/stats
JSON endpoint that the page polls.

    python -m arb dashboard            # http://127.0.0.1:8787
"""
from __future__ import annotations

import json
import logging
import sqlite3
import statistics
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

from .config import AppConfig

log = logging.getLogger(__name__)

STALE_AFTER_S = 180        # no log activity for this long => scanner looks down


def _connect_ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _age(iso: Optional[str]) -> Optional[float]:
    if not iso:
        return None
    try:
        return (datetime.now(timezone.utc)
                - datetime.fromisoformat(iso)).total_seconds()
    except ValueError:
        return None


def collect_stats(cfg: AppConfig) -> dict[str, Any]:
    """Everything the page shows. Never raises: a partial dashboard beats none."""
    out: dict[str, Any] = {"generated_at": datetime.now(timezone.utc).isoformat()}

    # --- process liveness, inferred from log activity -----------------------
    project = Path(__file__).resolve().parent.parent
    log_file = project / "scanner.log"
    err_file = project / "scanner.err.log"
    last_activity = None
    if log_file.exists():
        last_activity = time.time() - log_file.stat().st_mtime
    out["scanner"] = {
        "seconds_since_activity": round(last_activity) if last_activity is not None else None,
        "up": last_activity is not None and last_activity < STALE_AFTER_S,
        "log_tail": _tail(log_file, 12),
        "recent_errors": [l for l in _tail(err_file, 60)
                          if "Error" in l or "ERROR" in l or "Traceback" in l][-5:],
    }

    try:
        conn = _connect_ro(cfg.db_file)
    except sqlite3.Error as e:
        out["error"] = f"cannot open {cfg.db_file}: {e}"
        return out

    q = lambda sql, *a: conn.execute(sql, a).fetchone()[0]  # noqa: E731

    def kv(key: str) -> Optional[str]:
        r = conn.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
        return r["v"] if r else None

    # --- liveness: heartbeat beats log mtime --------------------------------
    # The scanner writes kv "scanner_heartbeat" only when a scan runs to
    # completion. Log-file activity is kept as a fallback display value, but
    # "up" is judged by the heartbeat: a scanner failing every scan still
    # writes log lines, so mtime alone always reads UP.
    hb_age = _age(kv("scanner_heartbeat"))
    out["scanner"]["seconds_since_heartbeat"] = (
        round(hb_age) if hb_age is not None else None)
    enabled_intervals = [rc.scan_interval_minutes
                         for rc in cfg.retailers.values() if rc.enabled]
    # stale threshold: 2x the fastest enabled interval, floor 30 min
    # (a single scan can legitimately take 20+ min on slow retailers)
    hb_stale_s = max(2 * 60 * min(enabled_intervals, default=15), 1800)
    out["scanner"]["up"] = hb_age is not None and hb_age < hb_stale_s
    out["scanner"]["heartbeat_stale_after_s"] = hb_stale_s

    # --- catalog coverage ---------------------------------------------------
    total_products = q("SELECT COUNT(*) FROM retail_products")
    in_stock = q("SELECT COUNT(*) FROM retail_products WHERE in_stock=1")
    with_code = q("SELECT COUNT(DISTINCT style_code) FROM retail_products "
                  "WHERE style_code IS NOT NULL")
    resolved = q("SELECT COUNT(*) FROM stockx_products WHERE found=1")
    no_match = q("SELECT COUNT(*) FROM stockx_products WHERE found=0")
    checked = resolved + no_match
    out["coverage"] = {
        "retail_products": total_products,
        "in_stock": in_stock,
        "distinct_style_codes": with_code,
        "checked_against_stockx": checked,
        "matched_on_stockx": resolved,
        "not_on_stockx": no_match,
        "pending": max(with_code - checked, 0),
        "percent": round(100 * checked / with_code, 1) if with_code else 0.0,
    }

    # --- market data --------------------------------------------------------
    snapshots = q("SELECT COUNT(*) FROM market_snapshots")
    fresh_cutoff = (datetime.now(timezone.utc)
                    - timedelta(minutes=cfg.stockx.market_refresh_minutes)).isoformat()
    fresh = q("SELECT COUNT(*) FROM market_snapshots WHERE fetched_at >= ?",
              fresh_cutoff)
    with_bid = 0
    ratios: list[float] = []
    for row in conn.execute("SELECT data_json FROM market_snapshots"):
        try:
            d = json.loads(row["data_json"])
        except (json.JSONDecodeError, TypeError):
            continue
        if d.get("highest_bid"):
            with_bid += 1
            if d.get("lowest_ask"):
                ratios.append(d["highest_bid"] / d["lowest_ask"])
    out["market"] = {
        "variant_snapshots": snapshots,
        "fresh": fresh,
        "with_live_bid": with_bid,
        "with_live_bid_pct": round(100 * with_bid / snapshots, 1) if snapshots else 0.0,
        "median_bid_ask_pct": round(100 * statistics.median(ratios), 1) if ratios else None,
    }

    # --- barcode (GTIN) matching -------------------------------------------
    try:
        gtin_total = q("SELECT COUNT(*) FROM stockx_gtins")
        gtin_hit = q("SELECT COUNT(*) FROM stockx_gtins WHERE found=1")
    except sqlite3.Error:
        gtin_total = gtin_hit = 0
    out["gtin"] = {
        "looked_up": gtin_total,
        "matched": gtin_hit,
        "hit_rate": round(100 * gtin_hit / gtin_total, 1) if gtin_total else 0.0,
    }

    # --- StockX API budget --------------------------------------------------
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    used = q("SELECT COUNT(*) FROM api_requests WHERE ts >= ?", cutoff)
    out["api"] = {
        "used_24h": used,
        "budget": cfg.stockx.daily_request_budget,
        "percent": round(100 * used / cfg.stockx.daily_request_budget, 1)
        if cfg.stockx.daily_request_budget else 0.0,
        "hard_limit": 25000,
    }

    # --- per-retailer status ------------------------------------------------
    retailers = []
    for name, rc in cfg.retailers.items():
        row = conn.execute(
            "SELECT * FROM scan_log WHERE retailer=? ORDER BY started_at DESC LIMIT 1",
            (name,)).fetchone()
        age = _age(row["started_at"]) if row else None
        interval_s = rc.scan_interval_minutes * 60
        started, finished = kv(f"scan_started:{name}"), kv(f"scan_finished:{name}")
        # a scan is in flight when it started more recently than the last finish
        scanning = bool(started and (not finished or started > finished))
        retailers.append({
            "name": name,
            "enabled": rc.enabled,
            "interval_min": rc.scan_interval_minutes,
            "scanning": scanning,
            "scanning_for_s": round(_age(started) or 0) if scanning else None,
            "last_scan_age_s": round(age) if age is not None else None,
            "overdue": bool(rc.enabled and not scanning and age is not None
                            and age > interval_s * 2),
            "products_seen": row["products_seen"] if row else None,
            "candidates": row["candidates"] if row else None,
            "opportunities": row["opportunities"] if row else None,
            "alerts_sent": row["alerts_sent"] if row else None,
            "consecutive_failures": int(kv(f"consecutive_failures:{name}") or 0),
        })
    out["retailers"] = sorted(retailers, key=lambda r: (not r["enabled"], r["name"]))

    # --- opportunities + review queue --------------------------------------
    out["alerts"] = {
        "total": q("SELECT COUNT(*) FROM opportunities"),
        "last_24h": q("SELECT COUNT(*) FROM opportunities WHERE last_alerted >= ?",
                      cutoff),
        "review_queue": q("SELECT COUNT(*) FROM review_queue WHERE resolved=0"),
    }
    opportunities = []
    for row in conn.execute(
            "SELECT key, last_alerted, last_profit, was_in_stock, payload_json "
            "FROM opportunities ORDER BY last_profit DESC LIMIT 15"):
        item = {"key": row["key"], "profit": row["last_profit"],
                "in_stock": bool(row["was_in_stock"]),
                "last_alerted": row["last_alerted"]}
        try:
            p = json.loads(row["payload_json"])
            item.update({
                "title": p["stockx"].get("title") or p["retail"]["name"],
                "size": p["size_label"],
                "retailer": p["retail"]["retailer"],
                "price": p["retail"]["price"],
                "url": p["retail"]["url"],
                "stockx_url": p["stockx"].get("url_key"),
                "bid": (p.get("market") or {}).get("highest_bid"),
                "ask": (p.get("market") or {}).get("lowest_ask"),
                # both scenarios, so a list-ask spread is never mistaken for
                # money you can take right now
                "sell_now_profit": (p.get("sell_now") or {}).get("profit"),
                "list_ask_profit": (p.get("list_ask") or {}).get("profit"),
            })
        except (json.JSONDecodeError, KeyError, TypeError):
            pass
        opportunities.append(item)
    out["opportunities"] = opportunities

    # --- scan history (last 20 scans, any retailer) -------------------------
    out["recent_scans"] = [dict(r) for r in conn.execute(
        "SELECT retailer, started_at, products_seen, candidates, opportunities, "
        "alerts_sent FROM scan_log ORDER BY started_at DESC LIMIT 20")]

    out["settings"] = {
        "min_profit_eur": cfg.filters.min_profit_eur,
        "require_live_bid": cfg.filters.require_live_bid,
        "transaction_fee_pct": cfg.profit.transaction_fee_pct,
        "processing_fee_pct": cfg.profit.processing_fee_pct,
        "shipping_eur": cfg.profit.shipping_to_stockx_eur,
        "vat_enabled": cfg.vat.enabled,
    }
    conn.close()
    return out


def _tail(path: Path, n: int) -> list[str]:
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            return [l.rstrip() for l in f.readlines()[-n:]]
    except OSError:
        return []


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>stockx-arb dashboard</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{color-scheme:dark light;--bg:#0f1115;--card:#171a21;--line:#262b36;
--fg:#e6e9ef;--dim:#9aa4b2;--ok:#3fb950;--warn:#d29922;--bad:#f85149;--acc:#58a6ff}
@media(prefers-color-scheme:light){:root{--bg:#f6f7f9;--card:#fff;--line:#e3e6ea;
--fg:#1c2027;--dim:#5b6473;--acc:#0969da}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,
"Segoe UI",sans-serif;padding:20px}
h1{font-size:18px;margin:0}h2{font-size:13px;text-transform:uppercase;
letter-spacing:.06em;color:var(--dim);margin:22px 0 10px}
header{display:flex;align-items:center;gap:14px;flex-wrap:wrap;margin-bottom:6px}
.pill{padding:3px 10px;border-radius:999px;font-weight:600;font-size:12px}
.up{background:rgba(63,185,80,.15);color:var(--ok)}
.down{background:rgba(248,81,73,.15);color:var(--bad)}
.grid{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(190px,1fr))}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px}
.card .label{color:var(--dim);font-size:12px}
.card .value{font-size:26px;font-weight:650;margin-top:2px;letter-spacing:-.02em}
.card .sub{color:var(--dim);font-size:12px;margin-top:3px}
.bar{height:6px;background:var(--line);border-radius:99px;margin-top:9px;overflow:hidden}
.bar>span{display:block;height:100%;background:var(--acc)}
.scroll{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:13px;min-width:560px}
th{text-align:left;color:var(--dim);font-weight:600;font-size:11px;
text-transform:uppercase;letter-spacing:.05em;padding:7px 10px;border-bottom:1px solid var(--line)}
td{padding:7px 10px;border-bottom:1px solid var(--line)}
tr:last-child td{border-bottom:0}
.num{text-align:right;font-variant-numeric:tabular-nums}
.muted{color:var(--dim)}.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}
.acc{color:var(--acc)}
a{color:var(--acc);text-decoration:none}a:hover{text-decoration:underline}
pre{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:12px;overflow-x:auto;font-size:12px;color:var(--dim);margin:0}
footer{margin-top:22px;color:var(--dim);font-size:12px}
</style></head><body>
<header><h1>stockx-arb</h1><span id="status" class="pill down">checking…</span>
<span class="muted" id="updated"></span></header>
<div id="app"></div>
<footer>Auto-refreshes every 10s · read-only view of state.db</footer>
<script>
const fmt=n=>n==null?"—":n.toLocaleString();
const ago=s=>s==null?"never":s<60?`${s}s ago`:s<3600?`${Math.round(s/60)}m ago`
  :`${Math.round(s/3600)}h ago`;
function card(label,value,sub,pct){return `<div class="card"><div class="label">${label}</div>
<div class="value">${value}</div>${sub?`<div class="sub">${sub}</div>`:""}
${pct!=null?`<div class="bar"><span style="width:${Math.min(pct,100)}%"></span></div>`:""}</div>`}
async function tick(){
 let d; try{d=await (await fetch("/api/stats",{cache:"no-store"})).json()}catch(e){return}
 const st=document.getElementById("status");
 st.className="pill "+(d.scanner.up?"up":"down");
 st.textContent=d.scanner.up?"● scanner running":"● scanner DOWN";
 document.getElementById("updated").textContent=
   "last completed scan "+ago(d.scanner.seconds_since_heartbeat)
   +" · last log activity "+ago(d.scanner.seconds_since_activity);
 const c=d.coverage,m=d.market,a=d.api,al=d.alerts,s=d.settings;
 let h=`<div class="grid">
 ${card("Products checked vs StockX",fmt(c.checked_against_stockx),
        `${c.percent}% of ${fmt(c.distinct_style_codes)} style codes · ${fmt(c.pending)} pending`,c.percent)}
 ${card("Matched on StockX",fmt(c.matched_on_stockx),
        `${fmt(c.not_on_stockx)} not carried by StockX`)}
 ${card("Retail products tracked",fmt(c.retail_products),`${fmt(c.in_stock)} currently in stock`)}
 ${card("StockX API used (24h)",fmt(a.used_24h),
        `budget ${fmt(a.budget)} · hard cap ${fmt(a.hard_limit)}`,a.percent)}
 ${card("Market snapshots",fmt(m.variant_snapshots),
        `${fmt(m.fresh)} fresh · ${m.with_live_bid_pct}% have a live bid`)}
 ${card("Median bid vs ask",m.median_bid_ask_pct==null?"—":m.median_bid_ask_pct+"%",
        "why sell-now rarely clears")}
 ${card("Barcodes resolved",fmt(d.gtin.matched),
        `${fmt(d.gtin.looked_up)} looked up · ${d.gtin.hit_rate}% on StockX · exact size matches`)}
 ${card("Alerts sent",fmt(al.total),`${fmt(al.last_24h)} in last 24h · ${fmt(al.review_queue)} in review queue`)}
 ${card("Profit floor",`€${s.min_profit_eur}`,
        (s.require_live_bid?"sell-now only (live bid)":"best of bid/ask")+
        ` · fees ${(s.transaction_fee_pct*100).toFixed(1)}%+${(s.processing_fee_pct*100).toFixed(0)}% · ship €${s.shipping_eur}`)}
 </div>`;
 h+=`<h2>Retailers</h2><div class="scroll"><table><tr><th>Retailer</th><th>Status</th>
 <th class="num">Last scan</th><th class="num">Products</th><th class="num">Candidates</th>
 <th class="num">Opportunities</th><th class="num">Alerts</th></tr>`;
 for(const r of d.retailers){
  const state=!r.enabled?`<span class="muted">disabled</span>`
    :r.scanning?`<span class="acc">scanning ${ago(r.scanning_for_s)===("never")?"":"("+ago(r.scanning_for_s).replace(" ago","")+")"}</span>`
    :r.last_scan_age_s==null?`<span class="warn">no scan yet</span>`
    :r.overdue?`<span class="bad">overdue</span>`:`<span class="ok">idle</span>`;
  const fails=r.consecutive_failures>0
    ?` <span class="bad">${r.consecutive_failures}× failed</span>`:"";
  h+=`<tr><td>${r.name}</td><td>${state}${fails}</td><td class="num muted">${ago(r.last_scan_age_s)}</td>
  <td class="num">${fmt(r.products_seen)}</td><td class="num">${fmt(r.candidates)}</td>
  <td class="num">${fmt(r.opportunities)}</td><td class="num">${fmt(r.alerts_sent)}</td></tr>`}
 h+=`</table></div>`;
 h+=`<h2>Top opportunities</h2>`;
 if(!d.opportunities.length){h+=`<div class="card muted">No opportunities recorded yet.</div>`}
 else{h+=`<div class="scroll"><table><tr><th>Shoe</th><th>Size</th><th>Retailer</th>
  <th class="num">Buy</th><th class="num">Bid</th><th class="num">Ask</th>
  <th class="num">Sell now</th><th class="num">List @ ask</th><th>Seen</th></tr>`;
  const money=(v)=>v==null?`<span class="muted">—</span>`
    :`<span class="${v>=s.min_profit_eur?"ok":"bad"}">€${v.toFixed(2)}</span>`;
  for(const o of d.opportunities){
   const live=o.sell_now_profit!=null&&o.sell_now_profit>=s.min_profit_eur;
   h+=`<tr><td>${o.url?`<a href="${o.url}" target="_blank" rel="noopener">${o.title||o.key}</a>`:(o.title||o.key)}
   ${live?' <span class="ok">⚡</span>':''}</td>
   <td>${o.size||"—"}</td><td class="muted">${o.retailer||"—"}</td>
   <td class="num">${o.price!=null?"€"+o.price.toFixed(0):"—"}</td>
   <td class="num">${o.bid!=null?"€"+o.bid.toFixed(0):`<span class="muted">none</span>`}</td>
   <td class="num muted">${o.ask!=null?"€"+o.ask.toFixed(0):"—"}</td>
   <td class="num">${money(o.sell_now_profit)}</td>
   <td class="num muted">${o.list_ask_profit==null?"—":"€"+o.list_ask_profit.toFixed(2)}</td>
   <td class="muted">${(o.last_alerted||"").slice(5,16).replace("T"," ")}</td></tr>`}
  h+=`</table></div>
  <div class="sub muted" style="margin-top:8px">⚡ = sell-now clears the €${s.min_profit_eur} floor
  (what you get dumping into the live bid). "List @ ask" is shown for context only —
  it requires waiting for a buyer. Rows from before sell-now gating may show a
  healthy list-ask profit next to a negative sell-now figure; that is the honest picture.</div>`}
 if(d.scanner.recent_errors.length){
  h+=`<h2>Recent errors</h2><pre>${d.scanner.recent_errors.join("\\n")
      .replace(/[<>&]/g,x=>({"<":"&lt;",">":"&gt;","&":"&amp;"}[x]))}</pre>`}
 h+=`<h2>Live log</h2><pre>${d.scanner.log_tail.join("\\n")
     .replace(/[<>&]/g,x=>({"<":"&lt;",">":"&gt;","&":"&amp;"}[x]))}</pre>`;
 document.getElementById("app").innerHTML=h;
}
tick();setInterval(tick,10000);
</script></body></html>"""


def run_dashboard(cfg: AppConfig, host: str = "127.0.0.1", port: int = 8787) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):                      # noqa: N802
            if self.path.startswith("/api/stats"):
                try:
                    body = json.dumps(collect_stats(cfg), default=str).encode()
                except Exception as e:         # never take the page down
                    body = json.dumps({"error": str(e)}).encode()
                self._send(body, "application/json")
            elif self.path in ("/", "/index.html"):
                self._send(PAGE.encode("utf-8"), "text/html; charset=utf-8")
            else:
                self.send_error(404)

        def _send(self, body: bytes, ctype: str) -> None:
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):          # quiet: no per-request noise
            pass

    server = ThreadingHTTPServer((host, port), Handler)
    print(f"dashboard: http://{host}:{port}  (Ctrl+C to stop)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
