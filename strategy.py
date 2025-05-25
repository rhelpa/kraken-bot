# strategy.py
import math
from config import DIP_THRESHOLD, MAX_OPEN, TRAIL_PCT
from exchange_client import fetch_price, exchange
from indicators import ema, atr, pos_size, update_depth_ema
import logging
logger = logging.getLogger(__name__)

def filter_tradeable(symbols, positions, last_price, cash):
    """Return list of symbols meeting dip, cash, volume & depth criteria."""
    tradeable = []
    for sym in symbols:
        price = fetch_price(sym)
        if not price:
            continue
        mkt    = exchange.markets[sym]
        minlot = mkt["limits"]["amount"]["min"] or 0
        req    = minlot * price
        reasons = []
        if cash < req:
            reasons.append("cash<min")
        if price > last_price[sym] * DIP_THRESHOLD:
            reasons.append("no-dip")
        if update_depth_ema(sym) < 50:
            reasons.append("thin-book")
        if not reasons:
            tradeable.append(sym)
    return tradeable


def generate_actions(sym, positions, last_price, open_n, cash, equity):
    """Decide buy/sell actions for a given symbol."""
    actions = []
    price = fetch_price(sym)
    pos = positions.get(sym)

    # ENTRY
    if pos is None and open_n < MAX_OPEN \
       and price <= last_price[sym] * DIP_THRESHOLD \
       and price > ema(sym):
        a   = atr(sym)
        sl  = price - a * SL_ATR_MULT
        tp  = price + a * TP_ATR_MULT
        raw_qty = pos_size(price, sl, equity)
        max_qty = cash / price
        qty = min(raw_qty, max_qty)
        minlot = exchange.markets[sym]["limits"]["amount"]["min"] or 1e-8
        qty = math.floor(qty / minlot) * minlot
        if qty >= minlot:
            actions.append(("buy", sym, qty, price, tp, sl))

    # EXIT
    elif pos:
        # trailing stop
        if price > pos["entry"]:
            pos["sl"] = max(pos["sl"], price * (1 - TRAIL_PCT/100))
        # take profit or stop loss
        if price >= pos["tp"]:
            actions.append(("sell", sym, pos["amount"], price, "TP"))
        elif price <= pos["sl"]:
            actions.append(("sell", sym, pos["amount"], price, "SL"))

    return actions
