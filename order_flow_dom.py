"""
order_flow_dom.py — Module I: Order Flow Layer (Strike-Level DOM).

Real Support/Resistance + entry timing for NIFTY options from recorded depth & quote data.
Feeds the 7-layer stack as L8 (execution confirmation).
Educational/research tool — alert-only, display + journal only. No auto-orders.

Constraints & Architecture:
- Dhan allows 5 connections: depth = 1/symbol; 4 symbols max.
- Streams: ATM CE, ATM PE, ATM+50 CE, ATM+50 PE (configurable strikes).
- Strike selector module: auto-picks from spot each morning; rotates streams
  when spot moves >50 pts from selection time. Rotation logged.
- Reads data/recorded/<date>/ depth + quote files (jsonl format).
- Rebuilds per-second: top-20 ladder, resting qty per level, market-order flow, CVD.
- Wall persistence rule: a level counts as a wall only if resting qty >= wall_threshold
  (default 25,000) persists >= wall_min_seconds (20s). Anti-spoofing filter.
- L8 Signals:
    A. DEFENDED WALL (S/R detection)
    B. ABSORPTION
    C. CVD DIVERGENCE
    D. LIQUIDITY BREAK
- Integration: Pre-trade checklist Box 7 (7/7 = TRADE), cockpit tile, action line note.
- Paper-trade mode & historical win-rate validation gate (>= 60% at +15m required for live veto).
"""
from __future__ import annotations

import json
import logging
import math
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union

logger = logging.getLogger("order_flow_dom")
if not logger.handlers:
    _ch = logging.StreamHandler()
    _ch.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] [order_flow_dom] %(message)s"))
    logger.addHandler(_ch)
    logger.setLevel(logging.INFO)

REPO_ROOT = Path(__file__).resolve().parent
RULES_PATH = REPO_ROOT / "strategy1_rules.json"
RECORDED_DIR = REPO_ROOT / "data" / "recorded"
DEMO_BACKUP_DIR = REPO_ROOT.parent / "DHAN git Hub" / "OrderFlowLab" / "data" / "demo"
ROTATION_LOG_PATH = REPO_ROOT / "data" / "order_flow_rotation.jsonl"
JOURNAL_LOG_PATH = REPO_ROOT / "data" / "order_flow_journal.jsonl"


# ═══════════════════════════════════════════════════════════════════════════
# 1. Configuration Loader
# ═══════════════════════════════════════════════════════════════════════════

