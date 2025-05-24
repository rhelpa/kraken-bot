#!/usr/bin/env python3
"""
kraken-bot.py
─────────────────────────────────────────────────────────────────────────────
• Dip‑buy strategy with ATR‑adaptive TP/SL, 1 % trailing stop & EMA trend filter
• Automatically **adopts pre‑existing wallet exposure** (weighted FIFO cost)
• Heart‑beat every cycle (cash, equity, open trades) + concise BUY/SELL banners
• Appends every new trade fill to /mnt/bot-log-share/kraken-trades.csv
• “Safe Sell” handler skips delisted/illiquid markets & checks wallet balance

Revision 2025‑05‑24
─────────────────────────────────────────────────────────────────────────────
✓ Ticket size is now *capped* to free USD (`min(risk×equity, cash)`).
✓ Quantity is rounded **down** to the nearest lot step ⇒ cost ≤ cash.
✓ Pre‑trade guard (`cost ≤ cash`) stops `EOrder:Insufficient funds`.
✓ Skip‑reason logger shows why a pair is ❌ (e.g. `cash<min`, `no‑dip`).
"""

import os
import time
import csv
import pathlib
import logging
import datetime
import math
from dotenv import load_dotenv
import ccxt
from logging.handlers import TimedRotatingFileHandler

# ─── Configuration ─────────────────────────────────────────────────────────
load_dotenv()
API_KEY, API_SECRET = os.getenv("API_KEY"), os.getenv("API_SECRET")

SYMBOLS = [
    "SOL/USD", "ETH/USD", "BTC/USD", "XRP/USD",
    "DOGE/USD", "TIA/USD", "FARTCOIN/USD", "GHIBLI/USD",
    "BAL/USD", "LOFI/USD", "ZEC/USD", "ELX/USD", "BODEN/USD"
]

DIP_THRESHOLD  = 0.95   # deeper pullbacks only
EMA_PERIOD     = 20     # 20‑hour EMA
ATR_PERIOD     = 14     # 14 × 1‑min bars
TP_ATR_MULT    = 3.0    # profit target: 3× ATR
SL_ATR_MULT    = 1.5    # stop loss: 1.5× ATR
TRAIL_PCT      = 1.0    # 1 % trailing stop
RISK_FRAC      = 0.02   # 2 % of equity per entry
MAX_OPEN       = 2
POLL_INTERVAL  = 60     # seconds
MIN_USD_EXPOS  = 10     # adopt only positions ≥ $10
MIN_24H_VOL    = 50_000 # minimum $50 k of daily volume
MIN_BOOK_UNITS = 50     # min base‐asset units in top book levels

depth_ema = {}

# ─── Paths (NAS share) ─────────────────────────────────────────────────────
MOUNT_DIR = pathlib.Path("/mnt/bot-log-share")
MOUNT_DIR.mkdir(parents=True, exist_ok=True)
TRADE_CSV = MOUNT_DIR / "kraken-trades.csv"
LOG_PATH  = MOUNT_DIR / "kraken-bot.log"

# ─── Logging ───────────────────────────────────────────────────────────────
logger = logging.getLogger()
logger.setLevel(logging.DEBUG)

fh = TimedRotatingFileHandler(
    LOG_PATH,
    when="midnight",      # rotate at 00:00 UTC
    interval=1,
    backupCount=7,
    encoding="utf-8"
)
fh.setFormatter(logging.Formatter(
    "%(asctime)s %(levelname)s: %(message)s", "%Y-%m-%d %H:%M:%S"
))
logger.addHandler(fh)

ch = logging.StreamHandler()
ch.setFormatter(logging.Formatter(
    "%(asctime)s %(levelname)s: %(message)s", "%H:%M:%S"
))
ch.setLevel(logging.INFO)
logger.addHandler(ch)

# ─── Exchange connection ──────────────────────────────────────────────────
exchange = ccxt.kraken({
    "apiKey": API_KEY,
    "secret": API_SECRET,
    "enableRateLimit": True,
    "timeout": 60000,
})
exchange.load_markets()

