import math
from datetime import datetime
from decimal import Decimal
from typing import List
from exchange_client import exchange, fetch_price, map_sym
from config import EMA_PERIOD, ATR_PERIOD, RISK_FRAC, MODE
import logging

logger = logging.getLogger(__name__)

# a simple cache so you don't hammer the API every tick
_trend_cache: dict[str, float] = {}


class SimExchange:
    # ...
    def fetch_ohlcv(self, symbol, timeframe="1m", limit=100, **_):
        # assume self.df is bar-indexed; slice the last `limit` rows
        bars = self.df.iloc[max(0, self.i - limit): self.i + 1][symbol]
        return [
            [None, o, h, l, c, None]          # timestamp, open, high, low, close, volume
            for o, h, l, c in zip(bars.open, bars.high, bars.low, bars.close)
        ]


def trend_4h_ema(symbol: str, period: int = 50, limit: int = 100) -> float:
    # only refresh every N minutes or on cache-miss
    #ohlcv = exchange.fetch_ohlcv(symbol, timeframe="4h", limit=limit)
    sym  = map_sym(symbol) 
    ohlc = exchange.fetch_ohlcv(sym, "1m", limit=n + 1)
    closes = [row[4] for row in ohlcv]
    ema_values = ema(closes, period)
    _trend_cache[symbol] = ema_values[-1]
    return ema_values[-1]

def ema(symbol: str, n: int = EMA_PERIOD) -> Decimal:
    """Calculate simple EMA over 1h closes with logging."""

    if MODE == "SIM":
        symbol = map_sym(symbol)
        
    #logger.info(f"Fetching {n} 1h candles for EMA calculation of {symbol}")
    candles = exchange.fetch_ohlcv(symbol, "1h", limit=n)

    closes = [c[4] for c in candles]
    k = 2 / (n + 1)
    e = closes[0]
    #logger.info(f"{symbol} EMA initial value (first close): {e}")
    for i, price in enumerate(closes[1:], start=1):
        e = price * k + e * (1 - k)
        logger.debug(f"{symbol} EMA step {i}: price={price}, ema={e}")
    ema_value = Decimal(e)
    #logger.info(f"Computed EMA({symbol}, {n}): {ema_value}")
    return ema_value


def atr(symbol: str, n: int = ATR_PERIOD) -> Decimal:
    if MODE == "SIM":
        return None   
    """
    Average True Range on 1-minute candles with logging.

    Returns:
        Decimal(ATR)  – 0 if fewer than 2 candles.
    """
    logger.info(f"Fetching {n + 1} 1m candles for ATR calculation of {symbol}")
    ohlc: List[List[float | str]] = exchange.fetch_ohlcv(symbol, "1m", limit=n + 1)
    if len(ohlc) < 2:
        logger.warning(f"Not enough data to compute ATR({symbol}, {n}); returning 0")
        return Decimal("0")

    trs: List[Decimal] = []
    for idx, (prev, curr) in enumerate(zip(ohlc, ohlc[1:]), start=1):
        prev_close = Decimal(str(prev[4]))
        high = Decimal(str(curr[2]))
        low = Decimal(str(curr[3]))
        tr = max(
            high - low,
            abs(high - prev_close),
            abs(low - prev_close),
        )
        trs.append(tr)
        logger.debug(f"{symbol} True Range step {idx}: {tr}")

    atr_value = sum(trs) / Decimal(len(trs))
    logger.info(f"Computed ATR({symbol}, {n}): {atr_value}")
    return atr_value


def pos_size(entry: float, stop: float, equity: float) -> Decimal:
    """Return quantity sizing given risk fraction, with logging."""
    logger.info(f"Calculating position size: entry={entry}, stop={stop}, equity={equity}")
    risk = equity * RISK_FRAC
    unit = abs(entry - stop)
    size = (Decimal(risk) / Decimal(unit)) if unit else Decimal(0)
    logger.info(f"Risk amount: {risk}, price unit: {unit}, position size: {size}")
    return size

# Depth EMA filter
_depth_ema: dict[str, Decimal] = {}

def update_depth_ema(symbol: str, alpha: float = 0.2, levels: int = 5) -> Decimal:
    """Update and return EMA of order book depth at top levels, with logging."""
    logger.info(f"Fetching order book for {symbol} (top {levels} levels)")
    book = exchange.fetch_order_book(symbol)
    bid_depth = sum(entry[1] for entry in book.get("bids", [])[:levels])
    ask_depth = sum(entry[1] for entry in book.get("asks", [])[:levels])
    total = bid_depth + ask_depth

    prev = _depth_ema.get(symbol, total)
    new = alpha * total + (1 - alpha) * prev
    _depth_ema[symbol] = new

    logger.info(f"Depth EMA updated for {symbol}: previous={prev}, total={total}, new EMA={new}")
    return new
