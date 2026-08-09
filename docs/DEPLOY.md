# Running stockx-arb on a VPS

Moves the scanner off your laptop so it runs 24/7. Everything below is Linux
(Debian/Ubuntu); the two Windows `.bat` runners are the only pieces that don't
carry over, and systemd replaces them properly.

---

## 0. Read this first — do not run both at once

The scanner is rate-limited against **your** StockX account: 25,000 requests /
24h, hard. Two copies running share that budget and will exhaust it early,
after which neither can price anything. They would also both send Discord
alerts, and each keeps its own dedup state, so you'd get everything twice.

**When the VPS goes live, stop the local one.** On Windows: close the
`run_scanner.bat` / `run_dashboard.bat` windows and remove the two shortcuts
from
`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup`
so they don't come back on next boot.

Running the local copy briefly for a one-off `arb report` is fine — the
`.stockx_ratelimit` lease file only coordinates processes on the *same*
machine, but a single manual command is a handful of requests.

---

## 1. Pick a VPS

Anything with 1 vCPU / 1 GB RAM is plenty — the work is almost entirely
waiting on network I/O. Hetzner CX22 (~€4/mo) or similar is more than enough.

**Choose an EU location** (Helsinki, Falkenstein, Nuremberg). The scrapers hit
Estonian and Nordic shops; an EU IP is geographically honest, lower latency,
and less likely to trip the geo/bot heuristics that already cost us weekend.ee
for a day. Avoid US datacentres for this.

Debian 12 or Ubuntu 24.04.

---

## 2. Base setup

```bash
ssh root@<vps-ip>

apt update && apt install -y python3 python3-venv python3-pip git
adduser --system --group --home /opt/stockx-arb arb
```

## 3. Get the code

```bash
cd /opt
git clone https://github.com/choppashxt/stockx-arb.git stockx-arb-src
cp -r stockx-arb-src/. /opt/stockx-arb/
rm -rf stockx-arb-src
cd /opt/stockx-arb

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

`truststore` is in requirements and is harmless here — it exists for the
TLS-intercepting antivirus on the Windows machine and is a no-op on a clean
Linux box.

## 4. Credentials

`.env` is gitignored, so it is NOT in the clone. Create it:

```bash
sudo -u arb nano /opt/stockx-arb/.env
```

```
STOCKX_API_KEY=...
STOCKX_CLIENT_ID=...
STOCKX_CLIENT_SECRET=...
STOCKX_REFRESH_TOKEN=...
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
```

Copy the values from the `.env` on your Windows machine. **Do not run
`arb auth` on the VPS** — it needs a browser callback and there isn't one. The
existing refresh token works from any IP; the client just exchanges it for
access tokens.

Lock it down:

```bash
chown arb:arb /opt/stockx-arb/.env
chmod 600 /opt/stockx-arb/.env
```

## 5. Bring the database (optional, recommended)

`state.db` holds ~4,000 resolved products, cached variants and market
snapshots. Without it the scanner works but spends its first day or two
re-resolving the catalogue from scratch.

From Windows (PowerShell), with the local scanner **stopped** so the file is
not mid-write:

```powershell
scp "C:\Users\ADMIN\Documents\Cs2 arbitrage\stockx-arb\state.db" arb@<vps-ip>:/opt/stockx-arb/state.db
```

Then on the VPS:

```bash
chown arb:arb /opt/stockx-arb/state.db
chmod 600 /opt/stockx-arb/state.db
```

**Treat `state.db` as a secret.** Its `kv` table caches your live StockX access
and refresh tokens. Don't put it anywhere shared.

## 6. Verify before starting anything

```bash
cd /opt/stockx-arb
sudo -u arb .venv/bin/python -m arb selftest     # offline, no creds needed
sudo -u arb .venv/bin/python -m arb status       # reads .env + DB
```

`selftest` runs the whole pipeline against fixtures. If it passes, the install
is sound. `status` confirms credentials load and shows the API budget.

## 7. Install the services

```bash
cp /opt/stockx-arb/deploy/stockx-arb-scanner.service /etc/systemd/system/
cp /opt/stockx-arb/deploy/stockx-arb-dashboard.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now stockx-arb-scanner stockx-arb-dashboard
```

Check it:

```bash
systemctl status stockx-arb-scanner
journalctl -u stockx-arb -f          # live log, Ctrl-C to detach
```

`Restart=always` with `RestartSec=60` replaces the `.bat` loop, and services
start on boot — so a VPS reboot brings everything back without you.

## 8. Reaching the dashboard

The dashboard has **no authentication** and shows your live opportunities and
API budget, so it binds to `127.0.0.1` only. Tunnel to it:

```bash
ssh -N -L 8787:127.0.0.1:8787 arb@<vps-ip>
```

Then open `http://127.0.0.1:8787` in your own browser. Do not "fix" this by
binding `0.0.0.0`.

---

## Day-to-day

```bash
journalctl -u stockx-arb -f                       # watch it work
journalctl -u stockx-arb --since "1 hour ago"     # recent activity
systemctl restart stockx-arb-scanner              # after a config change
sudo -u arb /opt/stockx-arb/.venv/bin/python -m arb report    # live opportunities
```

Updating after a code change:

```bash
cd /opt/stockx-arb
sudo -u arb git pull
sudo -u arb .venv/bin/pip install -r requirements.txt   # only if deps changed
systemctl restart stockx-arb-scanner stockx-arb-dashboard
```

Editing `config.yaml` requires a scanner restart — config is read once at
startup, which is the same reason the dashboard didn't show weekend.ee until it
was restarted.

## Things that will bite

- **Clock/timezone.** The VPS will be UTC; your laptop logs were local time.
  Alert timestamps and `--until` dates on `arb watch` are UTC on the server.
- **A silent retailer looks like a quiet market.** weekend.ee returned 403 for
  a full day and simply reported "0 products scraped" each cycle. Worth
  skimming `journalctl` occasionally for repeated zero-product scans.
- **The daily budget resets on a rolling 24h window**, not at midnight.
- **Don't expose the dashboard.** Repeating it because it is the one genuinely
  risky misconfiguration available here.
