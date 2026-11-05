"""
algo_execution_engine/server.py — FastAPI Application for Setup #1 Algorithmic Execution Service.
Running on Port 8500.

Endpoints:
  - GET  /api/algo/status             -> Full real-time telemetry, active positions, daily P&L, rally state, circuit breakers.
  - GET  /api/algo/positions          -> Active and completed positions today.
  - POST /api/algo/mode               -> Toggle execution mode ("PAPER", "LIVE", "PAUSED") [Requires X-Algo-Secret].
  - POST /api/algo/emergency-flatten  -> Immediately square off open positions and cancel Dhan SL-M orders [Requires X-Algo-Secret].
  - POST /api/algo/reset-circuit-breaker -> Manual override for circuit breaker [Requires X-Algo-Secret].
  - GET  /health                      -> Quick health check.
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from .engine import Setup1ExecutionEngine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
)
log = logging.getLogger("algo.server")

# Engine singleton managed in FastAPI lifespan (never at module scope)
engine: Optional[Setup1ExecutionEngine] = None


def get_engine() -> Setup1ExecutionEngine:
    """Provides engine instance, instantiating lazily if accessed outside lifespan in test contexts."""
    global engine
    if engine is None:
        engine = Setup1ExecutionEngine()
    return engine


def verify_admin_secret(req: Request) -> None:
    """Enforces shared-secret header from .env on sensitive administrative actions."""
    expected_secret = os.environ.get("ALGO_SHARED_SECRET", "").strip()
    provided_secret = (req.headers.get("X-Algo-Secret") or req.headers.get("x-algo-secret") or "").strip()
    if not expected_secret or provided_secret != expected_secret:
        log.warning("🚨 Unauthorized administrative request blocked: Invalid or missing X-Algo-Secret header.")
        raise HTTPException(
            status_code=401,
            detail={"ok": False, "error": "Unauthorized: Invalid or missing X-Algo-Secret header"}
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    global engine
    log.info("Starting Setup #1 Algorithmic Execution Service on port 8500...")
    engine = Setup1ExecutionEngine()
    await engine.start()
    yield
    log.info("Shutting down Setup #1 Algorithmic Execution Service...")
    if engine is not None:
        await engine.stop()


app = FastAPI(
    title="Setup #1 Algorithmic Execution Service",
    description="Headless Execution Engine for Stage 2 8 EMA Long with Exchange-Side SL-M",
    version="1.0.0",
    lifespan=lifespan,
)

# Localhost origins only. An external website (e.g. stockmojo.in) must NEVER call local order engines.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:8500",
        "http://127.0.0.1:8500",
        "http://localhost:8510",
        "http://127.0.0.1:8510",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ModeRequest(BaseModel):
    mode: str  # "PAPER" | "LIVE_1LOT" | "LIVE" | "PAUSED"


class EmergencyFlattenRequest(BaseModel):
    reason: Optional[str] = "Manual operator flatten"


class AuditScreenshotsRequest(BaseModel):
    directory_path: Optional[str] = None
    output_csv: Optional[str] = None
    lookback_days: Optional[int] = 90


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
    return get_engine().get_telemetry_status()


@app.get("/api/algo/stage3-4-dashboard")
def get_stage3_4_dashboard() -> Dict[str, Any]:
    """Exposes Stage 2 Backtest summary, Stage 3 Sandbox 5 Gates, and Stage 4 Live Gatekeeper telemetry."""
    return get_engine().get_stage3_4_dashboard_data()


@app.get("/api/algo/positions")
def get_positions() -> Dict[str, Any]:
    eng = get_engine()
    return {
        "active_position": eng.active_position.to_dict() if eng.active_position else None,
        "closed_positions": eng.closed_positions,
        "daily_trades_count": eng.daily_trades_count,
        "daily_pnl_inr": round(eng.daily_pnl_inr, 2),
    }


@app.post("/api/algo/mode", dependencies=[Depends(verify_admin_secret)])
def set_execution_mode(req: ModeRequest) -> Dict[str, Any]:
    eng = get_engine()
    res = eng.gateway.switch_execution_mode(new_mode=req.mode)
    if not res.get("ok"):
        raise HTTPException(status_code=403, detail=res)
    log.info("Execution mode updated to %s", res.get("execution_mode"))
    return res


@app.post("/api/algo/emergency-flatten", dependencies=[Depends(verify_admin_secret)])
async def emergency_flatten(req: Optional[EmergencyFlattenRequest] = None) -> Dict[str, Any]:
    """Instantly squares off any active position and cancels pending companion SL-M orders."""
    eng = get_engine()
    reason = (req.reason if req else None) or "Manual Emergency Flatten"
    res = await eng.emergency_flatten_all(reason=reason)
    return res


@app.post("/api/algo/reset-circuit-breaker", dependencies=[Depends(verify_admin_secret)])
def reset_circuit_breaker() -> Dict[str, Any]:
    eng = get_engine()
    eng.circuit_breaker_tripped = False
    eng.circuit_breaker_reason = ""
    eng.gateway.circuit_breaker_tripped = False
    eng.gateway.circuit_breaker_reason = ""
    log.warning("Circuit breaker manually reset by operator")
    return {"ok": True, "circuit_breaker": "RESET"}


@app.post("/api/algo/audit-screenshots")
def audit_screenshots_endpoint(req: Optional[AuditScreenshotsRequest] = None) -> Dict[str, Any]:
    """
    Scans directory for daily 15-minute trading homework screenshots,
    classifies setups, checks gap rules, cross-matches with SQLite trade logs,
    and returns the comparison table and CSV export path.
    Gemini API key is read strictly from .env.
    """
    from .vision_audit import VisionAuditAgent
    payload = req or AuditScreenshotsRequest()
    agent = VisionAuditAgent(gemini_api_key=os.environ.get("GEMINI_API_KEY"))
    res = agent.audit_directory(
        directory_path=payload.directory_path,
        output_csv_path=payload.output_csv,
        lookback_days=payload.lookback_days or 90,
    )
    return res
