# NIFTY 50 · ICT / SMC desk

A web app for NIFTY 50 that reads the market the way an ICT / Smart-Money trader does — liquidity, sweeps,
structure, order blocks, FVGs, premium/discount, OTE — from **Dhan API v2** live data, shows it on a
TradingView Lightweight Chart next to the option chain, alerts on three playbook setups and paper-trades them.
**It never places a real order** — there is no order endpoint anywhere in the code.

```
backend/   FastAPI + SQLite  (Python 3.9+, same .venv style as your OC folder)   → port 8600
frontend/  React 18 + TypeScript + Tailwind + lightweight-charts 5.1              → built into frontend/dist
docs/GUIDE.md   build order, every rule, the 8-Oct-2026 worked example, top 10 mistakes
```

## 1 · First-time setup (Ubuntu)

```bash
cd ict_smc_app/backend
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

cd ../frontend
npm install
npm run build                 # writes frontend/dist — the backend serves it
```
Node 18+ is needed only to build the frontend.

## 2 · Credentials

The backend reads a `.env`. Point it at the one your OC app already uses, so the daily token is pasted once:

```bash
export ENV_FILE="$HOME/Documents/Swathi K N/1 Trading/option chain/OC/.env"
```
Keys it uses: `DHAN_CLIENT_ID`, `DHAN_ACCESS_TOKEN` (required), `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
(optional), `AUTO_RENEW_TOKEN=1` (optional — renews a still-valid web token at 08:35). The `.env` is re-read
when it changes, so `sed -i` on the token at 08:30 needs no restart. See `backend/.env.example`.

## 3 · Run

```bash
cd ict_smc_app/backend
.venv/bin/python -m uvicorn app.api.main:app --host 0.0.0.0 --port 8600
```
Open `http://localhost:8600` — or from your phone over Tailscale: `http://100.113.93.21:8600`.

Developing the UI with hot reload instead: keep the backend running, then `cd frontend && npm run dev` →
`http://localhost:5173` (it proxies `/api` and `/ws` to 8600).

## 4 · Test it after market hours (fake Dhan replay of 8 Oct 2026)

Two terminals, both in `backend/`:
```bash
# terminal 1 — the fake Dhan server (REST + binary WebSocket feed), replaying 8 Oct from 14:20 at 12× speed
export ICT_SIM_START=$(python3 -c "import datetime as d;print(int(d.datetime(2026,10,8,14,20,tzinfo=d.timezone(d.timedelta(hours=5,minutes=30))).timestamp()))")
export ICT_SIM_REAL0=$(date +%s) ICT_SIM_SPEED=12 ICT_DATA_DIR=$PWD/data_replay
.venv/bin/python tools/fake_dhan.py

# terminal 2 — the app against the fake (copy the SAME four exports first)
ENV_FILE=tools/fake.env .venv/bin/python -m uvicorn app.api.main:app --port 8600
```
`FAKE_DROP_AFTER_S=45` on the fake makes it cut the socket every 45 s, to watch auto-reconnect and gap repair.
`ICT_DATA_DIR` keeps the replay's SQLite away from your real one.

## 5 · Tests
```bash
cd backend && .venv/bin/python -m pytest -q        # 55 tests: engine on real 8-Oct candles, 8-Oct baseline lock, CRT + TBS (setup D), playbook, paper, alerts, feed decoder, zone registry, delta rules, CSV alignment, strike selector parity, strike flow, proxy
```

## 6 · Ports in use on your laptop
8501 dashboard · 8502 / 8503 Paper Desks · 8505 / 8506 replay · 8510 / 8511 after-hours desks · **8600 this app** ·
8701 fake Dhan (testing only).

## 7 · Run it at boot (optional)
```bash
sudo tee /etc/systemd/system/ict-smc.service >/dev/null <<EOF
[Unit]
Description=NIFTY ICT/SMC desk
After=network-online.target
[Service]
User=$USER
WorkingDirectory=$PWD
Environment=ENV_FILE=$HOME/Documents/Swathi K N/1 Trading/option chain/OC/.env
ExecStart=$PWD/.venv/bin/python -m uvicorn app.api.main:app --host 0.0.0.0 --port 8600
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload && sudo systemctl enable --now ict-smc
```
(run from `backend/`). It holds **one** of your five Dhan WebSocket connections.

## 7b · Delta / Footprint (optional)
The NIFTY index has no volume, so delta needs a source: **Delta / Footprint** tab → `csv` (upload from your
footprint tool) or, later, `footprint_feed` (wire an aggressor-tagged futures feed). `mock` is fake data for
testing the screen only. See GUIDE §8. Delta never creates a trade; it only adjusts Setup A/C confidence.

## 7c · Strike mode (selected CE / PE)
Structure, zones and the playbook stay on the NIFTY index. The **Instrument · strikes** card picks the CE and PE
with your OC Strike Advisor rule (MJ course pick), shows OI / ΔOI / IV / Greeks with a one-line build read, and
switches the active strike only on a 3-minute close. Those strikes feed delta (when a source exists), the opt-in
**Options Flow Proxy — PROXY (not real delta)**, and premium P&L on paper trades. *Option chart* mode is
experimental. Needs `pip install -r requirements.txt` again (adds pandas). See GUIDE §9.

## 7d · Setup D — CRT + TBS
AR's Candle Range Theory + Turtle Body Soup on three timeframe pairs (1h→1m, 4h→5m, 1D→1h). HTF CRT candle with a
keylevel (OHP / OLP / OB / FVG) → next HTF candle manipulates CRTH / CRTL → LTF TBS (body close beyond) → Model #1 →
entry on the close beyond Model #1. Half at the 50 % with the stop to breakeven, rest to the opposite CRT extreme.
Separate engine; A / B / C are unchanged. Settings in `rules.json → crt_tbs`. See GUIDE §10.

## 8 · Not in v1 (on purpose)
No order placement. No futures volume / VWAP (the NIFTY index has no volume — use your OC app's futures VWAP).
Intraday OI snapshots are stored (`chain_snap`) but not charted yet. The playbook is unproven until it has
30+ paper trades — treat its numbers as a hypothesis.
