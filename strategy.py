from __future__ import annotations

import math
import logging
from decimal import Decimal, ROUND_DOWN
from typing import Dict, List, Literal, NamedTuple, Tuple
from indicators import trend_4h_ema

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
    return (steps * minlot).quantize(minlot)


# ──────────────────────────────────────────────────────────────────────────
#  PUBLIC API
# ──────────────────────────────────────────────────────────────────────────
def filter_tradeable(
    symbols: List[str],
    positions: Dict[str, dict],
    last_price: Dict[str, Decimal],
    cash: Decimal,
) -> List[str]:
    """Return symbols that pass trend, dip, cash, and order-book depth filters."""
    tradeable: List[str] = []

    for sym in symbols:
        price = fetch_price(sym)
        if price is None:
            logger.debug("%s skipped – no ticker", sym)
            continue

        # compute minimum notional
        mkt = exchange.markets[sym]
        minlot = Decimal(mkt["limits"]["amount"]["min"] or "1e-8")
        min_notional = minlot * price

        reasons: List[str] = []

        # 1) trend filter: only buy if price is above its 50×4h-EMA
        trend_val = Decimal(str(trend_4h_ema(sym, period=50)))
        if price < trend_val:
            reasons.append("below-4h-EMA")

        # 2) ensure sufficient cash to meet min notional
        if cash < min_notional:
            reasons.append("cash<min")

        # 3) dip filter
        if price > last_price[sym] * Decimal(str(DIP_THRESHOLD)):
            reasons.append("no-dip")

        # 4) order-book depth filter
        if update_depth_ema(sym) < 50:
            reasons.append("thin-book")

        if reasons:
            logger.debug(
                "%s filtered out (%s)",
                sym,
                ", ".join(reasons)
            )
        else:
            logger.debug(
                "%s passed filters – price: %.2f, 4h-EMA: %.2f, last: %.2f, min_notional: %.2f",
                sym,
                price,
                trend_val,
                last_price[sym],
                min_notional,
            )
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
) -> Tuple[List[TradeAction], List[str]]:
    """
    Returns:
      - actions: List[TradeAction] to send
      - reasons: if actions==[], concrete rule-names why no trade was generated
    """
    actions: List[TradeAction] = []
    reasons: List[str] = []

    price = fetch_price(sym)
    if price is None:
        reasons.append("no-price")
        return actions, reasons

    pos = positions.get(sym)

    # ── ENTRY ────────────────────────────────────────────────────────────
    if pos is None:
        # 1) max-open guard
        if open_n >= MAX_OPEN:
            reasons.append("max-open-reached")
        else:
            # 2) dip vs last cycle
            dip_ok = price <= last_price[sym] * Decimal(str(DIP_THRESHOLD))
            if not dip_ok:
                reasons.append("no-dip")
            # 3) under EMA
            ema_val = ema(sym)
            if ema_val <= 0:
                reasons.append("no-ema")
            elif price >= ema_val:
                reasons.append("above-ema")

            # only size if both dip_ok and ema_val>0
            if dip_ok and ema_val > 0 and price < ema_val:
                # 4) compute SL/TP
                vol = atr(sym)
                sl = price - vol * Decimal(str(SL_ATR_MULT))
                tp = price + vol * Decimal(str(TP_ATR_MULT))

                # 5) sizing
                raw_qty = pos_size(price, sl, equity)
                max_qty = cash / price
                qty = Decimal(min(raw_qty, max_qty))

                # 6) round to minlot
                minlot = Decimal(exchange.markets[sym]["limits"]["amount"]["min"] or "1e-8")
                qty = _round_qty(qty, minlot)

                if qty >= minlot:
                    actions.append(
                        TradeAction("buy", sym, qty, price, tag="entry", tp=tp, sl=sl)
                    )
                    logger.info(
                        "%s ENTRY placed @ %.2f | qty=%.6f | tp=%.2f | sl=%.2f",
                        sym, price, qty, tp, sl
                    )
                else:
                    reasons.append("qty<minlot")

    # ── EXIT ─────────────────────────────────────────────────────────────
    elif pos:
        logger.debug("%s evaluating exit – price: %.2f | TP: %.2f | SL: %.2f",
                     sym, price, pos.get("tp"), pos.get("sl"))

        # 1) compute a safe “entry price” fallback
        entry_price = (
            pos.get("entry")
            or pos.get("avg_price")
            or pos.get("blended_price")
            or price
        )
        prev_peak = peak_cache.get(sym, entry_price)

        # 2) lift trailing stop relative to the highest seen
        peak = max(prev_peak, price)
        peak_cache[sym] = peak
        new_sl = peak * (1 - Decimal(str(TRAIL_PCT)) / 100)
        if new_sl > pos["sl"]:
            pos["sl"] = new_sl
            logger.debug("%s trail-stop lifted to %.2f", sym, new_sl)

        # 3) check TP / SL
        if price >= pos["tp"]:
            actions.append(TradeAction("sell", sym, pos["amount"], price, tag="TP"))
            logger.info("%s TAKE-PROFIT hit @ %.2f", sym, price)
        elif price <= pos["sl"]:
            actions.append(TradeAction("sell", sym, pos["amount"], price, tag="SL"))
            logger.info("%s STOP-LOSS hit @ %.2f", sym, price)
        else:
            reasons.append("no-exit-signal")

    return actions, reasons