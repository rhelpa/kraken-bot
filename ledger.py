# ledger.py

import csv
from decimal import Decimal
from config import TRADE_CSV, SYMBOLS, MIN_USD_EXPOS, TP_ATR_MULT, SL_ATR_MULT
from exchange_client import exchange, fetch_price, open_position_from_history
from indicators import atr
import logging
logger = logging.getLogger(__name__)


DEC_TOL = Decimal("1e-8")

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


def initialize_positions() -> dict[str, dict | None]:
    """
    Build `positions` using wallet balances *and* trade history,
    skipping dust, zero-price markets, and tiny exposures.
    """
    positions: dict[str, dict | None] = {}
    bal        = exchange.fetch_balance()

    for sym in SYMBOLS:
        base       = sym.split("/")[0]
        wallet_qty = Decimal(str(bal.get(base, {}).get("total", 0)))

        spot = fetch_price(sym) or 0
        if spot is None:          # NEW
            continue              # unsupported symbol → just ignore
        if spot == 0:
            logger.warning("%s: price=0 – market ignored", sym)
            positions[sym] = None
            continue

        usd_value = wallet_qty * spot
        if usd_value < MIN_USD_EXPOS:    # dust filter
            positions[sym] = None
            continue

        # --- History reconstruction ----------------------------------------
        hist_qty, hist_entry = open_position_from_history(sym)
        hist_qty     = hist_qty or 0
        hist_entry   = hist_entry or 0

        # --- Reconcile wallet vs history -----------------------------------
        qty, blended_price = reconcile_wallet(
            sym,
            wallet_qty,
            hist_qty,
            hist_entry,
            spot
        )
        avg_price = blended_price if blended_price is not None else hist_entry

        # --- Protective levels ---------------------------------------------
        a  = atr(sym) or avg_price * Decimal("0.01")     # fallback: 1 % of price
        sl = avg_price - a * SL_ATR_MULT
        tp = avg_price + a * TP_ATR_MULT

        positions[sym] = {
            "amount": qty,
            "avg_price": avg_price,
            "sl": sl,
            "tp": tp
        }

    return positions

def _to_dec(x) -> Decimal:
    """Cast floats/ints/str to Decimal exactly once at the boundary."""
    return x if isinstance(x, Decimal) else Decimal(str(x))


def reconcile_wallet(
    sym: str,
    wallet_qty,                 # Decimal | float
    hist_qty,                   # Decimal | float
    hist_entry,                 # Decimal | float   – average cost from FIFO
    spot,                       # Decimal | float   – latest market price
    *,
    tol: Decimal | float = DEC_TOL
) -> tuple[Decimal, Decimal]:
    """
    Harmonise Kraken wallet balances with trade-history quantities.

    Returns:
        (qty_to_use, avg_price_to_use) – both as Decimal.
    """
    # ---------- cast *once* ------------------------------------------------
    wallet_qty = _to_dec(wallet_qty)
    hist_qty   = _to_dec(hist_qty)
    hist_entry = _to_dec(hist_entry)
    spot       = _to_dec(spot)
    tol        = _to_dec(tol)

    # 1️⃣ Quantities already match → keep historical blended price
    if abs(wallet_qty - hist_qty) <= tol:
        return wallet_qty, hist_entry

    delta_qty = wallet_qty - hist_qty

    # 2️⃣ Spot is zero → can’t blend safely
    if spot == 0:
        logger.warning(
            "%s: price=0; ignoring stray %s units",
            sym, delta_qty
        )
        return hist_qty, hist_entry

    # 3️⃣ Normal blend at spot for the stray quantity
    logger.warning(
        "%s: wallet %s ≠ history %s (stray %s) – blending at %s",
        sym, wallet_qty, hist_qty, delta_qty, spot
    )
    cost_basis = hist_qty * hist_entry + delta_qty * spot
    avg_price  = cost_basis / wallet_qty

    return wallet_qty, avg_price
