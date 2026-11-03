"""
algo_execution_engine/main.py — Standalone Runner for Port 8500.

Usage:
  python3 -m algo_execution_engine.main
  or
  uvicorn algo_execution_engine.server:app --host 0.0.0.0 --port 8500
"""
import sys
from pathlib import Path
import uvicorn

from .strategy_setup1 import load_config


def run() -> None:
    cfg = load_config()
    host = str(cfg.get("service", {}).get("host", "0.0.0.0"))
    port = int(cfg.get("service", {}).get("port", 8500))

    print(f"🚀 Launching Setup #1 Algorithmic Execution Service on http://{host}:{port} ...")
    uvicorn.run("algo_execution_engine.server:app", host=host, port=port, log_level="info")


if __name__ == "__main__":
    run()
