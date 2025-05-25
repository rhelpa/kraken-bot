# main.py

from config import LOG_PATH, SYMBOLS, POLL_INTERVAL, RISK_FRAC, DIP_THRESHOLD, MIN_ORDER_USD
from logger_setup import setup_logger

# wire up logging *after* config is loaded
logger = setup_logger(log_path=LOG_PATH)
logger.info("🟢 Kraken bot starting…")

import time
import datetime
from exchange_client import fetch_price, account_cash, exchange, lot_step
from ledger import append_new_trades, initialize_positions
from strategy import filter_tradeable, generate_actions
from indicators import ema
from decimal import Decimal

positions = initialize_positions()
last_price: dict[str, Decimal] = {s: Decimal(str(fetch_price(s)))   # cast once
                                  for s in SYMBOLS}
last_trade_id = append_new_trades(None)

logger.info("▶ bot online – risk %.2f%%/trade", RISK_FRAC*100)


while True:
    try:
        # ── ONE-SHOT SNAPSHOTS ────────────────────────────────────────────
        prices = {sym: fetch_price(sym) for sym in SYMBOLS}               # ← NEW
        bal    = exchange.fetch_balance()
        cash   = account_cash()

        # qty of each base-asset currently in the wallet
        wallet_qty = {
            sym: Decimal(str(bal.get(sym.split("/")[0], {}).get("total", 0)))
            for sym in SYMBOLS
        }                                                                 # ← NEW

        # ── PORTFOLIO METRICS ────────────────────────────────────────────
        cost_basis = sum(                                                 # unchanged
            pos["amount"] * pos["avg_price"]
            for pos in positions.values() if pos
        )

        portfolio_value = sum((wallet_qty[sym] * prices[sym]               # ← NEW
                              for sym in SYMBOLS if wallet_qty[sym]), Decimal("0"))
        equity = cash + portfolio_value                                   # ← FIX (single calc)

        unreal = equity - (cash + cost_basis)                             # ← SIMPLER, same math
        # alternatively keep the per-symbol form:
        # unreal = sum(
        #     positions[s]["amount"] * (prices[s] - positions[s]["avg_price"])
        #     for s in SYMBOLS if positions[s]
        # )

        ticket = max(RISK_FRAC * equity, MIN_ORDER_USD)                        # unchanged
        open_n = sum(1 for p in positions.values() if p)

        # ── HEARTBEAT ────────────────────────────────────────────────────
        now = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        logger.info(
            "♥ %s UTC | Cash $%.2f | Equity $%.2f | Open %d | "
            "Ticket $%.2f | UnrealPnL $%.2f",
            now, cash, equity, open_n, ticket, unreal
        )
        logger.debug("UNR-P&L check ⇒ equity %.2f – cash %.2f – cost_basis %.2f = %.2f",
                     equity, cash, cost_basis, unreal)

        # ── DETAILED DIP LOG ─────────────────────────────────────────────
        for s in SYMBOLS:
            ref_price = ema(s)
            logger.info("%s current=%.2f, ref=%.2f, ratio=%.4f",
                        s, prices[s], ref_price, prices[s] / ref_price)

        # ── TRADING FILTER ───────────────────────────────────────────────
        tradeable = []
        for sym in SYMBOLS:
            price  = prices[sym]                                          # ← reuse cache
            mkt    = exchange.markets[sym]
            minlot = lot_step(sym)
            req    = minlot * price

            reasons = []
            if not mkt["active"] or price == 0:
                reasons.append("inactive")
            if cash < req:
                reasons.append("cash<min")
            EPS = 1e-9
            ratio = price / last_price[sym]
            if ratio > DIP_THRESHOLD + EPS:
                reasons.append("no-dip")

            ok = not reasons
            if ok:
                tradeable.append(sym)

            note = "" if ok else f" ({', '.join(reasons)})"
            logger.info("↗ %-10s $%.4f | minlot %.4f (~$%.2f) %s%s",
                        sym, price, minlot, req, "✅" if ok else "❌", note)

        logger.info("📊 Tradeable (%d): %s",
                    len(tradeable), ", ".join(tradeable) or "none")

        # ── HOUSEKEEPING ────────────────────────────────────────────────
        last_trade_id = append_new_trades(last_trade_id)
        last_price    = prices[s]                       # ← reuse cache
        time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        logger.warning("⏹ stopped – open positions: %s", {s:p for s,p in positions.items() if p})
        break
    except Exception:
        logger.exception("Main-loop error")
        time.sleep(POLL_INTERVAL)
