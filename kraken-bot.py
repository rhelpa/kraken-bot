#!/usr/bin/env python3
"""
kraken-bot.py
─────────────────────────────────────────────────────────────────────────────
• Dip-buy strategy with ATR-adaptive TP/SL, trailing stop & EMA trend filter
• Automatically **adopts pre-existing wallet exposure** (weighted FIFO cost)
• Heart-beat every cycle (cash, equity, open trades) + concise BUY/SELL banners
• Appends every new trade fill to /mnt/bot-log-share/kraken-trades.csv
• “Safe Sell” handler skips delisted/illiquid markets & checks wallet balance
"""

import os, time, csv, pathlib, logging, datetime
from   dotenv import load_dotenv
import ccxt

# ─── Config ──────────────────────────────────────────────────────────────
load_dotenv()
API_KEY, API_SECRET = os.getenv("API_KEY"), os.getenv("API_SECRET")

SYMBOLS        = ["SOL/USD","ETH/USD","BTC/USD","XRP/USD",
                  "DOGE/USD","TIA/USD","FARTCOIN/USD","GHIBLI/USD","BAL/USD","LOFI/USD","ZEC/USD","ELX/USD","BODEN/USD"]
# ─── Strategy Parameters (adjusted) ────────────────────────────────────────
DIP_THRESHOLD  = 0.98      # buy only on ≥2% dips (filters out noise)
EMA_PERIOD     = 20        # faster 20-hour EMA to track trend more responsively
ATR_PERIOD     = 14        # keep ATR on 1 min bars for volatility sizing
TP_ATR_MULT    = 2.0       # 2× ATR profit target (boosts gross R:R to ~2:1)
SL_ATR_MULT    = 1.2       # 1.2× ATR stop-loss (gives price a bit more breathing room)
TRAIL_PCT      = 1.0       # 1% trailing stop (locks in gains on strong moves)
RISK_FRAC      = 0.01     # risk just 0.5% of equity per new trade
MAX_OPEN       = 2         # no more than 2 concurrent positions (caps total risk ~1%)
POLL_INTERVAL  = 60        # poll every 60 s (reduces API load, still timely)
MIN_USD_EXPOS  = 10        # adopt only pre-existing positions ≥ $10 in size

# ─── Paths (absolute on mounted share) ───────────────────────────────────
MOUNT_DIR = pathlib.Path("/mnt/bot-log-share")
MOUNT_DIR.mkdir(parents=True, exist_ok=True)   # ensure path exists

TRADE_CSV = MOUNT_DIR / "kraken-trades.csv"
LOG_PATH  = MOUNT_DIR / "kraken-bot.log"

# ─── Logging ─────────────────────────────────────────────────────────────
logger = logging.getLogger()
logger.setLevel(logging.DEBUG)

fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
fh.setFormatter(logging.Formatter(
    "%(asctime)s %(levelname)s: %(message)s", "%Y-%m-%d %H:%M:%S"))
logger.addHandler(fh)

ch = logging.StreamHandler()
ch.setFormatter(logging.Formatter(
    "%(asctime)s %(levelname)s: %(message)s", "%H:%M:%S"))
ch.setLevel(logging.INFO)
logger.addHandler(ch)

# ─── Exchange ────────────────────────────────────────────────────────────
exchange = ccxt.kraken({
    "apiKey":          API_KEY,
    "secret":          API_SECRET,
    "enableRateLimit": True,
    "timeout":         60000,
})
exchange.load_markets()

# ─── Helpers ────────────────────────────────────────────────────────────
def fetch_price(sym):
    """Return last traded price, or 0 if the market doesn’t exist or an error occurs."""
    try:
        last = exchange.fetch_ticker(sym).get("last") or 0
        return float(last)
    except ccxt.BadSymbol:
        # this symbol isn’t on Kraken
        logger.warning("Skip %s — unknown market symbol", sym)
        return 0.0
    except Exception as e:
        # some other fetch error (network, timeout, parse, etc)
        logger.error("Error fetching price for %s — %s", sym, str(e))
        return 0.0

def account_cash():
    bal = exchange.fetch_balance()["free"]
    return float(bal.get("USD", 0))

def ema(sym, n=EMA_PERIOD):
    candles = exchange.fetch_ohlcv(sym, "1h", limit=n)
    closes  = [c[4] for c in candles]
    k, e    = 2/(n+1), closes[0]
    for p in closes[1:]:
        e = p*k + e*(1-k)
    return e