# ─── Helper utilities ─────────────────────────────────────────────────────

def fetch_price(sym: str) -> float:
    try:
        return float(exchange.fetch_ticker(sym)["last"] or 0)
    except Exception as e:
        logger.error("Price fetch %s – %s", sym, e)
        return 0.0


def account_cash() -> float:
    return float(exchange.fetch_balance()["free"].get("USD", 0))


def ema(sym: str, n: int = EMA_PERIOD) -> float:
    candles = exchange.fetch_ohlcv(sym, "1h", limit=n)
    closes  = [c[4] for c in candles]
    k, e    = 2/(n+1), closes[0]
    for p in closes[1:]:
        e = p*k + e*(1-k)
    return e


def atr(sym: str, n: int = ATR_PERIOD) -> float:
    ohlc = exchange.fetch_ohlcv(sym, "1m", limit=n+1)
    trs  = [
        max(h-l, abs(h-prev_c), abs(l-prev_c))
        for (_,_,h,l,_,_), (_,_,_,_,prev_c,_) in zip(ohlc[1:], ohlc)
    ]
    return sum(trs)/len(trs) if trs else 0


def pos_size(entry: float, stop: float, equity: float) -> float:
    risk = equity * RISK_FRAC
    unit = abs(entry - stop)
    return 0 if unit == 0 else risk / unit

# ─── Safe maker‑sell (limit POST‑only) ─────────────────────────────────────

def safe_market_sell(sym: str, qty: float) -> bool:
    mkt = exchange.market(sym)
    if not mkt["active"]:
        logger.warning("Skip %s – inactive/delisted", sym)
        return False
    price = fetch_price(sym)
    if price == 0:
        logger.warning("Skip %s – price 0", sym)
        return False

    free_amt = exchange.fetch_balance().get(mkt["base"], {}).get("free", 0)
    qty = min(qty, free_amt)
    minlot = mkt["limits"]["amount"]["min"] or 0
    if qty < minlot:
        logger.warning("Skip sell %s – free %.8f < minlot %.8f", sym, free_amt, minlot)
        return False

    try:
        book = exchange.fetch_order_book(sym)
        bid = book['bids'][0][0]
        limit_price = float(exchange.price_to_precision(sym, bid))
        order = exchange.create_limit_sell_order(
            sym, qty, limit_price, { 'postOnly': True }
        )
        logger.info("✅ Sold %.8f %s (order %s)", qty, sym, order['id'])
        return True
    except ccxt.InsufficientFunds:
        logger.warning("Sell failed %s – insufficient funds", sym)
    except Exception as e:
        logger.error("Sell failed %s – %s", sym, e)
    return False

# ─── Trade history helpers ────────────────────────────────────────────────

def fetch_all_trades(sym: str, max_pages: int = 20):
    trades, ofs = [], 0
    for _ in range(max_pages):
        page = exchange.fetch_my_trades(sym, params={"ofs": ofs})
        if not page:
            break
        trades.extend(page)
        ofs += 50
        if len(page) < 50:
            break
    return sorted(trades, key=lambda t: t["timestamp"])


def open_position_from_history(sym: str):
    inventory = []
    for t in fetch_all_trades(sym):
        qty = t["amount"]
        if t["side"] == "buy":
            inventory.append([qty, t["price"]])
        else:
            q = qty
            while q > 0 and inventory:
                if inventory[0][0] > q:
                    inventory[0][0] -= q
                    q = 0
                else:
                    q -= inventory[0][0]
                    inventory.pop(0)
    qty_net = sum(q for q,_ in inventory)
    if qty_net == 0:
        return 0, 0
    cost = sum(q*p for q,p in inventory)
    return qty_net, cost/qty_net

# ─── Market‑making ping‑pong (optional) ───────────────────────────────────

