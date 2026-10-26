# Equivalence check: does the desk's default fast (KV-cached) Kronos loop produce the SAME
# candles as the reference auto-regressive loop? This is the single riskiest assumption in the
# desk, because sample_paths(..., fast=True) is the default and the band/up-prob are built from
# whatever it returns. We do NOT need the 407 MB trained weights for this: equivalence is a
# property of the two inference implementations given IDENTICAL weights, so a small random-init
# Kronos with the real architecture (model/module.py, model/kronos.py) exercises the exact code
# paths _infer and _infer_fast take. Same torch seed -> identical sampled tokens -> identical
# candles, or the fast path is wrong.
import numpy as np
import pandas as pd
import torch

import kronos_paths as kp
from model import Kronos, KronosTokenizer

torch.manual_seed(0)

# Small but architecturally real config (same class args as Kronos-base, scaled down for CPU).
S1, S2, GROUP = 6, 6, 3           # codebook_dim = 12, divisible by group_size 3
D_MODEL, N_HEADS, FF, NLAYERS = 48, 4, 96, 2
tok = KronosTokenizer(d_in=6, d_model=D_MODEL, n_heads=N_HEADS, ff_dim=FF,
                      n_enc_layers=2, n_dec_layers=2, ffn_dropout_p=0.0, attn_dropout_p=0.0,
                      resid_dropout_p=0.0, s1_bits=S1, s2_bits=S2, beta=0.0, gamma0=1.0,
                      gamma=1.0, zeta=1.0, group_size=GROUP).eval()
mdl = Kronos(s1_bits=S1, s2_bits=S2, n_layers=NLAYERS, d_model=D_MODEL, n_heads=N_HEADS,
             ff_dim=FF, ffn_dropout_p=0.0, attn_dropout_p=0.0, resid_dropout_p=0.0,
             token_dropout_p=0.0, learn_te=False).eval()

# Synthetic OHLCV history: a gentle random walk, 360 hourly bars, with the real IST slot grid.
HIST, PRED, N, MAXCTX = 360, 24, 8, 512
rng = np.random.default_rng(1)
close = 20000 + np.cumsum(rng.normal(0, 25, HIST))
hi = close + np.abs(rng.normal(0, 10, HIST)); lo = close - np.abs(rng.normal(0, 10, HIST))
op = close + rng.normal(0, 5, HIST); vol = np.abs(rng.normal(1e5, 2e4, HIST))
past = pd.DataFrame(dict(open=op, high=hi, low=lo, close=close, volume=vol))

SLOTS = ["09:15", "10:15", "11:15", "12:15", "13:15", "14:15"]  # desk's 6 true-60min slots
def grid(n, start):
    out, d, k = [], start.normalize(), SLOTS.index(start.strftime("%H:%M"))
    while len(out) < n:
        if k == len(SLOTS):
            d, k = d + pd.Timedelta(days=1), 0
            while d.weekday() >= 5: d += pd.Timedelta(days=1)
            continue
        hh, mm = map(int, SLOTS[k].split(":")); out.append(d + pd.Timedelta(hours=hh, minutes=mm)); k += 1
    return pd.Series(out)
x_ts = grid(HIST, pd.Timestamp("2025-01-02 09:15"))
y_ts = grid(PRED + 1, x_ts.iloc[-1]).iloc[1:].reset_index(drop=True)

res = {}
for name, fast in (("reference (slow)", False), ("desk default (fast)", True)):
    torch.manual_seed(7)                       # identical RNG -> identical sampling
    res[name] = kp.sample_paths(tok, mdl, past, x_ts, y_ts, n_paths=N, T=1.0, top_p=0.9,
                                max_context=MAXCTX, seed=7, fast=fast)
    print(f"{name:20s} shape {res[name].shape}")

a, b = res["reference (slow)"], res["desk default (fast)"]
diff = np.abs(a - b)
print("\nmax abs diff over all paths/candles/channels :", float(diff.max()))
print("max abs diff on CLOSE channel (price points) :", float(diff[:, :, 3].max()))
print("close, path 0, first 6 candles")
print("  slow:", np.round(a[0, :6, 3], 3))
print("  fast:", np.round(b[0, :6, 3], 3))
print("\nVERDICT:", "PASS - fast loop == reference loop" if diff.max() < 1e-3
      else "FAIL - fast loop DIVERGES from reference")

# Also prove the assert fires exactly where predicted: hist + pred must be <= max_context.
print("\n--- context-window assert boundary (fast path) ---")
for h in (MAXCTX - PRED, MAXCTX - PRED + 1):
    try:
        xts2 = grid(h, pd.Timestamp("2025-01-02 09:15"))
        yts2 = grid(PRED + 1, xts2.iloc[-1]).iloc[1:].reset_index(drop=True)
        p2 = past.iloc[:1].reindex(range(h)).ffill().reset_index(drop=True)
        torch.manual_seed(1)
        kp.sample_paths(tok, mdl, p2, xts2, yts2, n_paths=2, max_context=MAXCTX, fast=True)
        print(f"  hist={h:3d} + pred={PRED} = {h+PRED:3d}  -> OK")
    except AssertionError as e:
        print(f"  hist={h:3d} + pred={PRED} = {h+PRED:3d}  -> AssertionError: {e}")