def atr(sym, n=ATR_PERIOD):
    ohlc = exchange.fetch_ohlcv(sym, "1m", limit=n+1)
    trs  = [
        max(h-l, abs(h-prev_c), abs(l-prev_c))
        for (_,_,h,l,_,_), (_,_,_,_,prev_c,_) in zip(ohlc[1:], ohlc)
    ]
    return sum(trs)/len(trs) if trs else 0

def pos_size(entry, stop, balance_usd):
    risk = balance_usd * RISK_FRAC
    unit = abs(entry - stop)
    return 0 if unit == 0 else risk / unit

# ─── Safe-sell wrapper ───────────────────────────────────────────────────
def safe_market_sell(sym, qty):
    """Attempt to sell qty; silently skip if market inactive or no funds."""
    mkt = exchange.market(sym)
    if not mkt["active"] or mkt.get("info", {}).get("status") == "delisted":
        logger.warning("Skip %s – market inactive/delisted", sym)
        return
    if fetch_price(sym) == 0:
        logger.warning("Skip %s – ticker price is 0", sym)
        return

    base        = mkt["base"]
    bal         = exchange.fetch_balance()
    free_amt    = bal.get(base, {}).get("free", 0)
    minlot      = mkt["limits"]["amount"]["min"] or 0
    qty_to_sell = min(qty, free_amt)

    if qty_to_sell < minlot:
        logger.warning("Skip sell %s – free %.8f < min lot %.8f",
                       sym, free_amt, minlot)
        return

    try:
        order = exchange.create_market_sell_order(sym, qty_to_sell)
        logger.info("✅ Sold %.8f %s (order %s)",
                    qty_to_sell, sym, order["id"])
        return True
    except ccxt.InsufficientFunds:
        logger.warning("Sell failed %s – insufficient funds (%.8f free)",
                       sym, free_amt)
    except ccxt.BaseError as e:
        logger.error("Sell failed %s – %s", sym, str(e))
    return False

# ─── Trade-history utilities ─────────────────────────────────────────────
def fetch_all_trades(sym, max_pages=20):
    trades, ofs = [], 0
    for _ in range(max_pages):
        page = exchange.fetch_my_trades(sym, params={"ofs": ofs})
        if not page:
            break
        trades.extend(page)
        ofs += 50
        if len(page) < 50:
            break
    trades.sort(key=lambda t: t["timestamp"])
    return trades

def open_position_from_history(sym):
    inv = []
    for t in fetch_all_trades(sym):
        qty = t["amount"]
        if t["side"] == "buy":
            inv.append([qty, t["price"]])
        else:  # sell
            q = qty
            while q > 0 and inv:
                if inv[0][0] > q:
                    inv[0][0] -= q
                    q = 0
                else:
                    q -= inv[0][0]
                    inv.pop(0)
    qty_net = sum(q for q, _ in inv)
    if qty_net == 0:
        return 0, 0
    cost = sum(q * p for q, p in inv)
    return qty_net, cost / qty_net

# ─── CSV ledger ─────────────────────────────────────────────────────────
def append_new_trades(last_id=None):
    recent = exchange.fetch_my_trades(limit=50)
    if not recent:
        return last_id
    recent.sort(key=lambda t: t["id"])
    with TRADE_CSV.open("a", newline="") as f:
        w = csv.writer(f)
        if f.tell() == 0:
            w.writerow(["id", "time", "symbol", "side", "qty",
                        "price", "cost", "fee", "order"])
        for t in recent:
            if last_id is not None and t["id"] <= last_id:
                continue
            w.writerow([t["id"], t["datetime"], t["symbol"], t["side"],
                        t["amount"], t["price"], t["cost"],
                        t["fee"]["cost"], t["order"]])
    return recent[-1]["id"]

# ─── Initial wallet adoption ────────────────────────────────────────────
positions = {}
bal       = exchange.fetch_balance()
for sym in SYMBOLS:
    base    = sym.split("/")[0]
    held    = float(bal.get(base, {}).get("total", 0))
    usd_val = held * fetch_price(sym)
    if usd_val >= MIN_USD_EXPOS:
        qty, entry = open_position_from_history(sym)
        if qty:
            a = atr(sym) or entry * 0.01
            positions[sym] = {"entry": entry, "amount": qty,
                              "sl": entry - a * SL_ATR_MULT,
                              "tp": entry + a * TP_ATR_MULT}
            logger.info("↻ Adopted %s %.6f @ %.4f  (TP %.4f SL %.4f)",
                        sym, qty, entry, entry + a * TP_ATR_MULT,
                        entry - a * SL_ATR_MULT)
        else:
            positions[sym] = None
    else:
        positions[sym] = None

