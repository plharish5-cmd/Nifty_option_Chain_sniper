#!/usr/bin/env python3
"""Run the pre-registered advisory study; no order or broker calls."""
import sys,argparse,json
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from analyst.trend_capture_evaluation_8510 import run_study
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--market');p.add_argument('--ledger');p.add_argument('--mfe');p.add_argument('--output',required=True);a=p.parse_args()
 r=run_study(a.mfe,a.market,a.ledger,a.output)
 print(json.dumps({k:v for k,v in r.items() if k not in ('stage2b_trades','mfe_summary','sessions')},indent=2,allow_nan=False))
