"""
kronos/kronos_sidecar.py — Kronos forecasting service, deliberately OUTSIDE the trading desk.

Why a separate process and a separate venv:
  torch + the Kronos weights are ~1.5 GB of dependencies. The desk on 8600 is what Harish
  actually trades from; it must not gain a heavy import, a new Python floor, or a new way to
  crash. This sidecar owns torch entirely. If it is not running, the desk's Kronos tab shows
  "forecast service offline" and everything else on 8600 behaves exactly as before.

Endpoints (localhost only by default):
  GET  /health            model + torch status
  POST /model/download    fetch Kronos-base from the official HF release (SHA-256 pinned)
  POST /forecast          candles in -> N sampled future paths out (no averaging)

Alert-only: this service reads candles and returns numbers. It never places an order.

Kronos: Shi et al., Tsinghua University (arXiv 2508.02739). Model code under MIT licence,
vendored unchanged in ./model/ with its LICENSE file.
"""
from __future__ import annotations

import hashlib
import logging
import os
import sys
import threading
import urllib.request
from typing import Any, Dict, List, Optional

from fastapi import FastAPI
from pydantic import BaseModel

HERE = os.path.dirname(os.path.abspath(__file__))
MODELS = os.path.join(HERE, "models")
sys.path.insert(0, HERE)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("kronos_sidecar")

# The official Kronos release on Hugging Face, pinned by size and SHA-256 so a broken or
# swapped download is refused. Values taken from the published release.
HF = "https://huggingface.co/NeoQuasar/{repo}/resolve/main/{file}"
MODEL_FILES = [
    ("Kronos-Tokenizer-base", "config.json", 301,
     "2366e7ccfec76cbc19cf3c4c1b9c5d901be336ca1e83f2d2292c9bff381b77a2"),
    ("Kronos-Tokenizer-base", "model.safetensors", 15842368,
     "59d85f6af76a2c3b8240ea06cb21db4213b4eeca053f246b23e29cf832fc6bee"),
    ("Kronos-base", "config.json", 228,
     "77ebc3038b647709b92be002f801d72e1a385f4c8c2c5aa1cc6cf21fcfe44eb2"),
    ("Kronos-base", "model.safetensors", 409264008,
     "abff193acab6db1a0368e9773e75799d11403b6d054ee6d5f0a11aeabc5f4b83"),
]

app = FastAPI(title="Kronos forecast sidecar")

_lock = threading.Lock()
_model: Optional[tuple] = None
model_state: Dict[str, Any] = {
    "status": "missing",
    "done": 0,
    "total": sum(f[2] for f in MODEL_FILES),
    "error": "",
}


