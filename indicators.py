# indicators.py
import math
from exchange_client import exchange, fetch_price
from config import EMA_PERIOD, ATR_PERIOD, RISK_FRAC
import logging
logger = logging.getLogger()   # root logger


def ema(symbol: str, n: int = EMA_PERIOD) -> float:
    """Calculate simple EMA over 1h closes."""
    candles = exchange.fetch_ohlcv(symbol, "1h", limit=n)
    closes  = [c[4] for c in candles]
    k = 2 / (n + 1)
    e = closes[0]
    for price in closes[1:]:
        e = price * k + e * (1 - k)
    return e


def atr(symbol: str, n: int = ATR_PERIOD) -> float:
    """Compute ATR over 1m bars."""
    ohlc = exchange.fetch_ohlcv(symbol, "1m", limit=n+1)
    trs = []
    for prev, curr in zip(ohlc, ohlc[1:]):
        _, _, high, low, _, _ = curr
        prev_close = prev[4]
        trs.append(max(high - low,
                      abs(high - prev_close),
                      abs(low - prev_close)))
    return sum(trs) / len(trs) if trs else 0


def pos_size(entry: float, stop: float, equity: float) -> float:
    """Return quantity sizing given risk fraction."""
    risk = equity * RISK_FRAC
    unit = abs(entry - stop)
    return risk / unit if unit else 0

# Depth EMA filter
depth_ema = {}

def update_depth_ema(symbol: str, alpha: float = 0.2, levels: int = 5) -> float:
    """Smooth book depth for top levels."""
    book = exchange.fetch_order_book(symbol)
    bid = sum(vol for _, vol in book["bids"][:levels])
    ask = sum(vol for _, vol in book["asks"][:levels])
    total = bid + ask
    prev = depth_ema.get(symbol, total)
    depth_ema[symbol] = alpha * total + (1 - alpha) * prev
    return depth_ema[symbol]
