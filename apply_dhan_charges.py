"""
apply_dhan_charges.py — make paper_trading.py's charge engine match Dhan's contract notes exactly.

Verified from your Dhan Global Transaction report (164 rows, Jul–Oct 2026):
  Brokerage ₹20 / executed order · STT 0.15 % sell · Txn 0.03553 % both legs (NSE 0.03503 % + IPFT 0.0005 %)
  SEBI 0.0001 % both · Stamp 0.003 % buy · GST 18 % of (brokerage + txn incl. IPFT + SEBI + other charges)

Changes (backup paper_trading.py.bak_dhancharges, compile-checked, safe to run twice):
  • built-in defaults: STT 0.10 % → 0.15 %, IPFT 0.00005 % → 0.0005 %
  • GST base now includes IPFT (Dhan's "Txn. Charges" column already contains it)
  • data/paper_trading/paper_config.json: old STT / IPFT values upgraded (other settings untouched)

  .venv/bin/python apply_dhan_charges.py --check
  .venv/bin/python apply_dhan_charges.py
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).parent
P = ROOT / "paper_trading.py"
MARK = "DHANCHARGES"
EDITS = [
    ('    "stt_sell_rate": 0.001,', '    "stt_sell_rate": 0.0015,              # DHANCHARGES: 0.15 % since 1 Apr 2026'),
    ('    "ipft_rate": 0.0000005,', '    "ipft_rate": 0.000005,                # DHANCHARGES: IPFT 0.0005 %'),
    ('cfg.get("stt_sell_rate", 0.001)', 'cfg.get("stt_sell_rate", 0.0015)'),
    ('cfg.get("ipft_rate", 0.0000005)', 'cfg.get("ipft_rate", 0.000005)'),
    ('    gst_base = brokerage + exchange_fee + sebi_fee',
     '    gst_base = brokerage + exchange_fee + ipft_fee + sebi_fee   # DHANCHARGES: Dhan GST base includes IPFT'),
    ('      - STT: 0.1% strictly on Sell-side premium turnover (Buy side is ₹0)',
     '      - STT: 0.15% strictly on Sell-side premium turnover (Buy side is ₹0)'),
    ('      - IPFT: ₹0.5 per crore (0.00005%) on total premium turnover',
     '      - IPFT: 0.0005% on total premium turnover (Dhan txn = 0.03503% + 0.0005% = 0.03553%)'),
]


def main():
    check = "--check" in sys.argv
    if not P.exists():
        print("✘ paper_trading.py not found — run from the OC folder"); return
    t = P.read_text(encoding="utf-8")
    if MARK in t:
        print("• paper_trading.py: already done")
    else:
        missing = [a for a, _ in EDITS if t.count(a) != 1]
        if missing:
            print("✘ paper_trading.py differs from the version I know — NOT changed. Missing:", *missing[:3], sep="\n   ")
            return
        new = t
        for a, b in EDITS:
            new = new.replace(a, b, 1)
        compile(new, "paper_trading.py", "exec")
        if check:
            print("✔ paper_trading.py: can be updated")
        else:
            shutil.copy2(P, P.with_name(P.name + ".bak_dhancharges"))
            P.write_text(new, encoding="utf-8")
            print("✔ paper_trading.py: charges now match Dhan (backup paper_trading.py.bak_dhancharges)")
    cf = ROOT / "data" / "paper_trading" / "paper_config.json"
    if cf.exists() and not check:
        c = json.loads(cf.read_text())
        ch = []
        if abs(float(c.get("stt_sell_rate", 0.0015)) - 0.001) < 1e-9:
            c["stt_sell_rate"] = 0.0015; ch.append("STT 0.15 %")
        if abs(float(c.get("ipft_rate", 0.000005)) - 0.0000005) < 1e-12:
            c["ipft_rate"] = 0.000005; ch.append("IPFT 0.0005 %")
        if ch:
            cf.write_text(json.dumps(c, indent=2))
        print("✔ paper_config.json:", ", ".join(ch) if ch else "already correct")
    if not check:
        print("Done — restart the dashboard / phone desk.")


if __name__ == "__main__":
    main()
