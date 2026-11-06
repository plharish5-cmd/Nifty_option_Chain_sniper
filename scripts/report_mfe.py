#!/usr/bin/env python3
"""Read-only censored and post-entry horizon MFE report; no orders or ledger writes."""
import sys,json,argparse
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from analyst.trend_capture_research_8510 import report_mfe

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--market');ap.add_argument('--ledger');ap.add_argument('--output');args=ap.parse_args()
    report=report_mfe(args.market,args.ledger)
    text=json.dumps(report,indent=2,allow_nan=False)
    if args.output:Path(args.output).write_text(text+'\n')
    concise={k:v for k,v in report.items() if k not in ('rows','session_labels')}
    print(json.dumps(concise,indent=2,allow_nan=False))
    return 0
if __name__=='__main__':raise SystemExit(main())
