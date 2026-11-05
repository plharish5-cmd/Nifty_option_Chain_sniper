"""
algo_execution_engine/strategy_1h_80ema.py — Re-export alias for strategy_1h_8ema.py.
Maintains backward compatibility with any consumers importing strategy_1h_80ema.
"""
from .strategy_1h_8ema import (
    Strategy1hSignal,
    Strategy1h8EmaEvaluator,
    Strategy1h80EmaEvaluator,
    build_1h_data,
    load_config,
)

__all__ = [
    "Strategy1hSignal",
    "Strategy1h8EmaEvaluator",
    "Strategy1h80EmaEvaluator",
    "build_1h_data",
    "load_config",
]
