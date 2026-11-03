"""
algo_execution_engine/server.py — FastAPI Application for Setup #1 Algorithmic Execution Service.
Running on Port 8500.

Endpoints:
  - GET  /api/algo/status             -> Full real-time telemetry, active positions, daily P&L, rally state, circuit breakers.
  - GET  /api/algo/positions          -> Active and completed positions today.
  - POST /api/algo/mode               -> Toggle execution mode ("PAPER", "LIVE", "PAUSED").
  - POST /api/algo/emergency-flatten  -> Immediately square off open positions and cancel Dhan SL-M orders.
  - POST /api/algo/reset-circuit-breaker -> Manual override for circuit breaker.
  - GET  /health                      -> Quick health check.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from .engine import Setup1ExecutionEngine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
)
log = logging.getLogger("algo.server")

# Global engine singleton
engine = Setup1ExecutionEngine()


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Starting Setup #1 Algorithmic Execution Service on port 8500...")
    await engine.start()
    yield
    log.info("Shutting down Setup #1 Algorithmic Execution Service...")
    await engine.stop()


app = FastAPI(
    title="Setup #1 Algorithmic Execution Service",
    description="Headless Execution Engine for Stage 2 8 EMA Long with Exchange-Side SL-M",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ModeRequest(BaseModel):
    mode: str  # "PAPER" | "LIVE_1LOT" | "LIVE" | "PAUSED"
    force_override_token: Optional[str] = None


class EmergencyFlattenRequest(BaseModel):
    reason: Optional[str] = "Manual operator flatten"


class AuditScreenshotsRequest(BaseModel):
    directory_path: Optional[str] = None
    output_csv: Optional[str] = None
    lookback_days: Optional[int] = 90
    gemini_api_key: Optional[str] = None


# ─────────────────────────────────────────────────────────────────────────────
# ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/health")
def health() -> Dict[str, Any]:
    return {"status": "ok", "service": "algo_execution_engine_setup1", "port": 8500}


@app.get("/")
def root() -> Dict[str, Any]:
    return {
        "service": "Setup #1 (Stage 2 8 EMA Long) Algorithmic Execution Engine",
        "port": 8500,
        "docs": "/docs",
        "telemetry": "/api/algo/status",
        "stage3_4_dashboard": "/api/algo/stage3-4-dashboard",
    }


@app.get("/api/algo/status")
def get_algo_status() -> Dict[str, Any]:
    """Exposes active positions, daily trade count, P&L, rally counter state, and health check."""
    return engine.get_telemetry_status()


@app.get("/api/algo/stage3-4-dashboard")
def get_stage3_4_dashboard() -> Dict[str, Any]:
    """Exposes Stage 2 Backtest summary, Stage 3 Sandbox 5 Gates, and Stage 4 Live Gatekeeper telemetry."""
    return engine.get_stage3_4_dashboard_data()


@app.get("/api/algo/positions")
def get_positions() -> Dict[str, Any]:
    return {
        "active_position": engine.active_position.to_dict() if engine.active_position else None,
        "closed_positions": engine.closed_positions,
        "daily_trades_count": engine.daily_trades_count,
        "daily_pnl_inr": round(engine.daily_pnl_inr, 2),
    }


@app.post("/api/algo/mode")
def set_execution_mode(req: ModeRequest) -> Dict[str, Any]:
    res = engine.gateway.switch_execution_mode(
        new_mode=req.mode,
        force_override_token=req.force_override_token,
    )
    if not res.get("ok"):
        raise HTTPException(status_code=403, detail=res)
    log.info("Execution mode updated to %s", res.get("execution_mode"))
    return res


@app.post("/api/algo/emergency-flatten")
async def emergency_flatten(req: EmergencyFlattenRequest) -> Dict[str, Any]:
    """Instantly squares off any active position and cancels pending companion SL-M orders."""
    res = await engine.emergency_flatten_all(reason=req.reason or "Manual Emergency Flatten")
    return res


@app.post("/api/algo/reset-circuit-breaker")
def reset_circuit_breaker() -> Dict[str, Any]:
    engine.circuit_breaker_tripped = False
    engine.circuit_breaker_reason = ""
    engine.gateway.circuit_breaker_tripped = False
    engine.gateway.circuit_breaker_reason = ""
    log.warning("Circuit breaker manually reset by operator")
    return {"ok": True, "circuit_breaker": "RESET"}


@app.post("/api/algo/audit-screenshots")
def audit_screenshots_endpoint(req: Optional[AuditScreenshotsRequest] = None) -> Dict[str, Any]:
    """
    Scans directory for daily 15-minute trading homework screenshots,
    classifies setups, checks gap rules, cross-matches with SQLite trade logs,
    and returns the comparison table and CSV export path.
    """
    from .vision_audit import VisionAuditAgent
    payload = req or AuditScreenshotsRequest()
    agent = VisionAuditAgent(gemini_api_key=payload.gemini_api_key)
    res = agent.audit_directory(
        directory_path=payload.directory_path,
        output_csv_path=payload.output_csv,
        lookback_days=payload.lookback_days or 90,
    )
    return res