def place_mm_orders(symbol: str, stake_usd: float, spread_pct: float = 0.002):
    book = exchange.fetch_order_book(symbol)
    mid = (book['bids'][0][0] + book['asks'][0][0]) / 2
    size = stake_usd / mid
    buy_price  = float(exchange.price_to_precision(symbol, mid*(1-spread_pct/2)))
    sell_price = float(exchange.price_to_precision(symbol, mid*(1+spread_pct/2)))

    exchange.create_limit_buy_order(symbol, size,  buy_price,  {'postOnly': True})
    exchange.create_limit_sell_order(symbol, size, sell_price, {'postOnly': True})

# ─── Depth EMA filter ─────────────────────────────────────────────────────

def update_depth_ema(symbol: str, alpha: float = 0.2, levels: int = 5) -> float:
    book = exchange.fetch_order_book(symbol)
    bid_depth = sum(vol for _, vol in book['bids'][:levels])
    ask_depth = sum(vol for _, vol in book['asks'][:levels])
    total    = bid_depth + ask_depth

    prev = depth_ema.get(symbol, total)
    depth_ema[symbol] = alpha*total + (1-alpha)*prev
    return depth_ema[symbol]

# ─── CSV ledger ───────────────────────────────────────────────────────────

def append_new_trades(last_id=None):
    recent = exchange.fetch_my_trades(limit=50)
    if not recent:
        return last_id
    recent.sort(key=lambda t: t["id"])
    with TRADE_CSV.open("a", newline="") as f:
        w = csv.writer(f)
        if f.tell() == 0:
            w.writerow(["id","time","symbol","side","qty","price","cost","fee","order"])
        for t in recent:
            if last_id is not None and t["id"] <= last_id:
                continue
            w.writerow([
                t["id"], t["datetime"], t["symbol"], t["side"],
                t["amount"], t["price"], t["cost"],
                t["fee"]["cost"], t["order"]
            ])
    return recent[-1]["id"]

# ─── Initial wallet adoption ──────────────────────────────────────────────
positions = {}
bal = exchange.fetch_balance()
for sym in SYMBOLS:
    base    = sym.split("/")[0]
    held    = float(bal.get(base, {}).get("total", 0))
    usd_val = held * fetch_price(sym)
    if usd_val >= MIN_USD_EXPOS:
        qty, entry = open_position_from_history(sym)
        if qty:
            a = atr(sym) or entry*0.01
            positions[sym] = {
                "entry": entry,
                "amount": qty,
                "sl": entry - a*SL_ATR_MULT,
                "tp": entry + a*TP_ATR_MULT
            }
            logger.info(
                "↻ Adopted %s %.6f @ %.4f (TP %.4f SL %.4f)",
                sym, qty, entry, entry + a*TP_ATR_MULT, entry - a*SL_ATR_MULT
            )
        else:
            positions[sym] = None
    else:
        positions[sym] = None

last_price    = {s: fetch_price(s) for s in SYMBOLS}
last_trade_id = append_new_trades(None)

# ─── Main loop ────────────────────────────────────────────────────────────
logger.info(
    "▶ bot online – risk %.2f%%/trade, max %d open",
    RISK_FRAC*100, MAX_OPEN
)

