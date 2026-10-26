# Kronos forecast sidecar — NIFTY 50

Runs the Kronos foundation model (Tsinghua, arXiv 2508.02739, MIT licence) for the
**🔮 Kronos** tab on the desk at 8600.

## Why it is a separate process

torch plus the weights is ~1.5 GB. The desk on 8600 is what gets traded from; it must not
gain a heavy import or a new way to crash. This sidecar owns torch entirely, in its own
venv, on `127.0.0.1:8701`. If it is down, the Kronos tab says so and **nothing else on the
desk is affected**.

## Start / stop

```bash
cd ict_smc_app/kronos
./run_kronos.sh            # first run creates .venv and installs torch (a few minutes)
./run_kronos.sh stop
```

Then open the desk, go to the **🔮 Kronos** tab, and press **Download the model** once
(~407 MB from the authors' official Hugging Face release; every file is checked against a
pinned SHA-256 before it is used). Weights land in `ict_smc_app/kronos/models/`.

## What it does

| Step | Detail |
|---|---|
| Context | last **360** one-hour NIFTY 50 index candles (~60 sessions) |
| Forecast | next **24** one-hour candles (~4 sessions) |
| Runs | **30** sampled paths, all kept — the spread is the whole point |
| Output | mean path, full band, direction **count**, volatility amplification |

## Two things to keep straight

**The direction figure is a count, not a probability.** "22 of 30 runs ended higher" means
22 of the model's own sampled paths ended higher. It is not a 73% chance NIFTY rises. The
UI says this on every hover.

**NIFTY 50 is an index, so Dhan returns volume 0.** An index has no traded quantity. The
panel therefore reports `n/a (index)` rather than inventing a volume forecast. Point it at
a futures series if you want a real volume read.

## Honest numbers

Published backtests of this model on Indian equities (Tradehull, Oct 2026):

- direction **47.6%** correct on TCS vs **50.5%** for paths built by resampling the stock's
  own past hourly returns — i.e. no better than chance
- the real close finished **outside** the forecast band about **2 times in 3** on CIPLA — the
  band runs too narrow

Use **Step back** (←) and **Reveal** (R) on the tab to check this for yourself on NIFTY
before trusting any forecast. A unanimous 30-of-30 forecast can still be wrong.

24 one-hour candles ≈ 4 sessions, so this is a **positional** read. It has no bearing on
0-DTE or intraday expiry decisions — those live on the PE−CE and expiry cockpit tabs.

## Files

- `kronos_sidecar.py` — FastAPI service: `/health`, `/model/download`, `/forecast`
- `kronos_paths.py` — Kronos inference that keeps every sampled path (MIT, from the Kronos repo)
- `model/` — Kronos model code, vendored unchanged with `LICENSE_KRONOS_MIT.txt`
- `run_kronos.sh` — venv bootstrap + start/stop

Alert-only: this service reads candles and returns numbers. It never places an order.
