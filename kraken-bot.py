# main.py

from config import LOG_PATH, SYMBOLS, POLL_INTERVAL, RISK_FRAC, DIP_THRESHOLD, MIN_ORDER_USD
from logger_setup import setup_logger

# wire up logging *after* config is loaded
logger = setup_logger(log_path=LOG_PATH)
logger.info("🟢 Kraken bot starting…")

import time
import datetime
from typing import Dict, List
from exchange_client import fetch_price, account_cash, exchange, lot_step
from ledger import append_new_trades, initialize_positions
from strategy import TradeAction, filter_tradeable, generate_actions
from indicators import ema
from decimal import Decimal

positions   = initialize_positions()

# Build peak_cache with a fallback from "entry" → "avg_price"
peak_cache: Dict[str, Decimal] = {}
for sym, pos in positions.items():
    if pos is None:
        continue

    # pick whichever one exists
    entry_price = pos.get("entry") or pos.get("avg_price") or pos.get("blended_price")
    if entry_price is None:
        # safety: skip if we truly have no idea what we paid
        logger.warning("Skipping peak_cache for %s: no entry price in %r", sym, pos)
        continue

    peak_cache[sym] = entry_price
    
last_price: dict[str, Decimal] = {s: Decimal(str(fetch_price(s)))   # cast once
                                  for s in SYMBOLS}
last_trade_id = append_new_trades(None)

logger.info("▶ bot online – risk %.2f%%/trade", RISK_FRAC*100)


while True:
    try:
        # ── ONE-SHOT SNAPSHOTS ────────────────────────────────────────────
        price_snapshot = {
            sym: Decimal(fetch_price(sym))         # keys like "SOL/USD"
            for sym in SYMBOLS
        }      
        bal    = exchange.fetch_balance()
        cash   = account_cash()

        # qty of each base-asset currently in the wallet
        wallet_qty = {
            sym: Decimal(str(bal.get(sym.split("/")[0], {}).get("total", 0)))
            for sym in SYMBOLS
        }                                                                 

        # ── PORTFOLIO METRICS ────────────────────────────────────────────
        cost_basis = sum(                                                 
            pos["amount"] * pos["avg_price"]
            for pos in positions.values() if pos
        )

        portfolio_value = sum(
            wallet_qty.get(sym, Decimal(0)) * price_snapshot[sym]
            for sym in SYMBOLS
        )
        equity = cash + portfolio_value                                   

        unreal = equity - (cash + cost_basis)                             
        # alternatively keep the per-symbol form:
        # unreal = sum(
        #     positions[s]["amount"] * (prices[s] - positions[s]["avg_price"])
        #     for s in SYMBOLS if positions[s]
        # )

        ticket = max(RISK_FRAC * equity, MIN_ORDER_USD)                        
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
                        s, price_snapshot[s], ref_price, price_snapshot[s] / ref_price)

        # ── TRADING FILTER ───────────────────────────────────────────────
        tradeable = []
        for sym in SYMBOLS:
            price     = price_snapshot[sym]
            ref_price = ema(sym)               # ← recalc EMA here
            mkt       = exchange.markets[sym]
            minlot    = lot_step(sym)
            req    = minlot * price

            reasons = []
            if not mkt["active"] or price == 0:
                reasons.append("inactive")
            if cash < req:
                reasons.append("cash<min")

            EPS = 1e-9
            if ref_price <= 0:
                reasons.append("no-ref")      # avoid div-by-zero
                ratio = float("nan")
            else:
                ratio = price / ref_price
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
        
        all_actions: List[TradeAction] = []

        # Note: open_n should be the count of current positions (len of non-None in positions)
        # peak_cache is a dict you maintain across loops to track each symbol's high since entry
        for sym in tradeable:
            acts = generate_actions(
                sym=sym,
                positions=positions,
                last_price=last_price,
                open_n=sum(1 for p in positions.values() if p),
                cash=cash,
                equity=equity,
                peak_cache=peak_cache,
            )
            all_actions.extend(acts)

        # Now actually send them to the exchange
        for act in all_actions:
            if act.side == "buy":
                order = exchange.create_order(
                    symbol=act.symbol,
                    type="market",
                    side="buy",
                    amount=float(act.amount)   # or Decimal, depending on your client
                )
                logger.info("🟢 BUY %s @ %.4f × %.4f → %r",
                            act.symbol, act.price, act.amount, order)
            elif act.side == "sell":
                order = exchange.create_order(
                    symbol=act.symbol,
                    type="market",
                    side="sell",
                    amount=float(act.amount)
                )
                logger.info("🔴 SELL %s @ %.4f × %.4f → %r",
                            act.symbol, act.price, act.amount, order)



        # ── HOUSEKEEPING ─────────────────────────────────────────────────────────
        # record any fills in your CSV / DB
        last_trade_id = append_new_trades(last_trade_id)

        # update your in-memory book so generate_actions sees the new positions next loop
        for act in all_actions:
            if act.side == "buy":
                positions[act.symbol] = {
                    "amount": act.amount,
                    "entry": act.price,
                    "sl": act.sl,
                    "tp": act.tp,
                }
                peak_cache[act.symbol] = act.price
            elif act.side == "sell":
                positions.pop(act.symbol, None)
                peak_cache.pop(act.symbol, None)

    except KeyboardInterrupt:
        logger.warning("⏹ stopped – open positions: %s", {s:p for s,p in positions.items() if p})
        break
    except Exception:
        logger.exception("Main-loop error")
        time.sleep(POLL_INTERVAL)