last_price    = {s: fetch_price(s) for s in SYMBOLS}
last_trade_id = append_new_trades(None)

# ─── Main loop ──────────────────────────────────────────────────────────
logger.info("▶ bot online – risk %.2f%%/trade, max %d open",
            RISK_FRAC * 100, MAX_OPEN)

while True:
    try:
        # ── Snapshot cash/equity ─────────────────────────────────────────
        cash   = account_cash()
        bal    = exchange.fetch_balance()
        equity = cash + sum(
            bal.get(s.split("/")[0], {}).get("total", 0) * fetch_price(s)
            for s in SYMBOLS
        )
        open_n = sum(1 for p in positions.values() if p)

        ticket     = cash * RISK_FRAC
        unreal_pnl = sum(
            (bal.get(s.split("/")[0], {}).get("total", 0) -
             (positions[s]["amount"] if positions[s] else 0)) *
            fetch_price(s)
            for s in SYMBOLS if positions.get(s)
        )

        logger.info("♥ %s UTC | Cash $%.2f | Equity $%.2f | Open %d | "
                    "Ticket $%.2f | UnrealPnL $%.2f",
                    datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                    cash, equity, open_n, ticket, unreal_pnl)

        # ─── Scan symbols ────────────────────────────────────────────────
        tradeable = []
        for sym in SYMBOLS:
            price  = fetch_price(sym)
            mkt    = exchange.markets[sym]
            minlot = mkt["limits"]["amount"]["min"] or 0
            req    = minlot * price
            ok     = (mkt["active"] and price > 0 and minlot > 0 and cash >= req)
            if ok:
                tradeable.append(sym)
            logger.info("↗ %-10s $%.4f | minlot %.4f (~$%.2f) %s",
                        sym, price, minlot, req, "✅" if ok else "❌")
        logger.info("📊 Tradeable (%d): %s",
                    len(tradeable), ", ".join(tradeable) or "none")

        last_trade_id = append_new_trades(last_trade_id)

        # ─── Position maintenance & entries ──────────────────────────────
        for sym in SYMBOLS:
            price = fetch_price(sym)
            if price == 0:                         # skip dead ticker
                continue

            # Prune ghost positions (balance dropped below minlot)
            base   = sym.split("/")[0]
            held   = bal.get(base, {}).get("total", 0)
            minlot = exchange.markets[sym]["limits"]["amount"]["min"] or 0
            if positions[sym] and held < minlot:
                positions[sym] = None

            pos = positions[sym]

            # ENTRY
            if pos is None and open_n < MAX_OPEN \
            and price <= last_price[sym] * DIP_THRESHOLD \
            and price > ema(sym):

                a   = atr(sym)
                slp = price - a * SL_ATR_MULT
                tpp = price + a * TP_ATR_MULT
                # 1) how many units our risk model wants
                raw_qty = pos_size(price, slp, cash)
                # 2) the absolute max units our free USD can buy
                max_qty = cash / price
                # 3) pick the smaller, then round down to the market’s lot size
                qty = min(raw_qty, max_qty)
                #    (requires ccxt v1; adjust for v2 if needed)
                qty = float(exchange.amount_to_precision(sym, qty))

                if qty >= minlot:
                    try:
                        exchange.create_market_buy_order(sym, qty)
                        positions[sym] = {"entry": price, "amount": qty,
                                          "sl": slp, "tp": tpp}
                        logger.info("BUY ▶ %-10s %.6f @ %.4f (TP %.4f SL %.4f)",
                                    sym, qty, price, tpp, slp)
                        open_n += 1
                    except Exception:
                        logger.exception("Buy failed %s", sym)

            # MANAGEMENT / EXIT
            elif pos:
                if price > pos["entry"]:           # trail stop
                    pos["sl"] = max(pos["sl"],
                                    price * (1 - TRAIL_PCT / 100))

                if price >= pos["tp"] or price <= pos["sl"]:
                    tag = "TP" if price >= pos["tp"] else "SL"
                    if safe_market_sell(sym, pos["amount"]):
                        positions[sym] = None
                        open_n -= 1
                        logger.info("SELL ◀ %-10s %.6f @ %.4f (%s)",
                                    sym, pos["amount"], price, tag)

            last_price[sym] = price

        time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        still = {s: p for s, p in positions.items() if p}
        logger.warning("⏹ stopped – open positions: %s", still)
        break
    except Exception:
        logger.exception("Main-loop error")
        time.sleep(POLL_INTERVAL)
