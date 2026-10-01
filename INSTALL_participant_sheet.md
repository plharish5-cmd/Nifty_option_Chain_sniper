# Participant sheet (Stock Unlimited method) — install

Replicates the trainer's evening routine in `Partcipant_Data_Sheet_NEW.xlsm`:
downloads the three NSE files his Power Query used, pastes them into the same cells
his Ctrl+Q/W/E/R/K/P/O/I/U/L macros paste to, fills every formula down, handles
holidays the way he does (date set on the FII sheet only), and saves the workbook with
his colours, data bars, charts and Ctrl-key macros intact.

Verified: 25-Sep was removed from a copy of his workbook and rebuilt by this code —
all 186 filled cells on the 7 sheets (values and formulas) match his original exactly;
8 charts, 139 conditional-format rules, 190 data bars and all 12 Ctrl-key macros survive.

## 1. One-time setup (Ubuntu)

```bash
sudo apt install -y libreoffice-calc python3-uno        # LibreOffice does the Excel writing
cd ~/Documents/"Swathi K N"/"1 Trading"/"option chain"/OC
.venv/bin/pip install xlrd                              # reads NSE's fii_stats .xls
/usr/bin/python3 -c "import uno; print('uno ok')"       # must print: uno ok
```

Copy these files into the OC folder:
`participant_nse.py  participant_workbook_uno.py  update_participant_sheet.py
 xlsm_restore_vba.py  participant_ratios.py  participant_sheet_tab.py`

Put the trainer's workbook in place:

```bash
mkdir -p data/participant_sheet
cp ~/Downloads/Partcipant_Data_Sheet_NEW.xlsm data/participant_sheet/
```

The first run also keeps an untouched copy in `data/participant_sheet/original/`
(his macros are taken from it every time — never delete it).

## 2. Run it

```bash
.venv/bin/python update_participant_sheet.py            # catch up every missing day to today
.venv/bin/python update_participant_sheet.py --date 2026-09-28
# override the values he types by hand, for the latest day:
.venv/bin/python update_participant_sheet.py --nifty 23140 --banknifty 55580 \
      --fii-equity -3273.17 --dii-equity 2595.15 --fii-equity-prev -4617.61
```

Automatic sources for the typed values: Nifty close = last bar of `data/candles_5m/<date>.csv`
(your 15:45 timer keeps it complete), BankNifty close = Dhan, FII/DII cash = NSE's FII/DII
page (today only). The NSDL final figure for the previous day (FII!BD) has no free API —
pass `--fii-equity-prev` if you want it; the trainer says it changes nothing.

Output: `data/participant_sheet/Partcipant_Data_Sheet_NEW.xlsm` (updated in place, last 10
versions in `backups/`), a copy at `data/NSE_Participant_OI_and_Option_Chain_Master.xlsx`
for the app, `data/participants.parquet` appended, log in `data/participant_sheet/update.log`.

## 3. Every evening, automatically

```bash
cp systemd/nifty-participant-sheet.* ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now nifty-participant-sheet.timer
systemctl --user start nifty-participant-sheet.service      # test once now
tail -20 data/participant_sheet/update.log
```

Runs Mon–Fri at 19:20, 20:20, 21:20, 22:20, 23:20. A run where NSE has not published yet
just logs "not published yet"; a run with nothing new logs "already up to date".

## 4. See it in the app

```bash
.venv/bin/streamlit run participant_sheet_tab.py
```

Download button for the .xlsm, today's FII / Retail / PRO ratios, last 20 sessions painted
with his colours and data bars, ratio-vs-Nifty chart, net options/stock-futures bars, the
edge table, and an "Update now" button. To add it as a tab later: `import participant_sheet_tab`
and call `participant_sheet_tab.render()` inside a tab in dashboard.py.

## What is different from his Excel file

- **Refresh All no longer works inside Excel** on the updated file: LibreOffice removes the
  Power Query connections when it saves. On your Linux machine they never worked anyway;
  this code replaces them. Keep his original (in `original/`) if you ever use Windows Excel.
- Nothing else: same sheets, rows, formulas, colours, data bars, charts and Ctrl-key macros.
- Chart tabs he stopped updating (Vol Vs OI, Futures, FII vs spot, FII PCR, PRO vs Spot)
  are left exactly as he left them. 'OI vs Spot' and 'Cumulative', which he keeps current,
  are extended by one row per day together with their charts.

---

# Update 2 — from "Lecture 1.1 Participants Sheet How to Update"

