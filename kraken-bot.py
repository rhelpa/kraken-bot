# main.py
import time
import datetime
from config import SYMBOLS, POLL_INTERVAL, RISK_FRAC
from logger_setup import setup_logger
from exchange_client import fetch_price, account_cash, exchange
from ledger import append_new_trades, initialize_positions
from strategy import filter_tradeable, generate_actions
import logging
logger = logging.getLogger()   # root logger

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
        open_n = sum(1 for p in positions.values() if p)

        logger.info(
            "♥ %s UTC | Cash $%.2f | Equity $%.2f | Open %d",
            datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
            cash, equity, open_n
        )

        tradeable = filter_tradeable(SYMBOLS, positions, last_price, cash)
        for sym in tradeable:
            actions = generate_actions(sym, positions, last_price, open_n, cash, equity)
            for act in actions:
                if act[0] == "buy":
                    _, s, qty, price, tp, sl = act
                    order = exchange.create_limit_buy_order(
                        s, qty, float(exchange.price_to_precision(s, price)),
                        {"postOnly": True}
                    )
                    positions[s] = {"entry": price, "amount": qty, "sl": sl, "tp": tp}
                    cash -= qty * price
                    open_n += 1
                    logger.info("BUY ▶ %s %.6f @ %.4f (order %s)", s, qty, price, order["id"])
                elif act[0] == "sell":
                    _, s, qty, price, tag = act
                    from execution import safe_limit_sell
                    if safe_limit_sell(s, qty):
                        positions[s] = None
                        open_n -= 1
                        logger.info("SELL ◀ %s %.6f @ %.4f (%s)", s, qty, price, tag)

        last_trade_id = append_new_trades(last_trade_id)
        last_price = {s: fetch_price(s) for s in SYMBOLS}
        time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        logger.warning("⏹ stopped – open positions: %s", {s:p for s,p in positions.items() if p})
        break
    except Exception:
        logger.exception("Main-loop error")
        time.sleep(POLL_INTERVAL)