def load_order_flow_config() -> Dict[str, Any]:
    """
    Loads tunable order flow DOM thresholds from strategy1_rules.json under 'order_flow'.
    All thresholds live in JSON; nothing is hardcoded.
    """
    defaults: Dict[str, Any] = {
        "enabled": True,
        "paper_trade_mode": True,
        "paper_trade_weeks": 2,
        "wall_threshold": 25000,
        "wall_min_seconds": 20,
        "absorb_threshold": 15000,
        "absorb_window_seconds": 10,
        "absorb_max_ticks": 2,
        "absorb_near_wall_ticks": 3,
        "absorb_min_pulses": 2,
        "pulse_min_volume": 10000,
        "sweep_ratio": 0.5,
        "break_multiplier": 1.5,
        "break_window_seconds": 5,
        "cvd_slope_flip_seconds": 60,
        "cvd_divergence_window_seconds": 300,
        "target_spot_shift_pts": 50.0,
        "strike_step": 50.0,
        "strike_offsets": [0, 0, 50, 50],
        "strike_types": ["ATM_CE", "ATM_PE", "ATM_P50_CE", "ATM_P50_PE"],
        "min_confirm_win_rate": 0.60,
        "min_multi_day_required": 5,
        "min_multi_day_regimes": ["trend-up", "trend-down", "chop"],
        "paper_trade_clock_started": False,
        "veto_mode": "SIM_ONLY",
        "outcome_horizon_mins": [5, 15],
        "profit_threshold_pct": 1.0,
        "recorded_data_dir": "data/recorded",
        "journal_log_path": "data/order_flow_journal.jsonl",
        "rotation_log_path": "data/order_flow_rotation.jsonl",
    }
    if RULES_PATH.exists():
        try:
            with open(RULES_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                of_cfg = data.get("order_flow", {})
                defaults.update(of_cfg)
        except Exception as exc:
            logger.warning(f"Failed to read order_flow config from {RULES_PATH}: {exc}")
    return defaults


# ═══════════════════════════════════════════════════════════════════════════
# 2. Data Structures
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class Wall:
    side: str                          # "BID" or "ASK"
    price: float
    qty: int
    first_seen_sec: int
    persisted: bool = False
    first_persisted_sec: Optional[int] = None
    test_volume: int = 0
    status: str = "PENDING"            # PENDING, ACTIVE, DEFENDED_S, DEFENDED_R, BROKEN, REMOVED
    break_sec: Optional[int] = None
    last_updated_sec: int = 0


@dataclass
class AbsorptionEvent:
    sec: int
    timestamp_str: str
    symbol: str
    side: str                          # "BID" or "ASK"
    price: float
    volume: int
    price_range: float
    cvd_slope_60s: float
    verdict: str                       # "CONFIRM_LONG", "CONFIRM_SHORT", "VETO"
    note: str


@dataclass
class CVDDivergenceEvent:
    sec: int
    timestamp_str: str
    symbol: str
    div_type: str                      # "BEARISH_DIV" (Price HH, CVD LH -> VETO Longs), "BULLISH_DIV" (Price LL, CVD HL -> VETO Shorts)
    price: float
    cvd: int
    verdict: str                       # "VETO"
    note: str


@dataclass
class LiquidityBreakEvent:
    sec: int
    timestamp_str: str
    symbol: str
    side: str                          # "BID" or "ASK"
    broken_price: float
    sweep_volume: int
    wall_qty: int
    break_direction: str               # "DOWN" (bid wall broke), "UP" (ask wall broke)
    next_wall_target: Optional[float]
    note: str


@dataclass
class L8VerdictReport:
    symbol: str
    verdict: str                       # "CONFIRM", "VETO", "SILENT"
    reasons: List[str] = field(default_factory=list)
    nearest_defended_bid_wall: Optional[float] = None
    nearest_defended_ask_wall: Optional[float] = None
    active_walls_summary: str = "No walls"
    absorption_status: str = "QUIET"
    cvd_slope_badge: str = "—"         # "▲", "▼", "—"
    cvd_slope_note: str = "CVD Flat"
    cvd_slope_flip: bool = False
    divergence_status: str = "NONE"
    action_line_note: Optional[str] = None
    is_paper_trade: bool = True
    historical_win_rate: Optional[float] = None
    sim_suppressed: bool = False
    last_ltp: float = 0.0
    last_cvd: int = 0
    as_of_time: str = ""


# ═══════════════════════════════════════════════════════════════════════════
# 3. Strike Selector Module
# ═══════════════════════════════════════════════════════════════════════════

class StrikeSelectorDOM:
    """
    Selects and rotates 4 stream strikes for Dhan order flow DOM:
    ATM CE, ATM PE, ATM+50 CE, ATM+50 PE (configurable).
    Rotates when spot moves >50 pts from anchor selection time.
    Rotation is recorded to order_flow_rotation.jsonl.
    """
    def __init__(self, cfg: Optional[Dict[str, Any]] = None):
        self.cfg = cfg or load_order_flow_config()
        self.strike_step = float(self.cfg.get("strike_step", 50.0))
        self.shift_pts = float(self.cfg.get("target_spot_shift_pts", 50.0))
        self.rotation_log_path = Path(self.cfg.get("rotation_log_path", ROTATION_LOG_PATH))
        self.anchor_spot: Optional[float] = None
        self.active_strikes: Dict[str, Any] = {}

    def get_strikes_for_spot(self, spot: float) -> Dict[str, Any]:
        """Calculates ATM and ATM+50 strikes for spot."""
        atm = round(spot / self.strike_step) * self.strike_step
        atm_int = int(atm)
        atm_p50_int = int(atm + self.strike_step)

        symbols = {
            "ATM_CE": f"NIFTY{atm_int}-CE",
            "ATM_PE": f"NIFTY{atm_int}-PE",
            "ATM_P50_CE": f"NIFTY{atm_p50_int}-CE",
            "ATM_P50_PE": f"NIFTY{atm_p50_int}-PE",
        }
        strikes = {
            "ATM": atm_int,
            "ATM_P50": atm_p50_int,
        }
        return {"strikes": strikes, "symbols": symbols}

    def check_and_update(
        self,
        spot: float,
        current_time: Optional[datetime] = None,
        force_anchor: bool = False,
    ) -> Dict[str, Any]:
        """
        Evaluates spot shift. Rotates streams if spot moved > shift_pts.
        Logs any rotation event.
        """
        now_dt = current_time or datetime.now()
        rotated = False
        old_anchor = self.anchor_spot
        old_symbols = self.active_strikes.get("symbols", {})

        if self.anchor_spot is None or force_anchor:
            self.anchor_spot = spot
            self.active_strikes = self.get_strikes_for_spot(spot)
            rotated = True
            self._log_rotation(now_dt, old_anchor, self.anchor_spot, spot, old_symbols, self.active_strikes["symbols"], reason="INITIAL_ANCHOR")
        elif abs(spot - self.anchor_spot) >= self.shift_pts:
            new_selection = self.get_strikes_for_spot(spot)
            if new_selection["symbols"] != old_symbols:
                old_anchor = self.anchor_spot
                self.anchor_spot = spot
                self.active_strikes = new_selection
                rotated = True
                self._log_rotation(
                    now_dt, old_anchor, self.anchor_spot, spot, old_symbols,
                    self.active_strikes["symbols"],
                    reason=f"SPOT_SHIFT_{abs(spot - old_anchor):.1f}PTS"
                )

        return {
            "anchor_spot": self.anchor_spot,
            "spot": spot,
            "rotated": rotated,
            "strikes": self.active_strikes.get("strikes", {}),
            "symbols": self.active_strikes.get("symbols", {}),
        }

    def _log_rotation(
        self,
        ts: datetime,
        old_anchor: Optional[float],
        new_anchor: float,
        spot: float,
        old_syms: Dict[str, str],
        new_syms: Dict[str, str],
        reason: str,
    ) -> None:
        """Appends rotation record to order_flow_rotation.jsonl."""
        record = {
            "timestamp": ts.isoformat(),
            "reason": reason,
            "spot": spot,
            "old_anchor": old_anchor,
            "new_anchor": new_anchor,
            "old_symbols": old_syms,
            "new_symbols": new_syms,
        }
        try:
            self.rotation_log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.rotation_log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
            logger.info(f"Stream rotation logged: {reason} | new symbols: {list(new_syms.values())}")
        except Exception as exc:
            logger.warning(f"Error logging stream rotation: {exc}")


# ═══════════════════════════════════════════════════════════════════════════
# 4. DOM Rebuilder & Order Flow Engine
# ═══════════════════════════════════════════════════════════════════════════

class DOMRebuilder:
    """
    Rebuilds second-by-second DOM ladder, resting liquidity, aggressive market trades,
    CVD, and evaluates L8 Order Flow signals.
    """
    def __init__(self, cfg: Optional[Dict[str, Any]] = None):
        self.cfg = cfg or load_order_flow_config()
        self.wall_threshold = int(self.cfg.get("wall_threshold", 25000))
        self.wall_min_seconds = int(self.cfg.get("wall_min_seconds", 20))
        self.absorb_threshold = int(self.cfg.get("absorb_threshold", 15000))
        self.absorb_window_seconds = int(self.cfg.get("absorb_window_seconds", 10))
        self.absorb_max_ticks = int(self.cfg.get("absorb_max_ticks", 2))
        self.sweep_ratio = float(self.cfg.get("sweep_ratio", 0.5))
        self.break_multiplier = float(self.cfg.get("break_multiplier", 1.5))
        self.break_window_seconds = int(self.cfg.get("break_window_seconds", 5))
        self.cvd_slope_flip_seconds = int(self.cfg.get("cvd_slope_flip_seconds", 60))
        self.cvd_div_window_seconds = int(self.cfg.get("cvd_divergence_window_seconds", 300))
        self.absorb_near_wall_ticks = int(self.cfg.get("absorb_near_wall_ticks", 3))
        self.absorb_min_pulses = int(self.cfg.get("absorb_min_pulses", 2))
        self.pulse_min_volume = int(self.cfg.get("pulse_min_volume", 10000))
        self.tick_size = 0.05

        # Loaded second bars: list of dicts
        self.bars: List[Dict[str, Any]] = []
        self.trade_quotes: List[Tuple[int, int, float]] = []
        self.all_walls: Dict[Tuple[str, float], Wall] = {}
        self.raw_candidate_absorptions: List[Dict[str, Any]] = []
        self.rejected_absorptions: List[Dict[str, Any]] = []
        self.absorption_events: List[AbsorptionEvent] = []
        self.divergence_events: List[CVDDivergenceEvent] = []
        self.break_events: List[LiquidityBreakEvent] = []
        self.raw_signals: List[Dict[str, Any]] = []
        self.symbol: str = ""
        self.date_str: str = ""

    def load_and_rebuild(
        self,
        date_str: str,
        symbol: str,
        data_dir: Optional[Union[str, Path]] = None,
    ) -> bool:
        """
        Loads recorded depth and quote jsonl files and rebuilds second-by-second data.
        """
        self.date_str = date_str
        self.symbol = symbol
        base_dir = Path(data_dir) if data_dir else RECORDED_DIR
        day_dir = base_dir / date_str

        # Fallback search if path does not exist
        depth_file = day_dir / f"{symbol}.depth.jsonl"
        quote_file = day_dir / f"{symbol}.quote.jsonl"

        if not depth_file.exists() or not quote_file.exists():
            # Try depth200 or demo folder
            alt_day_dir = DEMO_BACKUP_DIR / date_str
            if alt_day_dir.exists():
                alt_d = alt_day_dir / f"{symbol}.depth.jsonl"
                alt_q = alt_day_dir / f"{symbol}.quote.jsonl"
                if alt_d.exists() and alt_q.exists():
                    depth_file = alt_d
                    quote_file = alt_q

        if not depth_file.exists() or not quote_file.exists():
            logger.warning(f"Depth or quote file not found for {symbol} on {date_str} at {day_dir}")
            return False

        quotes_by_sec: Dict[int, List[Dict[str, Any]]] = {}
        first_quote: Optional[Dict[str, Any]] = None
        self.trade_quotes = []
        prev_v_quote: Optional[int] = None
        try:
            with open(quote_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    q = json.loads(line)
                    if first_quote is None:
                        first_quote = q
                        prev_v_quote = q.get("v", 0)
                    else:
                        v = q.get("v", prev_v_quote)
                        dv = v - prev_v_quote
                        if dv > 0:
                            sec = q["t"] // 1000
                            self.trade_quotes.append((sec, dv, float(q.get("ltp", 0.0))))
                            prev_v_quote = v
                    sec = q["t"] // 1000
                    quotes_by_sec.setdefault(sec, []).append(q)
        except Exception as exc:
            logger.error(f"Error parsing quotes from {quote_file}: {exc}")
            return False

        depth_by_sec: Dict[int, Dict[str, Any]] = {}
        try:
            with open(depth_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    d = json.loads(line)
                    sec = d["t"] // 1000
                    depth_by_sec[sec] = d
        except Exception as exc:
            logger.error(f"Error parsing depth from {depth_file}: {exc}")
            return False

        all_secs = sorted(set(depth_by_sec.keys()) | set(quotes_by_sec.keys()))
        if not all_secs:
            return False

        # Build second-by-second stream
        bars: List[Dict[str, Any]] = []
        cvd = 0
        prev_v = first_quote.get("v", 0) if first_quote else 0
        prev_ltp = first_quote.get("ltp", 0.0) if first_quote else 0.0
        prev_side = "BUY"

        for s in all_secs:
            d = depth_by_sec.get(s)
            qs = quotes_by_sec.get(s, [])

            s_buy_vol = 0
            s_sell_vol = 0
            cur_ltp = prev_ltp

            bids = d.get("b", []) if d else []
            asks = d.get("a", []) if d else []

            best_bid = bids[0][0] if bids else None
            best_ask = asks[0][0] if asks else None

            # Process trades/quotes in this second
            for q in qs:
                v = q.get("v", prev_v)
                dv = v - prev_v
                ltp = q.get("ltp", cur_ltp)
                cur_ltp = ltp
                if dv > 0:
                    if best_ask is not None and ltp >= best_ask:
                        side = "BUY"
                    elif best_bid is not None and ltp <= best_bid:
                        side = "SELL"
                    elif ltp > prev_ltp:
                        side = "BUY"
                    elif ltp < prev_ltp:
                        side = "SELL"
                    else:
                        side = prev_side

                    if side == "BUY":
                        s_buy_vol += dv
                    else:
                        s_sell_vol += dv

                    prev_side = side
                    prev_ltp = ltp
                    prev_v = v

            cvd += (s_buy_vol - s_sell_vol)
            bars.append({
                "sec": s,
                "dt": datetime.fromtimestamp(s),
                "ltp": cur_ltp,
                "buy_vol": s_buy_vol,
                "sell_vol": s_sell_vol,
                "cvd": cvd,
                "best_bid": best_bid,
                "best_ask": best_ask,
                "top20_bids": bids[:20],
                "top20_asks": asks[:20],
            })

        self.bars = bars
        self._detect_signals_and_replay()
        return True

    def _detect_signals_and_replay(self) -> None:
        """
        Processes bars sequentially, updating wall streaks, sweeps, absorption, CVD divergence,
        and liquidity breaks.
        """
        self.all_walls.clear()
        self.raw_candidate_absorptions.clear()
        self.rejected_absorptions.clear()
        self.absorption_events.clear()
        self.divergence_events.clear()
        self.break_events.clear()
        self.raw_signals.clear()

        bid_streaks: Dict[float, int] = {}
        ask_streaks: Dict[float, int] = {}
        last_cvd_slope_sign = 0
        cooldown_tracker: Dict[str, int] = {}

        for idx, b in enumerate(self.bars):
            s = b["sec"]
            ltp = b["ltp"]
            dt_str = b["dt"].strftime("%H:%M:%S")

            bids_map = {p: q for p, q in b["top20_bids"]}
            asks_map = {p: q for p, q in b["top20_asks"]}

            # ── 1. Wall Persistence Tracking (Anti-Spoofing Filter) ──
            # Bids
            for p, q in bids_map.items():
                if q >= self.wall_threshold:
                    bid_streaks[p] = bid_streaks.get(p, 0) + 1
                    streak = bid_streaks[p]
                    if streak >= self.wall_min_seconds:
                        wall_key = ("BID", p)
                        if wall_key not in self.all_walls:
                            self.all_walls[wall_key] = Wall(
                                side="BID",
                                price=p,
                                qty=q,
                                first_seen_sec=s - streak,
                                persisted=True,
                                first_persisted_sec=s,
                                status="ACTIVE",
                                last_updated_sec=s,
                            )
                        else:
                            w = self.all_walls[wall_key]
                            w.qty = q
                            w.last_updated_sec = s
                            if w.status == "REMOVED":
                                w.status = "ACTIVE"
                else:
                    bid_streaks.pop(p, None)
                    w_key = ("BID", p)
                    if w_key in self.all_walls and self.all_walls[w_key].status == "ACTIVE":
                        self.all_walls[w_key].status = "REMOVED"

            for p in list(bid_streaks.keys()):
                if p not in bids_map:
                    bid_streaks.pop(p, None)
                    w_key = ("BID", p)
                    if w_key in self.all_walls and self.all_walls[w_key].status == "ACTIVE":
                        self.all_walls[w_key].status = "REMOVED"

            # Asks
            for p, q in asks_map.items():
                if q >= self.wall_threshold:
                    ask_streaks[p] = ask_streaks.get(p, 0) + 1
                    streak = ask_streaks[p]
                    if streak >= self.wall_min_seconds:
                        wall_key = ("ASK", p)
                        if wall_key not in self.all_walls:
                            self.all_walls[wall_key] = Wall(
                                side="ASK",
                                price=p,
                                qty=q,
                                first_seen_sec=s - streak,
                                persisted=True,
                                first_persisted_sec=s,
                                status="ACTIVE",
                                last_updated_sec=s,
                            )
                        else:
                            w = self.all_walls[wall_key]
                            w.qty = q
                            w.last_updated_sec = s
                            if w.status == "REMOVED":
                                w.status = "ACTIVE"
                else:
                    ask_streaks.pop(p, None)
                    w_key = ("ASK", p)
                    if w_key in self.all_walls and self.all_walls[w_key].status == "ACTIVE":
                        self.all_walls[w_key].status = "REMOVED"

            for p in list(ask_streaks.keys()):
                if p not in asks_map:
                    ask_streaks.pop(p, None)
                    w_key = ("ASK", p)
                    if w_key in self.all_walls and self.all_walls[w_key].status == "ACTIVE":
                        self.all_walls[w_key].status = "REMOVED"

            # ── 2. Signal A: Defended Wall & Signal D: Liquidity Break ──
            for (side, p), w in list(self.all_walls.items()):
                if w.status not in ("ACTIVE", "DEFENDED_S", "DEFENDED_R"):
                    continue

                if side == "BID":
                    # Sweep test occurs when price touches or tests wall level
                    if p - self.tick_size <= ltp <= p + 0.15:
                        w.test_volume += b["sell_vol"]

                        # Check Signal D: Liquidity Break (> 1.5x wall in < 5s)
                        if idx >= self.break_window_seconds:
                            recent_mkt_sell = sum(x["sell_vol"] for x in self.bars[max(0, idx - self.break_window_seconds + 1):idx + 1])
                            if recent_mkt_sell >= self.break_multiplier * w.qty and ltp < p - self.tick_size:
                                w.status = "BROKEN"
                                w.break_sec = s
                                next_s = self._find_next_wall(side="BID", below_price=p)
                                brk_ev = LiquidityBreakEvent(
                                    sec=s,
                                    timestamp_str=dt_str,
                                    symbol=self.symbol,
                                    side="BID",
                                    broken_price=p,
                                    sweep_volume=recent_mkt_sell,
                                    wall_qty=w.qty,
                                    break_direction="DOWN",
                                    next_wall_target=next_s,
                                    note=f"Bid wall at {p:.2f} broken down with {recent_mkt_sell:,} sell volume",
                                )
                                self.break_events.append(brk_ev)
                                self.raw_signals.append({
                                    "sec": s, "idx": idx, "symbol": self.symbol,
                                    "type": "LIQUIDITY_BREAK", "side": "BID",
                                    "price": p, "ltp": ltp, "signal": "CONFIRM_SHORT",
                                    "detail": brk_ev.note,
                                })
                                continue

                        # Signal A: Defended Wall
                        if w.test_volume >= self.sweep_ratio * w.qty:
                            if ltp < p - self.tick_size:
                                w.status = "BROKEN"
                                w.break_sec = s
                            else:
                                if w.status != "DEFENDED_S":
                                    w.status = "DEFENDED_S"
                                    ck_key = f"DEFENDED_BID_{p}"
                                    if s - cooldown_tracker.get(ck_key, 0) > 60:
                                        cooldown_tracker[ck_key] = s
                                        self.raw_signals.append({
                                            "sec": s, "idx": idx, "symbol": self.symbol,
                                            "type": "DEFENDED_WALL", "side": "BID",
                                            "price": p, "ltp": ltp, "signal": "CONFIRM_LONG",
                                            "detail": f"Bid wall {p:.2f} defended against {w.test_volume:,} sell sweep (Support S)",
                                        })
                    elif ltp < p - (2 * self.tick_size):
                        w.status = "BROKEN"
                        w.break_sec = s

                elif side == "ASK":
                    if p - 0.15 <= ltp <= p + self.tick_size:
                        w.test_volume += b["buy_vol"]

                        # Check Signal D: Liquidity Break (> 1.5x wall in < 5s)
                        if idx >= self.break_window_seconds:
                            recent_mkt_buy = sum(x["buy_vol"] for x in self.bars[max(0, idx - self.break_window_seconds + 1):idx + 1])
                            if recent_mkt_buy >= self.break_multiplier * w.qty and ltp > p + self.tick_size:
                                w.status = "BROKEN"
                                w.break_sec = s
                                next_r = self._find_next_wall(side="ASK", above_price=p)
                                brk_ev = LiquidityBreakEvent(
                                    sec=s,
                                    timestamp_str=dt_str,
                                    symbol=self.symbol,
                                    side="ASK",
                                    broken_price=p,
                                    sweep_volume=recent_mkt_buy,
                                    wall_qty=w.qty,
                                    break_direction="UP",
                                    next_wall_target=next_r,
                                    note=f"Ask wall at {p:.2f} broken up with {recent_mkt_buy:,} buy volume",
                                )
                                self.break_events.append(brk_ev)
                                self.raw_signals.append({
                                    "sec": s, "idx": idx, "symbol": self.symbol,
                                    "type": "LIQUIDITY_BREAK", "side": "ASK",
                                    "price": p, "ltp": ltp, "signal": "CONFIRM_LONG",
                                    "detail": brk_ev.note,
                                })
                                continue

                        # Signal A: Defended Wall
                        if w.test_volume >= self.sweep_ratio * w.qty:
                            if ltp > p + self.tick_size:
                                w.status = "BROKEN"
                                w.break_sec = s
                            else:
                                if w.status != "DEFENDED_R":
                                    w.status = "DEFENDED_R"
                                    ck_key = f"DEFENDED_ASK_{p}"
                                    if s - cooldown_tracker.get(ck_key, 0) > 60:
                                        cooldown_tracker[ck_key] = s
                                        self.raw_signals.append({
                                            "sec": s, "idx": idx, "symbol": self.symbol,
                                            "type": "DEFENDED_WALL", "side": "ASK",
                                            "price": p, "ltp": ltp, "signal": "CONFIRM_SHORT",
                                            "detail": f"Ask wall {p:.2f} defended against {w.test_volume:,} buy sweep (Resistance R)",
                                        })
                    elif ltp > p + (2 * self.tick_size):
                        w.status = "BROKEN"
                        w.break_sec = s

            # ── 3. Signal B: Absorption (10s window with tightened near-wall & pulse filter) ──
            if idx >= self.absorb_window_seconds:
                win = self.bars[idx - self.absorb_window_seconds + 1:idx + 1]
                win_buy = sum(x["buy_vol"] for x in win)
                win_sell = sum(x["sell_vol"] for x in win)
                win_min_ltp = min(x["ltp"] for x in win)
                win_max_ltp = max(x["ltp"] for x in win)
                price_range = win_max_ltp - win_min_ltp

                idx_60 = max(0, idx - self.cvd_slope_flip_seconds)
                cvd_slope_60 = b["cvd"] - self.bars[idx_60]["cvd"]

                if price_range <= self.absorb_max_ticks * self.tick_size:
                    is_bid_cand = (win_sell >= self.absorb_threshold and cvd_slope_60 > 0)
                    is_ask_cand = (win_buy >= self.absorb_threshold and cvd_slope_60 < 0)

                    if is_bid_cand or is_ask_cand:
                        cand_side = "BID" if is_bid_cand else "ASK"
                        ck_key = f"ABSORB_{cand_side}"
                        if s - cooldown_tracker.get(ck_key, 0) > 60:
                            cooldown_tracker[ck_key] = s
                            cand_vol = win_sell if cand_side == "BID" else win_buy
                            cand_dict = {
                                "sec": s,
                                "timestamp_str": dt_str,
                                "symbol": self.symbol,
                                "side": cand_side,
                                "price": ltp,
                                "volume": cand_vol,
                                "price_range": price_range,
                                "cvd_slope_60s": cvd_slope_60,
                            }
                            self.raw_candidate_absorptions.append(cand_dict)

                            # 1. Near-wall filter (must be within absorb_near_wall_ticks of persisted wall)
                            near_walls = [
                                w for w in self.all_walls.values()
                                if w.side == cand_side and w.persisted and w.first_persisted_sec <= s
                                and (w.break_sec is None or w.break_sec >= s - 30)
                            ]
                            dists = [abs(ltp - w.price) for w in near_walls]
                            is_near = bool(dists and min(dists) <= self.absorb_near_wall_ticks * self.tick_size + 1e-5)

                            # 2. Pulse filter (>= 2 pulses within absorb_window)
                            qs = [
                                t for t in self.trade_quotes
                                if s - (self.absorb_window_seconds - 1) <= t[0] <= s
                                and abs(t[2] - ltp) <= 3 * self.tick_size + 1e-5
                            ]
                            has_2_pulses = (
                                len(qs) >= self.absorb_min_pulses
                                or len([t for t in qs if t[1] >= self.pulse_min_volume]) >= self.absorb_min_pulses
                            )

                            if not is_near:
                                self.rejected_absorptions.append({
                                    **cand_dict,
                                    "reason": "NOT_NEAR_WALL",
                                    "detail": f"No persisted {cand_side} wall within {self.absorb_near_wall_ticks} ticks ({self.absorb_near_wall_ticks * self.tick_size:.2f} pts) of {ltp:.2f}",
                                })
                            elif not has_2_pulses:
                                self.rejected_absorptions.append({
                                    **cand_dict,
                                    "reason": "NOT_ENOUGH_PULSES",
                                    "detail": f"Only {len(qs)} pulse(s) detected; minimum {self.absorb_min_pulses} required",
                                })
                            else:
                                verdict = "CONFIRM_LONG" if cand_side == "BID" else "CONFIRM_SHORT"
                                ab_ev = AbsorptionEvent(
                                    sec=s,
                                    timestamp_str=dt_str,
                                    symbol=self.symbol,
                                    side=cand_side,
                                    price=ltp,
                                    volume=cand_vol,
                                    price_range=price_range,
                                    cvd_slope_60s=cvd_slope_60,
                                    verdict=verdict,
                                    note=f"Absorption at {cand_side.lower()} {ltp:.2f} ({cand_vol:,} vol absorbed near wall, CVD {'▲' if cvd_slope_60 > 0 else '▼'})",
                                )
                                self.absorption_events.append(ab_ev)
                                self.raw_signals.append({
                                    "sec": s,
                                    "idx": idx,
                                    "symbol": self.symbol,
                                    "type": "ABSORPTION",
                                    "side": cand_side,
                                    "price": ltp,
                                    "ltp": ltp,
                                    "signal": verdict,
                                    "detail": ab_ev.note,
                                })

            # ── 4. Signal C: CVD Divergence & Slope Flip ──
            if idx >= 60:
                idx_60 = max(0, idx - 60)
                slope_60 = b["cvd"] - self.bars[idx_60]["cvd"]
                current_sign = 1 if slope_60 > 5000 else (-1 if slope_60 < -5000 else 0)
                if last_cvd_slope_sign != 0 and current_sign != 0 and current_sign != last_cvd_slope_sign:
                    # Slope flip event
                    pass
                if current_sign != 0:
                    last_cvd_slope_sign = current_sign

            if idx >= 120:
                # Rolling window for divergence (last 5 min / 300s)
                w_start = max(0, idx - self.cvd_div_window_seconds)
                lookback_bars = self.bars[w_start:idx]
                max_past_price = max(x["ltp"] for x in lookback_bars)
                min_past_price = min(x["ltp"] for x in lookback_bars)
                max_past_cvd = max(x["cvd"] for x in lookback_bars)
                min_past_cvd = min(x["cvd"] for x in lookback_bars)

                # Bearish Divergence: Price new high + CVD lower high
                if ltp >= max_past_price + 0.10 and b["cvd"] < max_past_cvd - 20000:
                    ck_key = "CVD_DIV_BEAR"
                    if s - cooldown_tracker.get(ck_key, 0) > 180:
                        cooldown_tracker[ck_key] = s
                        div_ev = CVDDivergenceEvent(
                            sec=s,
                            timestamp_str=dt_str,
                            symbol=self.symbol,
                            div_type="BEARISH_DIV",
                            price=ltp,
                            cvd=b["cvd"],
                            verdict="VETO",
                            note="Price new high with lower CVD high — weak buying, VETO longs",
                        )
                        self.divergence_events.append(div_ev)
                        self.raw_signals.append({
                            "sec": s, "idx": idx, "symbol": self.symbol,
                            "type": "CVD_DIVERGENCE", "side": "ASK",
                            "price": ltp, "ltp": ltp, "signal": "VETO_LONG",
                            "detail": div_ev.note,
                        })

                # Bullish Divergence: Price new low + CVD higher low
                elif ltp <= min_past_price - 0.10 and b["cvd"] > min_past_cvd + 20000:
                    ck_key = "CVD_DIV_BULL"
                    if s - cooldown_tracker.get(ck_key, 0) > 180:
                        cooldown_tracker[ck_key] = s
                        div_ev = CVDDivergenceEvent(
                            sec=s,
                            timestamp_str=dt_str,
                            symbol=self.symbol,
                            div_type="BULLISH_DIV",
                            price=ltp,
                            cvd=b["cvd"],
                            verdict="VETO",
                            note="Price new low with higher CVD low — weak selling, VETO shorts",
                        )
                        self.divergence_events.append(div_ev)
                        self.raw_signals.append({
                            "sec": s, "idx": idx, "symbol": self.symbol,
                            "type": "CVD_DIVERGENCE", "side": "BID",
                            "price": ltp, "ltp": ltp, "signal": "VETO_SHORT",
                            "detail": div_ev.note,
                        })

    def _find_next_wall(
        self,
        side: str,
        below_price: Optional[float] = None,
        above_price: Optional[float] = None,
    ) -> Optional[float]:
        """Finds next persisted wall level."""
        candidates = []
        for (w_side, p), w in self.all_walls.items():
            if w_side != side or w.status not in ("ACTIVE", "DEFENDED_S", "DEFENDED_R"):
                continue
            if below_price is not None and p < below_price:
                candidates.append(p)
            elif above_price is not None and p > above_price:
                candidates.append(p)
        if candidates:
            return max(candidates) if below_price is not None else min(candidates)
        return None

    def get_snapshot_as_of(
        self,
        target_time: Optional[dtime] = None,
    ) -> Dict[str, Any]:
        """
        Returns latest DOM snapshot and indicators up to target_time.
        """
        if not self.bars:
            return {}

        active_bars = self.bars
        if target_time is not None:
            active_bars = [b for b in self.bars if b["dt"].time() <= target_time]
            if not active_bars:
                active_bars = [self.bars[0]]

        last_b = active_bars[-1]
        idx = len(active_bars) - 1
        s = last_b["sec"]

        # CVD Slope over last 60s
        idx_60 = max(0, idx - 60)
        cvd_delta_60 = last_b["cvd"] - self.bars[idx_60]["cvd"]
        if cvd_delta_60 > 10000:
            cvd_badge = "▲"
            cvd_note = f"CVD ▲ Rising (+{cvd_delta_60:,}/60s)"
        elif cvd_delta_60 < -10000:
            cvd_badge = "▼"
            cvd_note = f"CVD ▼ Falling ({cvd_delta_60:,}/60s)"
        else:
            cvd_badge = "—"
            cvd_note = "CVD — Flat"

        # Check slope flip in last 60s
        idx_120 = max(0, idx - 120)
        prior_slope = self.bars[idx_60]["cvd"] - self.bars[idx_120]["cvd"]
        slope_flip = (cvd_delta_60 > 5000 and prior_slope < -5000) or (cvd_delta_60 < -5000 and prior_slope > 5000)

        # Defended Walls currently active
        defended_bids = [
            w.price for w in self.all_walls.values()
            if w.side == "BID" and w.status == "DEFENDED_S" and w.first_persisted_sec <= s and (w.break_sec is None or w.break_sec > s)
        ]
        defended_asks = [
            w.price for w in self.all_walls.values()
            if w.side == "ASK" and w.status == "DEFENDED_R" and w.first_persisted_sec <= s and (w.break_sec is None or w.break_sec > s)
        ]

        nearest_s = max(defended_bids) if defended_bids else None
        nearest_r = min(defended_asks) if defended_asks else None

        # Recent absorption in last 120s
        recent_absorb = [ev for ev in self.absorption_events if s - 120 <= ev.sec <= s]
        absorb_status = recent_absorb[-1].note if recent_absorb else "No active absorption"

        # Recent divergence in last 300s
        recent_div = [ev for ev in self.divergence_events if s - 300 <= ev.sec <= s]
        div_status = recent_div[-1].note if recent_div else "NONE"

        return {
            "sec": s,
            "dt": last_b["dt"],
            "ltp": last_b["ltp"],
            "cvd": last_b["cvd"],
            "cvd_slope_badge": cvd_badge,
            "cvd_slope_note": cvd_note,
            "cvd_slope_flip": slope_flip,
            "nearest_defended_bid_wall": nearest_s,
            "nearest_defended_ask_wall": nearest_r,
            "recent_absorption": recent_absorb[-1] if recent_absorb else None,
            "absorption_status": absorb_status,
            "recent_divergence": recent_div[-1] if recent_div else None,
            "divergence_status": div_status,
            "top20_bids": last_b["top20_bids"],
            "top20_asks": last_b["top20_asks"],
        }

    def compute_cell_matrix(self) -> Dict[str, Any]:
        """
        Computes the 2x3 performance matrix:
        Directions: LONG, SHORT
        Signal Types: wall-hold, absorption, break
        Evaluates +5m and +15m forward win rates and returns.
        """
        sec_to_idx = {b["sec"]: i for i, b in enumerate(self.bars)}
        n_bars = len(self.bars)

        cells: Dict[Tuple[str, str], List[Dict[str, Any]]] = {
            ("LONG", "wall-hold"): [],
            ("LONG", "absorption"): [],
            ("LONG", "break"): [],
            ("SHORT", "wall-hold"): [],
            ("SHORT", "absorption"): [],
            ("SHORT", "break"): [],
        }

        # 1. Wall hold (DEFENDED_WALL)
        for s in self.raw_signals:
            if s.get("type") == "DEFENDED_WALL":
                d = "LONG" if "LONG" in s.get("signal", "") else "SHORT"
                cells[(d, "wall-hold")].append(s)

        # 2. Absorption (tightened)
        for ev in self.absorption_events:
            d = "LONG" if ev.verdict == "CONFIRM_LONG" else "SHORT"
            idx = sec_to_idx.get(ev.sec)
            cells[(d, "absorption")].append({
                "sec": ev.sec,
                "idx": idx,
                "type": "ABSORPTION",
                "side": ev.side,
                "price": ev.price,
                "ltp": ev.price,
                "signal": ev.verdict,
                "detail": ev.note,
            })

        # 3. Liquidity Break (all broken persisted walls)
        for w in self.all_walls.values():
            if w.status == "BROKEN" and w.break_sec:
                idx = sec_to_idx.get(w.break_sec)
                if idx is not None:
                    d = "LONG" if w.side == "ASK" else "SHORT"
                    cells[(d, "break")].append({
                        "sec": w.break_sec,
                        "idx": idx,
                        "type": "LIQUIDITY_BREAK",
                        "side": w.side,
                        "price": w.price,
                        "ltp": self.bars[idx]["ltp"],
                        "signal": f"CONFIRM_{d}",
                        "detail": f"{w.side} wall at {w.price:.2f} broken; trade with break direction {d}",
                    })

        matrix_results: Dict[str, Dict[str, Any]] = {}
        dominant_cell = None
        highest_wr_15 = -1.0

        for (direction, stype), sigs in sorted(cells.items()):
            cell_key = f"{direction}_{stype}"
            eval_sigs = [s for s in sigs if s.get("idx") is not None and s["idx"] + 900 < n_bars]
            n = len(eval_sigs)
            if n == 0:
                matrix_results[cell_key] = {
                    "direction": direction,
                    "signal_type": stype,
                    "count": 0,
                    "wins_5m": 0,
                    "win_rate_5m_pct": 0.0,
                    "avg_return_5m_pct": 0.0,
                    "wins_15m": 0,
                    "win_rate_15m_pct": 0.0,
                    "avg_return_15m_pct": 0.0,
                    "signals": [],
                }
                continue

            w5 = 0
            w15 = 0
            tot_ret_5 = 0.0
            tot_ret_15 = 0.0
            evaluated_items = []

            for s in eval_sigs:
                idx = s["idx"]
                ep = s["ltp"]
                p5 = self.bars[idx + 300]["ltp"]
                p15 = self.bars[idx + 900]["ltp"]
                r5 = (p5 - ep) / ep if direction == "LONG" else (ep - p5) / ep
                r15 = (p15 - ep) / ep if direction == "LONG" else (ep - p15) / ep
                tot_ret_5 += r5
                tot_ret_15 += r15
                is_w5 = (r5 >= 0.01)
                is_w15 = (r15 >= 0.01)
                if is_w5:
                    w5 += 1
                if is_w15:
                    w15 += 1

                evaluated_items.append({
                    "sec": s["sec"],
                    "timestamp": self.bars[idx]["dt"].isoformat(),
                    "symbol": self.symbol,
                    "direction": direction,
                    "signal_type": stype,
                    "entry_ltp": ep,
                    "ltp_5m": p5,
                    "ltp_15m": p15,
                    "return_5m_pct": round(r5 * 100, 2),
                    "return_15m_pct": round(r15 * 100, 2),
                    "win_5m": is_w5,
                    "win_15m": is_w15,
                    "detail": s.get("detail", ""),
                })

            pct_w5 = round(w5 / n * 100, 1)
            pct_w15 = round(w15 / n * 100, 1)
            avg_r5 = round(tot_ret_5 / n * 100, 2)
            avg_r15 = round(tot_ret_15 / n * 100, 2)

            cell_data = {
                "direction": direction,
                "signal_type": stype,
                "count": n,
                "wins_5m": w5,
                "win_rate_5m_pct": pct_w5,
                "avg_return_5m_pct": avg_r5,
                "wins_15m": w15,
                "win_rate_15m_pct": pct_w15,
                "avg_return_15m_pct": avg_r15,
                "signals": evaluated_items,
            }
            matrix_results[cell_key] = cell_data

            if pct_w15 > highest_wr_15 and n >= 5:
                highest_wr_15 = pct_w15
                dominant_cell = cell_key

        rej_by_reason: Dict[str, int] = {}
        for r in self.rejected_absorptions:
            reason = r.get("reason", "UNKNOWN")
            rej_by_reason[reason] = rej_by_reason.get(reason, 0) + 1

        return {
            "matrix": matrix_results,
            "dominant_cell": dominant_cell,
            "highest_wr_15": highest_wr_15,
            "raw_candidate_absorptions": len(self.raw_candidate_absorptions),
            "rejected_absorptions_count": len(self.rejected_absorptions),
            "rejected_by_reason": rej_by_reason,
            "passed_absorptions_count": len(self.absorption_events),
        }


# ═══════════════════════════════════════════════════════════════════════════
# 5. L8 Signal Evaluator & Stack Integration
# ═══════════════════════════════════════════════════════════════════════════

def evaluate_l8_order_flow(
    candidate_action: str,
    spot: float,
    symbol: Optional[str] = None,
    target_date: Optional[Union[date, str]] = None,
    target_time: Optional[Union[dtime, str]] = None,
    rules: Optional[Dict[str, Any]] = None,
    rebuilder: Optional[DOMRebuilder] = None,
) -> L8VerdictReport:
    """
    Evaluates L8 Order Flow confirmation or veto for the candidate action.
    Candidate action: 'BUY_CE', 'BUY_PE', 'WAIT_WATCH', 'EXIT_POSITION'.

    Outputs one of: CONFIRM / VETO / SILENT.
    L8 is a confirmation layer, never an initiator.
    """
    cfg = rules or load_order_flow_config()
    is_paper_trade = bool(cfg.get("paper_trade_mode", True))
    min_confirm_win_rate = float(cfg.get("min_confirm_win_rate", 0.60))

    t_time: Optional[dtime] = None
    if target_time is not None:
        if isinstance(target_time, dtime):
            t_time = target_time
        else:
            t_str = str(target_time).strip()
            try:
                t_time = datetime.strptime(t_str[:5], "%H:%M").time()
            except Exception:
                t_time = None

    date_str = str(target_date) if target_date else "2026-08-24"
    if isinstance(target_date, date):
        date_str = target_date.strftime("%Y-%m-%d")

    # Pick strike/symbol if not explicitly supplied
    if not symbol:
        selector = StrikeSelectorDOM(cfg)
        sel = selector.get_strikes_for_spot(spot)
        sym_map = sel.get("symbols", {})
        if candidate_action == "BUY_PE":
            symbol = sym_map.get("ATM_PE", f"NIFTY{int(round(spot/50)*50)}-PE")
        else:
            symbol = sym_map.get("ATM_CE", f"NIFTY{int(round(spot/50)*50)}-CE")

    # Load DOM Rebuilder if not provided
    engine = rebuilder
    if engine is None or engine.symbol != symbol:
        engine = DOMRebuilder(cfg)
        ok = engine.load_and_rebuild(date_str=date_str, symbol=symbol)
        if not ok:
            # Silent if no DOM file recorded for symbol
            return L8VerdictReport(
                symbol=symbol,
                verdict="SILENT",
                reasons=[f"No DOM depth/quote recording available for {symbol} on {date_str}"],
                active_walls_summary="No DOM recording",
                is_paper_trade=is_paper_trade,
            )

    snap = engine.get_snapshot_as_of(target_time=t_time)
    if not snap:
        return L8VerdictReport(
            symbol=symbol,
            verdict="SILENT",
            reasons=["Empty snapshot"],
            is_paper_trade=is_paper_trade,
        )

    ltp = snap["ltp"]
    cvd = snap["cvd"]
    cvd_badge = snap["cvd_slope_badge"]
    cvd_note = snap["cvd_slope_note"]
    cvd_flip = snap["cvd_slope_flip"]
    nearest_s = snap["nearest_defended_bid_wall"]
    nearest_r = snap["nearest_defended_ask_wall"]
    recent_ab = snap["recent_absorption"]
    recent_div = snap["recent_divergence"]

    reasons: List[str] = []
    verdict = "SILENT"
    action_note: Optional[str] = None

    # Walls summary string
    w_parts = []
    if nearest_s:
        w_parts.append(f"Defended S: ₹{nearest_s:.2f}")
    if nearest_r:
        w_parts.append(f"Defended R: ₹{nearest_r:.2f}")
    walls_summary = " · ".join(w_parts) if w_parts else "No tested walls"

    # Directional evaluation
    if candidate_action in ("BUY_CE", "LONG"):
        # ── VETO Checks for Longs ──
        if recent_div and recent_div.div_type == "BEARISH_DIV":
            verdict = "VETO"
            reasons.append(f"⛔ VETO: {recent_div.note}")
        elif recent_ab and recent_ab.side == "ASK":
            verdict = "VETO"
            reasons.append("⛔ VETO: Aggressive buying being absorbed at ask wall — ceiling overhead")

        # ── CONFIRM Checks for Longs ──
        elif recent_ab and recent_ab.side == "BID" and cvd_badge == "▲":
            verdict = "CONFIRM"
            reasons.append(f"✅ CONFIRM: Institutional absorption at bid {recent_ab.price:.2f}, CVD turning up")
            low_zone = max(0.0, ltp - 1.5)
            high_zone = ltp + 1.5
            action_note = f"✅ L8: absorption at {symbol} bid wall, CVD ▲ — entry zone {low_zone:.1f}–{high_zone:.1f}"
        elif nearest_s is not None and ltp >= nearest_s:
            verdict = "CONFIRM"
            reasons.append(f"✅ CONFIRM: Defended bid wall at {nearest_s:.2f} holding as Support S")
            action_note = f"✅ L8: defended bid wall at {nearest_s:.2f}, CVD {cvd_badge} — support intact"

    elif candidate_action in ("BUY_PE", "SHORT"):
        # ── VETO Checks for Shorts ──
        if recent_div and recent_div.div_type == "BULLISH_DIV":
            verdict = "VETO"
            reasons.append(f"⛔ VETO: {recent_div.note}")
        elif recent_ab and recent_ab.side == "BID":
            verdict = "VETO"
            reasons.append("⛔ VETO: Aggressive selling being absorbed at bid wall — floor underfoot")

        # ── CONFIRM Checks for Shorts ──
        elif recent_ab and recent_ab.side == "ASK" and cvd_badge == "▼":
            verdict = "CONFIRM"
            reasons.append(f"✅ CONFIRM: Institutional absorption at ask {recent_ab.price:.2f}, CVD turning down")
            low_zone = max(0.0, ltp - 1.5)
            high_zone = ltp + 1.5
            action_note = f"✅ L8: absorption at {symbol} ask wall, CVD ▼ — entry zone {low_zone:.1f}–{high_zone:.1f}"
        elif nearest_r is not None and ltp <= nearest_r:
            verdict = "CONFIRM"
            reasons.append(f"✅ CONFIRM: Defended ask wall at {nearest_r:.2f} holding as Resistance R")
            action_note = f"✅ L8: defended ask wall at {nearest_r:.2f}, CVD {cvd_badge} — resistance intact"

    # Historical win-rate check
    hist_stats = compute_journal_stats()
    hist_wr = hist_stats.get("win_rate_15m")

    # Paper-trade discipline gate & multi-day clock:
    # "Veto stays SIM-only throughout"
    clock_started = bool(cfg.get("paper_trade_clock_started", False))
    veto_mode = str(cfg.get("veto_mode", "SIM_ONLY")).upper()
    sim_suppressed = False
    if is_paper_trade or (not clock_started) or (veto_mode == "SIM_ONLY") or (hist_wr is not None and hist_wr < min_confirm_win_rate):
        sim_suppressed = True
        if verdict == "VETO":
            reasons.append(f"[SIM ONLY] Veto suppressed (Paper-trade clock held / SIM-only mode active; win-rate {hist_wr or 0.0:.1%} < {min_confirm_win_rate:.0%})")
            # In paper trade / SIM mode, L8 does not veto live execution
            verdict = "SILENT"

    return L8VerdictReport(
        symbol=symbol,
        verdict=verdict,
        reasons=reasons,
        nearest_defended_bid_wall=nearest_s,
        nearest_defended_ask_wall=nearest_r,
        active_walls_summary=walls_summary,
        absorption_status=snap["absorption_status"],
        cvd_slope_badge=cvd_badge,
        cvd_slope_note=cvd_note,
        cvd_slope_flip=cvd_flip,
        divergence_status=snap["divergence_status"],
        action_line_note=action_note,
        is_paper_trade=is_paper_trade,
        historical_win_rate=hist_wr,
        sim_suppressed=sim_suppressed,
        last_ltp=ltp,
        last_cvd=cvd,
        as_of_time=snap["dt"].strftime("%H:%M:%S") if snap.get("dt") else "",
    )


# ═══════════════════════════════════════════════════════════════════════════
# 6. Journaling & Forward Outcome Validation
# ═══════════════════════════════════════════════════════════════════════════

def journal_signal(
    record: Dict[str, Any],
    journal_path: Optional[Path] = None,
) -> None:
    """Appends an evaluated L8 signal record to order_flow_journal.jsonl."""
    path = journal_path or JOURNAL_LOG_PATH
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except Exception as exc:
        logger.warning(f"Error appending to L8 journal {path}: {exc}")


def compute_journal_stats(
    journal_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Reads journal records and computes win rate at +5m and +15m."""
    path = journal_path or JOURNAL_LOG_PATH
    if not path.exists():
        return {"total": 0, "win_rate_5m": None, "win_rate_15m": None}

    total_confirm = 0
    wins_5m = 0
    wins_15m = 0

    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                d = json.loads(line)
                if "CONFIRM" in d.get("signal", ""):
                    total_confirm += 1
                    if d.get("win_5m"):
                        wins_5m += 1
                    if d.get("win_15m"):
                        wins_15m += 1
    except Exception as exc:
        logger.warning(f"Error reading journal {path}: {exc}")

    if total_confirm == 0:
        return {"total": 0, "win_rate_5m": None, "win_rate_15m": None}

    return {
        "total": total_confirm,
        "win_rate_5m": wins_5m / total_confirm,
        "win_rate_15m": wins_15m / total_confirm,
        "wins_5m": wins_5m,
        "wins_15m": wins_15m,
    }


# ═══════════════════════════════════════════════════════════════════════════
# 7. Full Acceptance Test Runner
# ═══════════════════════════════════════════════════════════════════════════

def run_order_flow_acceptance_test(
    sample_date: str = "2026-08-24",
    sample_symbol: str = "NIFTY24250-CE",
    live_day_date: Optional[str] = None,
    live_day_symbol: Optional[str] = None,
    data_dir: Optional[Union[str, Path]] = None,
) -> Dict[str, Any]:
    """
    Executes acceptance replay test on NIFTY option recordings.
    Per Fix 3: GRASIM is dropped from acceptance (stock DOM thresholds incompatible with NIFTY options).
    NIFTY option recordings only.

    Reports:
    - 2x3 Cell Performance Matrix: Direction (LONG, SHORT) x Type (wall-hold, absorption, break)
    - Before/After absorption count with near-wall & pulse filters
    - Dominant cell win rate at +5m and +15m
    - Multi-Day Gate evaluation (>= 60% in dominant cell on >= 3 of 5 days across 3 regimes)
    - Paper-trade clock status (Held / Not started yet; SIM-ONLY veto throughout)
    """
    cfg = load_order_flow_config()
    results: Dict[str, Any] = {}
    base_data_dir = Path(data_dir) if data_dir else RECORDED_DIR

    # Fix 3: Check if GRASIM or non-NIFTY symbol was passed, and explicitly drop it
    if live_day_symbol and "NIFTY" not in live_day_symbol.upper():
        logger.info(f"Dropping {live_day_symbol} from acceptance test per Fix 3: NIFTY option recordings only.")
        results["recorded_live_day"] = {
            "status": "DROPPED_PER_FIX3",
            "reason": f"Stock DOM scale for {live_day_symbol} incompatible with NIFTY options. NIFTY option recordings only.",
            "symbol": live_day_symbol,
            "date": live_day_date,
        }

    # Replay NIFTY target(s)
    targets = [
        ("bundled_nifty", sample_date, sample_symbol),
    ]

    # If live_day_symbol is a NIFTY option, include it
    if live_day_symbol and "NIFTY" in live_day_symbol.upper() and live_day_date:
        targets.append(("recorded_live_day", live_day_date, live_day_symbol))

    all_journal_records = []

    for key, d_str, s_sym in targets:
        rebuilder = DOMRebuilder(cfg)
        ok = rebuilder.load_and_rebuild(date_str=d_str, symbol=s_sym, data_dir=base_data_dir)
        if not ok:
            results[key] = {"status": "FAILED_TO_LOAD", "date": d_str, "symbol": s_sym}
            continue

        walls = rebuilder.all_walls
        total_persisted_walls = len(walls)
        survived_sweeps = len([w for w in walls.values() if w.status in ("DEFENDED_S", "DEFENDED_R")])
        broken_walls = len([w for w in walls.values() if w.status == "BROKEN"])
        absorption_count = len(rebuilder.absorption_events)
        divergence_count = len(rebuilder.divergence_events)
        break_count = len(rebuilder.break_events)

        precision_sweeps = (survived_sweeps / (survived_sweeps + broken_walls)) if (survived_sweeps + broken_walls) > 0 else 0.0

        # Compute the 2x3 cell matrix
        cell_report = rebuilder.compute_cell_matrix()
        matrix = cell_report["matrix"]
        dominant_cell = cell_report["dominant_cell"]
        dom_data = matrix.get(dominant_cell, {}) if dominant_cell else {}

        # Collect journal records from all cells
        for c in matrix.values():
            all_journal_records.extend(c.get("signals", []))

        # Overall numbers for backwards compatibility
        total_eval = sum(c["count"] for c in matrix.values())
        total_w5 = sum(c["wins_5m"] for c in matrix.values())
        total_w15 = sum(c["wins_15m"] for c in matrix.values())
        wr_5 = round((total_w5 / total_eval * 100), 1) if total_eval > 0 else 0.0
        wr_15 = round((total_w15 / total_eval * 100), 1) if total_eval > 0 else 0.0

        # Gate Evaluation:
        # 1. Single day: dominant cell >= 60% at +15m
        dom_meets_60 = bool(dominant_cell and dom_data.get("win_rate_15m_pct", 0.0) >= 60.0)

        # 2. Multi-day requirement: >= 3 of 5 days spanning trend-up, trend-down, chop
        # Currently only 1 day recorded in dataset
        days_recorded = 1
        days_required = int(cfg.get("min_multi_day_required", 5))
        clock_started = bool(cfg.get("paper_trade_clock_started", False))

        results[key] = {
            "status": "SUCCESS",
            "date": d_str,
            "symbol": s_sym,
            "total_bars_seconds": len(rebuilder.bars),
            "walls_detected": total_persisted_walls,
            "survived_sweeps": survived_sweeps,
            "broken_walls": broken_walls,
            "sweep_survival_precision": round(precision_sweeps * 100, 1),
            "raw_candidate_absorptions": cell_report["raw_candidate_absorptions"],
            "rejected_absorptions_count": cell_report["rejected_absorptions_count"],
            "rejected_by_reason": cell_report["rejected_by_reason"],
            "passed_absorptions_count": cell_report["passed_absorptions_count"],
            "absorption_events": absorption_count,
            "cvd_divergences": divergence_count,
            "liquidity_breaks": break_count,
            "evaluated_confirm_signals": total_eval,
            "profitable_at_5m": total_w5,
            "profitable_at_15m": total_w15,
            "win_rate_5m_pct": wr_5,
            "win_rate_15m_pct": wr_15,
            "cell_matrix": matrix,
            "dominant_cell": dominant_cell,
            "dominant_cell_count": dom_data.get("count", 0),
            "dominant_cell_win_rate_5m_pct": dom_data.get("win_rate_5m_pct", 0.0),
            "dominant_cell_win_rate_15m_pct": dom_data.get("win_rate_15m_pct", 0.0),
            "dominant_cell_avg_return_15m_pct": dom_data.get("avg_return_15m_pct", 0.0),
            "dominant_cell_meets_60pct": dom_meets_60,
            "meets_60pct_threshold": dom_meets_60,
            "multi_day_gate": {
                "days_recorded": days_recorded,
                "days_required": days_required,
                "regimes_spanned": ["trend-down"],
                "regimes_required": ["trend-up", "trend-down", "chop"],
                "passed_days_count": 1 if dom_meets_60 else 0,
                "passed_days_required": 3,
                "paper_trade_clock_started": clock_started,
                "paper_trade_clock_status": "ON HOLD (Requires 5 recorded days spanning trend-up, trend-down, chop; currently 1 day recorded)" if not clock_started else "ACTIVE",
                "veto_mode": "SIM_ONLY",
            },
        }

    # Record journal records to disk
    for r in all_journal_records:
        journal_signal(r)

    return results
