# ledger.py
import csv
from config import TRADE_CSV, SYMBOLS, MIN_USD_EXPOS, TP_ATR_MULT, SL_ATR_MULT
from exchange_client import exchange, fetch_price, open_position_from_history
from indicators import atr
import logging
logger = logging.getLogger()   # root logger

def append_new_trades(last_id=None):
    """Append new trades to CSV and return latest trade id."""
    recent = exchange.fetch_my_trades(limit=50)
    if not recent:
        return last_id
    recent.sort(key=lambda t: t["id"])
    with TRADE_CSV.open("a", newline="") as f:
        writer = csv.writer(f)
        if f.tell() == 0:
            writer.writerow(["id","time","symbol","side","qty","price","cost","fee","order"])
        for t in recent:
            if last_id is not None and t["id"] <= last_id:
                continue
            writer.writerow([
                t["id"], t["datetime"], t["symbol"], t["side"],
                t["amount"], t["price"], t["cost"],
                t["fee"]["cost"], t["order"]
            ])
    return recent[-1]["id"]


def initialize_positions():
    """Adopt existing wallet positions via trade history."""
    positions = {}
    bal = exchange.fetch_balance()
    for sym in SYMBOLS:
        base  = sym.split("/")[0]
        held  = float(bal.get(base, {}).get("total", 0))
        usd_val = held * fetch_price(sym)
        if usd_val < MIN_USD_EXPOS:
            positions[sym] = None
            continue
        qty, entry = open_position_from_history(sym)
        if qty:
            a = atr(sym) or entry * 0.01
            positions[sym] = {
                "entry": entry,
                "amount": qty,
                "sl": entry - a * SL_ATR_MULT,
                "tp": entry + a * TP_ATR_MULT
            }
        else:
            positions[sym] = None
    return positions