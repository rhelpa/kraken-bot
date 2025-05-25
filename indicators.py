# indicators.py
import math
from decimal import Decimal
from typing import List
from exchange_client import exchange, fetch_price
from config import EMA_PERIOD, ATR_PERIOD, RISK_FRAC
import logging
logger = logging.getLogger(__name__)



def ema(symbol: str, n: int = EMA_PERIOD) -> Decimal:
    """Calculate simple EMA over 1h closes."""
    candles = exchange.fetch_ohlcv(symbol, "1h", limit=n)
    closes  = [c[4] for c in candles]
    k = 2 / (n + 1)
    e = closes[0]
    for price in closes[1:]:
        e = price * k + e * (1 - k)
    return Decimal(e)


def atr(symbol: str, n: int = ATR_PERIOD) -> Decimal:
    """
    Average True Range on 1-minute candles.

    Returns:
        Decimal(ATR)  – 0 if fewer than 2 candles.
    """
    # fetch_ohlcv: [t, open, high, low, close, volume]
    ohlc: List[List[float | str]] = exchange.fetch_ohlcv(symbol, "1m", limit=n + 1)
    if len(ohlc) < 2:
        return Decimal("0")

    trs: list[Decimal] = []
    for prev, curr in zip(ohlc, ohlc[1:]):
        prev_close = Decimal(str(prev[4]))

        high = Decimal(str(curr[2]))
        low  = Decimal(str(curr[3]))

        tr = max(
            high - low,
            abs(high - prev_close),
            abs(low - prev_close),
        )
        trs.append(tr)

    return sum(trs) / Decimal(len(trs))

def pos_size(entry: float, stop: float, equity: float) -> Decimal:
    """Return quantity sizing given risk fraction."""
    risk = equity * RISK_FRAC
    unit = abs(entry - stop)
    return risk / unit if unit else 0

# Depth EMA filter
depth_ema = {}

def update_depth_ema(symbol: str, alpha: float = 0.2, levels: int = 5) -> Decimal:
    book = exchange.fetch_order_book(symbol)
    bid_depth = sum(entry[1] for entry in book["bids"][:levels])
    ask_depth = sum(entry[1] for entry in book["asks"][:levels])
    total     = bid_depth + ask_depth

    prev = depth_ema.get(symbol, total)
    depth_ema[symbol] = alpha * total + (1 - alpha) * prev

    # return the new EMA value, not dict.update()
    return depth_ema[symbol]