def _sha(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def model_ready() -> bool:
    for repo, name, size, _ in MODEL_FILES:
        p = os.path.join(MODELS, repo, name)
        if not os.path.exists(p) or os.path.getsize(p) != size:
            return False
    return True


def refresh_model_state() -> None:
    if model_state["status"] != "downloading":
        model_state["status"] = "ready" if model_ready() else "missing"


def download_model() -> None:
    """Fetch each weight file, verify size AND SHA-256, and only then move it into place."""
    if model_state["status"] == "downloading":
        return
    model_state.update(status="downloading", done=0, error="")
    try:
        done = 0
        for repo, name, size, sha in MODEL_FILES:
            dest = os.path.join(MODELS, repo, name)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            if os.path.exists(dest) and os.path.getsize(dest) == size and _sha(dest) == sha:
                done += size
                model_state["done"] = done
                continue
            part = dest + ".part"
            log.info("downloading %s/%s (%.0f MB)", repo, name, size / 1e6)
            with urllib.request.urlopen(HF.format(repo=repo, file=name), timeout=120) as r, open(part, "wb") as f:
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    model_state["done"] = done
            if os.path.getsize(part) != size or _sha(part) != sha:
                os.remove(part)
                raise RuntimeError(f"{repo}/{name} did not match the official Kronos release — download refused")
            os.replace(part, dest)
        model_state["status"] = "ready"
        log.info("model ready in %s", MODELS)
    except Exception as e:
        log.error("model download failed: %s", e)
        model_state.update(status="error", error=str(e)[:300])


def _load():
    """Load tokenizer + model once. Import of kronos_paths (and torch) happens here, not at boot,
    so the sidecar can answer /health and serve the download before torch is even importable."""
    global _model
    with _lock:
        if _model is None:
            import kronos_paths as kp  # noqa: PLC0415 - deliberate lazy import
            threads = int(os.environ.get("KRONOS_THREADS", "6"))
            _model = kp.load(threads=threads, model_dir=MODELS)
            log.info("Kronos-base loaded (%d torch threads)", threads)
    return _model


class Candle(BaseModel):
    ts: int           # epoch seconds, candle start
    o: float
    h: float
    l: float
    c: float
    v: float = 0.0


class ForecastRequest(BaseModel):
    candles: List[Candle]
    pred: int = 24
    paths: int = 30
    temperature: float = 1.0
    top_p: float = 0.9
    top_k: int = 0
    seed: Optional[int] = None
    future_ts: List[int] = []      # candle-start epochs the desk wants forecast (session-aware)


@app.get("/health")
def health() -> Dict[str, Any]:
    refresh_model_state()
    try:
        import torch  # noqa: PLC0415
        torch_v = torch.__version__
    except Exception as e:
        torch_v = f"not installed ({e})"
    return {
        "ok": True,
        "service": "kronos-sidecar",
        "model": model_state,
        "model_dir": MODELS,
        "torch": torch_v,
        "loaded": _model is not None,
    }


@app.post("/model/download")
def start_download() -> Dict[str, Any]:
    refresh_model_state()
    if model_state["status"] == "ready":
        return {"ok": True, "model": model_state}
    threading.Thread(target=download_model, daemon=True).start()
    return {"ok": True, "model": model_state}


@app.post("/forecast")
def forecast(req: ForecastRequest) -> Dict[str, Any]:
    """Returns every sampled path. Averaging is the DESK's job — the spread is the whole point."""
    refresh_model_state()
    if model_state["status"] != "ready":
        return {"ok": False, "error": "model not downloaded", "model": model_state}
    if len(req.candles) < 64:
        return {"ok": False, "error": f"need at least 64 candles, got {len(req.candles)}"}
    if not req.future_ts:
        return {"ok": False, "error": "future_ts (the session-aware forecast timestamps) is required"}

    try:
        import pandas as pd  # noqa: PLC0415
        import kronos_paths as kp  # noqa: PLC0415

        tok, mdl = _load()
        df = pd.DataFrame([{
            "open": c.o, "high": c.h, "low": c.l, "close": c.c, "volume": c.v,
        } for c in req.candles])
        x_ts = pd.to_datetime([c.ts for c in req.candles], unit="s", utc=True).tz_convert("Asia/Kolkata").tz_localize(None)
        y_ts = pd.to_datetime(req.future_ts, unit="s", utc=True).tz_convert("Asia/Kolkata").tz_localize(None)

        z = kp.sample_paths(
            tok, mdl, df,
            pd.Series(x_ts), pd.Series(y_ts),
            n_paths=int(req.paths), T=float(req.temperature),
            top_k=int(req.top_k), top_p=float(req.top_p), seed=req.seed,
        )
        # z: (paths, pred, 6) with COLS = open, high, low, close, volume, amount
        out = [[[round(float(v), 4) for v in step[:5]] for step in path] for path in z]
        return {
            "ok": True,
            "cols": ["open", "high", "low", "close", "volume"],
            "paths": out,
            "future_ts": req.future_ts,
            "settings": {
                "hist": len(req.candles), "pred": req.pred, "paths": req.paths,
                "T": req.temperature, "top_p": req.top_p, "seed": req.seed,
            },
        }
    except Exception as e:
        log.exception("forecast failed")
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


if __name__ == "__main__":
    import uvicorn

    refresh_model_state()
    port = int(os.environ.get("KRONOS_PORT", "8701"))
    log.info("Kronos sidecar on 127.0.0.1:%d — model %s", port, model_state["status"])
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")
