# main.py

from config import LOG_PATH, SYMBOLS, POLL_INTERVAL, RISK_FRAC, DIP_THRESHOLD
from logger_setup import setup_logger

# wire up logging *after* config is loaded
logger = setup_logger(log_path=LOG_PATH)
logger.info("🟢 Kraken bot starting…")

import time
import datetime
from exchange_client import fetch_price, account_cash, exchange
from ledger import append_new_trades, initialize_positions
from strategy import filter_tradeable, generate_actions
from indicators import ema

positions = initialize_positions()
last_price = {s: fetch_price(s) for s in SYMBOLS}
last_trade_id = append_new_trades(None)

logger.info("▶ bot online – risk %.2f%%/trade", RISK_FRAC*100)


while True:
    try:
        cash = account_cash()
        bal  = exchange.fetch_balance()
        equity = cash + sum(
            bal.get(s.split("/")[0], {}).get("total", 0) * fetch_price(s)
            for s in SYMBOLS
        )
        ticket = min(RISK_FRAC * equity, cash)   # cap to free cash

        # here’s the fix: add the comprehension “for s in SYMBOLS”
        unreal = sum(
            (bal.get(s.split("/")[0], {}).get("total", 0) -
                (positions[s]["amount"] if positions[s] else 0)) *
            fetch_price(s)
            for s in SYMBOLS
        )

        open_n = sum(1 for p in positions.values() if p)

        # HEARTBEAT
        now = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        logger.info(
            "♥ %s UTC | Cash $%.2f | Equity $%.2f | Open %d | "
            "Ticket $%.2f | UnrealPnL $%.2f",
            now, cash, equity, open_n, ticket, unreal
        )

        # Detailed dip logging
        for s in SYMBOLS:
            price     = fetch_price(s)
            ref_price = ema(s)
            logger.info(f"{s} current={price:.2f}, ref={ref_price:.2f}, ratio={price/ref_price:.4f}")


        # TRADING FILTER
        tradeable = []
        for sym in SYMBOLS:
            price  = fetch_price(sym)
            mkt    = exchange.markets[sym]
            minlot = mkt["limits"]["amount"]["min"] or 0
            req    = minlot * price

            reasons = []
            if not mkt["active"] or price == 0:
                reasons.append("inactive")
            if cash < req:
                reasons.append("cash<min")
            if price > last_price[sym] * DIP_THRESHOLD:
                reasons.append("no-dip")

            ok = not reasons
            if ok:
                tradeable.append(sym)

            note = "" if ok else f" ({', '.join(reasons)})"
            logger.info("↗ %-10s $%.4f | minlot %.4f (~$%.2f) %s%s",
                        sym, price, minlot, req, "✅" if ok else "❌", note)

        logger.info("📊 Tradeable (%d): %s",
                    len(tradeable), ", ".join(tradeable) or "none")

        last_trade_id = append_new_trades(last_trade_id)
        last_price = {s: fetch_price(s) for s in SYMBOLS}
        time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        logger.warning("⏹ stopped – open positions: %s", {s:p for s,p in positions.items() if p})
        break
    except Exception:
        logger.exception("Main-loop error")
        time.sleep(POLL_INTERVAL)