New files: `nsdl_fpi.py`, `update_fpi_fortnight.py`, `fpi_sector_tab.py`,
`systemd/nifty-fpi-fortnight.service`, `systemd/nifty-fpi-fortnight.timer`.
Updated: `participant_workbook_uno.py`, `update_participant_sheet.py`, `participant_ratios.py`,
`participant_sheet_tab.py`. No new pip packages.

## What the evening run now also does

- **FII!BD (NSDL final FPI equity).** Fetched from NSDL every run and written into the blank BD
  cell of the trading day *before* each NSDL reporting date (his rule: NSDL is one day late,
  and sometimes gives two days together). Existing BD values are never overwritten. If NSDL
  can't be reached, the log says so and you can type it: `--fii-equity-prev 1938`.
- **Nifty & Bank (Ctrl+N / Ctrl+B).** Net futures qty, average buy/sell price, net options qty,
  average buy/sell strike, net options ₹ Cr, for Nifty (B:I) and BankNifty (N:U), from the
  fii_stats file with his lot sizes (65 / 30). His sheet stopped on 13-Aug because the Nifty
  options part showed `#REF!`; the automation writes the correct numbers there.
- **Daily Equity.** Formula rows are dragged down to keep level with the FII sheet, as he says.

## FPI Fortnight workbook

```bash
mkdir -p data/fpi_fortnight
cp "<path to>/FPI Fortnight Data.xlsx" "data/fpi_fortnight/FPI Fortnight Data.xlsx"
.venv/bin/python update_fpi_fortnight.py          # adds Sep 1-15 (and later ones once published)
tail -20 data/fpi_fortnight/update.log

cp systemd/nifty-fpi-fortnight.* ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now nifty-fpi-fortnight.timer
```

Each new fortnight becomes a new Sheet2 column (values only, his cell style, header typed as
text such as "Sep 1-15"); his green/red rule colours it. The Power Query URL date is changed
too, as in his Advanced-Editor step. This workbook is edited at file level, not through
LibreOffice, so Refresh All still works in Windows Excel.

The app's participant tab now ends with the FPI section: sector × fortnight table in his
colours, latest-fortnight bars, download, "Check NSDL" button and a history check.

---

# Update 3 — Lecture 2 "Data Correlation"

New: `participant_correlation.py`, `participant_correlation_rules.json` (his thresholds — edit
there), `participant_correlation_tab.py`. Updated: `participant_sheet_tab.py`,
`update_participant_sheet.py`. No new pip packages. Nifty OHLC comes from Dhan (your Data API).

```bash
.venv/bin/python participant_correlation.py      # today's readings + full history table
```

First run builds Nifty daily OHLC from Dhan (daily endpoint, else hourly intraday bars in
88-day windows, about 30 requests, ~15 s), cached in `data/ohlc/nifty_daily_dhan.csv` and
topped up with one request per run after that. yfinance only fills the months before Dhan's
~5-year intraday history. Rules R6-R9 need open/high/low, so a valid Dhan token is needed. The evening run now also logs a
"Correlation ..." line. In the app, the participant tab has a new "Data correlation" section:
rule cards for the next session, portfolio chart (Nifty + FII stock futures with his lines),
Y / AM chart, history check of every rule, his portfolio entries/exits, and his trading notes.

---

# Update 4 — Lecture 3 "Option Chain + Basic Candles"

New: `mentor_option_chain.py`, `mentor_option_chain_rules.json`, `mentor_option_chain_tab.py`,
`mentor_oc_watch.py`, `systemd/nifty-mentor-oc-watch.service` + `.timer`. Nothing in the scanner
changes: these only READ the snapshots it already stores (store.load_day).

```bash
.venv/bin/python mentor_option_chain.py --date 2026-09-25     # his reading of a stored session
.venv/bin/python mentor_option_chain.py --backtest            # history check on every stored session
.venv/bin/streamlit run mentor_option_chain_tab.py --server.port 8503

cp systemd/nifty-mentor-oc-watch.* ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now nifty-mentor-oc-watch.timer
```

The watcher runs every 3 minutes (09:20-15:30) and messages Telegram only when the writer side
flips, the price leaves a chain S/R zone, or ITM writers appear / start closing. Alert-only.

---

# Update 5 — Lecture 5 "Imbalance + Order Blocks"