while True:
    try:
        cash   = account_cash()
        bal    = exchange.fetch_balance()
        equity = cash + sum(
            bal.get(s.split("/")[0], {}).get("total", 0) * fetch_price(s)
            for s in SYMBOLS
        )
        open_n = sum(1 for p in positions.values() if p)

        ticket = min(RISK_FRAC * equity, cash)
        unreal = sum(
            (bal.get(s.split("/")[0], {}).get("total", 0) -
             (positions[s]["amount"] if positions[s] else 0)) *
            fetch_price(s)
            for s in SYMBOLS if positions.get(s)
        )

        logger.info(
            "♥ %s UTC | Cash $%.2f | Equity $%.2f | Open %d | "
            "Ticket $%.2f | UnrealPnL $%.2f",
            datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
            cash, equity, open_n, ticket, unreal
        )

        # ─── Volume filter ───────────────────────────────────────────
        qualified = []
        for s in SYMBOLS:
            ticker = exchange.fetch_ticker(s)
            if ticker['quoteVolume'] * ticker['last'] >= MIN_24H_VOL:
                qualified.append(s)
            else:
                logger.info("Skip %s – low 24h vol", s)

        # ─── Scan & filter for tradeable ─────────────────────────────
        tradeable = []
        for s in qualified:
            price  = fetch_price(s)
            mkt    = exchange.markets[s]
            minlot = mkt["limits"]["amount"]["min"] or 0
            req    = minlot * price
            reasons = []
            if not mkt["active"] or price == 0:
                reasons.append("inactive")
            if cash < req:
                reasons.append("cash<min")
            if price > last_price[s] * DIP_THRESHOLD:
                reasons.append("no-dip")
            if update_depth_ema(s) < MIN_BOOK_UNITS:
                reasons.append("thin-book")

            status = "✅" if not reasons else "❌"
            note   = "" if not reasons else f" ({', '.join(reasons)})"
            logger.info(
                "↗ %-10s $%.4f | minlot %.4f (~$%.2f) %s%s",
                s, price, minlot, req, status, note
            )
            if not reasons:
                tradeable.append(s)

        logger.info(
            "📊 Tradeable (%d): %s",
            len(tradeable), ", ".join(tradeable) or "none"
        )

        last_trade_id = append_new_trades(last_trade_id)

        # ─── Position maintenance & entries ─────────────────────────
        for sym in tradeable:
            price = fetch_price(sym)
            base  = sym.split("/")[0]
            held  = bal.get(base, {}).get("total", 0)
            if positions[sym] and held < (exchange.markets[sym]["limits"]["amount"]["min"] or 0):
                positions[sym] = None

            pos = positions[sym]

            # ENTRY
            if pos is None and open_n < MAX_OPEN \
               and price <= last_price[sym] * DIP_THRESHOLD \
               and price > ema(sym):

                a      = atr(sym)
                slp    = price - a * SL_ATR_MULT
                tpp    = price + a * TP_ATR_MULT
                raw_qty= pos_size(price, slp, equity)
                max_q  = cash / price
                qty    = min(raw_qty, max_q)

                # round down to lot size
                minlot = exchange.markets[sym]["limits"]["amount"]["min"] or 1e-8
                qty  = math.floor(qty / minlot) * minlot
                qty  = float(exchange.amount_to_precision(sym, qty))

                cost = qty * price
                if qty < minlot or cost > cash:
                    logger.info("Skip buy %s – qty %.8f cost $%.2f", sym, qty, cost)
                else:
                    try:
                        limit_price = float(exchange.price_to_precision(sym, price))
                        order = exchange.create_limit_buy_order(
                            sym, qty, limit_price, {'postOnly': True}
                        )
                        positions[sym] = {"entry": price, "amount": qty, "sl": slp, "tp": tpp}
                        logger.info(
                            "BUY ▶ %-10s %.6f @ %.4f (order %s) (TP %.4f SL %.4f)",
                            sym, qty, limit_price, order['id'], tpp, slp
                        )
                        open_n += 1
                        cash   -= cost
                    except Exception as e:
                        logger.exception("Buy failed %s – %s", sym, e)

            # EXIT / MANAGEMENT
            elif pos:
                # trailing stop
                if price > pos["entry"]:
                    pos["sl"] = max(pos["sl"], price * (1 - TRAIL_PCT/100))

                # take profit or stop loss
                if price >= pos["tp"] or price <= pos["sl"]:
                    tag = "TP" if price >= pos["tp"] else "SL"
                    if safe_market_sell(sym, pos["amount"]):
                        positions[sym] = None
                        open_n -= 1
                        logger.info(
                            "SELL ◀ %-10s %.6f @ %.4f (%s)",
                            sym, pos["amount"], price, tag
                        )

            last_price[sym] = price

        time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        open_pos = {s: p for s, p in positions.items() if p}
        logger.warning("⏹ stopped – open positions: %s", open_pos)
        break
    except Exception:
        logger.exception("Main-loop error")
        time.sleep(POLL_INTERVAL)
