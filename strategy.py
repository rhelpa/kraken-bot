# strategy.py
from __future__ import annotations

import math
import logging
from decimal import Decimal, ROUND_DOWN
from typing import Dict, List, Literal, NamedTuple

from config import (
    DIP_THRESHOLD,
    MAX_OPEN,
    TRAIL_PCT,
    SL_ATR_MULT,
    TP_ATR_MULT,
)
from exchange_client import fetch_price, exchange
from indicators import ema, atr, pos_size, update_depth_ema

logger = logging.getLogger(__name__)

Side = Literal["buy", "sell"]


class TradeAction(NamedTuple):
    side: Side
    symbol: str
    qty: Decimal
    price: Decimal
    tag: str  # "TP", "SL", or entry
    tp: Decimal | None = None
    sl: Decimal | None = None


def _round_qty(qty: Decimal, minlot: Decimal) -> Decimal:
    """Round *down* to the nearest valid lot size."""
    steps = (qty / minlot).quantize(0, ROUND_DOWN)
    return (steps * minlot).quantize(minlot)  # preserve exchange precision


# ──────────────────────────────────────────────────────────────────────────
#  PUBLIC API
# ──────────────────────────────────────────────────────────────────────────
def filter_tradeable(
    symbols: List[str],
    positions: Dict[str, dict],
    last_price: Dict[str, Decimal],
    cash: Decimal,
) -> List[str]:
    """Return symbols that pass dip, cash, order-book depth filters."""
    tradeable: List[str] = []

    for sym in symbols:
        price = fetch_price(sym)
        if price is None:
            logger.debug("%s skipped – no ticker", sym)
            continue

        mkt = exchange.markets[sym]
        minlot = Decimal(mkt["limits"]["amount"]["min"] or "1e-8")
        min_notional = minlot * price

        reasons: list[str] = []
        if cash < min_notional:
            reasons.append("cash<min")
        if price > last_price[sym] * Decimal(str(DIP_THRESHOLD)):
            reasons.append("no-dip")
        if update_depth_ema(sym) < 50:
            reasons.append("thin-book")

        if reasons:
            logger.debug("%s filtered out (%s)", sym, ", ".join(reasons))
        else:
            tradeable.append(sym)

    return tradeable


def generate_actions(
    sym: str,
    positions: Dict[str, dict],
    last_price: Dict[str, Decimal],
    open_n: int,
    cash: Decimal,
    equity: Decimal,
    peak_cache: Dict[str, Decimal],
) -> List[TradeAction]:
    """Return zero or more TradeActions for *sym*."""
    actions: List[TradeAction] = []
    price = fetch_price(sym)
    if price is None:
        return actions

    pos = positions.get(sym)

    # ── ENTRY ────────────────────────────────────────────────────────────
    if pos is None and open_n < MAX_OPEN:
        if (
            price <= last_price[sym] * Decimal(str(DIP_THRESHOLD))
            # optional EMA trend filter – **usually want price < EMA**
            and price < ema(sym)
        ):
            a = atr(sym)
            sl = price - a * Decimal(str(SL_ATR_MULT))
            tp = price + a * Decimal(str(TP_ATR_MULT))

            raw_qty = pos_size(price, sl, equity)
            max_qty = cash / price
            qty = Decimal(min(raw_qty, max_qty))

            minlot = Decimal(exchange.markets[sym]["limits"]["amount"]["min"] or "1e-8")
            qty = _round_qty(qty, minlot)

            if qty >= minlot:
                actions.append(
                    TradeAction("buy", sym, qty, price, tag="entry", tp=tp, sl=sl)
                )
            else:
                logger.debug("%s qty %.8f < minlot", sym, qty)

    # ── EXIT ─────────────────────────────────────────────────────────────
    elif pos:
        peak = peak_cache.get(sym, pos["entry"])
        peak = max(peak, price)
        peak_cache[sym] = peak  # update in-place

        # ratchet stop only if new high
        if peak != pos["entry"]:
            new_sl = peak * (1 - Decimal(str(TRAIL_PCT)) / 100)
            if new_sl > pos["sl"]:
                pos["sl"] = new_sl
                logger.debug("%s trail-stop lifted to %.2f", sym, new_sl)

        if price >= pos["tp"]:
            actions.append(TradeAction("sell", sym, pos["amount"], price, tag="TP"))
        elif price <= pos["sl"]:
            actions.append(TradeAction("sell", sym, pos["amount"], price, tag="SL"))

    return actions