New: `mentor_structure.py`, `mentor_structure_rules.json`, `mentor_structure_tab.py`.
Updated: `mentor_oc_watch.py` (the same 3-minute Telegram watcher now also reports a new BOS /
CHoCH, a new order-block zone, and a zone tap on the 15-min chart). No new timer needed.

```bash
.venv/bin/python mentor_structure.py                 # today's structure + zones
.venv/bin/python mentor_structure.py --backtest      # 1 year of Nifty 15-min candles from Dhan (~5 requests)
.venv/bin/streamlit run mentor_structure_tab.py --server.port 8504
```

The backtest prints a CONTROL row (the same candles in random order): his rules must beat it to
count as an edge. A candle touching both entry and SL is counted as a loss (conservative).

# Update 6 — Lecture 7 "Double bottoms and other important things"

New: `mentor_sweeps.py`, `mentor_sweeps_rules.json`, `mentor_sweeps_tab.py`.
Updated: `mentor_oc_watch.py`. The same 3-minute Telegram watcher now also reports:
- a new two-candle zone that passes 1:1;
- its retest;
- a swing high/low sweep;
- the first 15-min close outside the swing range.

No new timer is needed. Labels corrected: the Update 5 files now say Lecture 5.

```bash
cd "/home/harish/Documents/Swathi K N/1 Trading/option chain/OC"
unzip -o ~/Downloads/mentor_sweeps_v6.zip
.venv/bin/python mentor_sweeps.py                    # month / week / day read + today's 15-min zones
.venv/bin/python mentor_sweeps.py --backtest         # uses the cached Dhan 15-min + daily files (~10 s)
.venv/bin/streamlit run mentor_sweeps_tab.py --server.port 8505
systemctl --user restart nifty-mentor-oc-watch.timer
```

The backtest covers every rule on 15-min, day, week and month, each with a CONTROL row (the same
bars shuffled):
- double bottom/top;
- candle and swing sweeps;
- voids (first return + close rule);
- the swing range at the open.

The output also splits:
- with vs against the day structure;
- his 1:1 filter vs the trades it skips;
- his re-entry after the SL.

Volume climax needs futures candles (the index has no volume), so it isn't backtested.

## Update 6.1 — backtest verdicts in the app and alerts
- Telegram: a swing sweep WITH the day trend (R:R >= 1) comes first as "★ MAIN SIGNAL". Other swing sweeps are
  tagged [secondary]. 15-min two-candle zone alerts carry [weak in backtest: ...].
- Tab: each card shows the backtest verdict for its rule. Every `mentor_sweeps.py --backtest` run (or the tab
  button) saves the verdicts to `data/mentor_sweeps_verdicts.json`, and the cards and alerts use the latest run.
  Until the first run they show the 28-Sep-2026 numbers stored in `mentor_sweeps_rules.json`.

# Update 7 — Lecture 6 "Drawing OB + Voids + Shifting"
Updated: `mentor_structure.py` + rules + tab, `mentor_sweeps.py` + rules + tab, `mentor_oc_watch.py`.
- Structure engine: swings inside the previous swing's range are ignored; SHIFTING (no pullback, straight back
  below the low); older OBs dropped after a new break; re-entry after a wick-only SL; inducement-sweep entry.
  Flags in `mentor_structure_rules.json` (set false for the Lecture 5 behaviour).
- New: body voids (gap between candle bodies, yellow) and the volume-profile divergence (needs your stored
  futures candles in data/candles).
- Telegram: body-void signals, SHIFT events, divergence after 15:00; MIDDLE OBs no longer alerted.
```bash
.venv/bin/python mentor_structure.py --backtest     # new "Lecture 6" table at the end
.venv/bin/python mentor_sweeps.py --backtest        # new body_voids + volume tables and verdicts
systemctl --user restart nifty-mentor-oc-watch.timer
```

# Update 8 — final class (long "Lecture 6" recording)
New: `mentor_extras.py`, `mentor_extras_rules.json` (RSI divergences, close-based double-bottom exit, monthly-expiry
straddle, red-expiry BTST), `inside_bars_mentor.pine` (TradingView inside-candle marker, his valid-candle rule).
Updated: `mentor_sweeps.py` + rules + tab (volume rule split bottom/top, final-class notes and a new expander),
`mentor_structure_tab.py` (notes).
```bash
.venv/bin/python mentor_extras.py --backtest
.venv/bin/python mentor_sweeps.py --backtest      # volume section now splits red-bottom vs top
```
Pine: TradingView -> Pine Editor -> paste inside_bars_mentor.pine -> Add to chart.
