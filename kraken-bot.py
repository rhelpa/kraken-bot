#!/usr/bin/env python3
"""
kraken-bot.py
─────────────────────────────────────────────────────────────────────────────
• Dip-buy strategy with ATR-adaptive TP/SL, trailing stop, EMA trend filter
• Adopts any **existing wallet exposure** by reconstructing the true cost-basis
  from your Kraken trade history (weighted FIFO)
• Heart-beat every cycle (cash, equity, open trades) + concise BUY/SELL banners
• Appends every new trade fill to ~/kraken-trades.csv for permanent bookkeeping
"""

import os, time, csv, pathlib, logging, datetime
from   dotenv import load_dotenv
import ccxt

# ─── Config ──────────────────────────────────────────────────────────────
load_dotenv()
API_KEY, API_SECRET = os.getenv("API_KEY"), os.getenv("API_SECRET")

SYMBOLS        = ["SOL/USD","ETH/USD","BTC/USD","XRP/USD","DOGE/USD","TIA/USD","FARTCOIN/USD"]
DIP_THRESHOLD  = 0.99      # 1 % dip
EMA_PERIOD     = 50        # 1-h candles
ATR_PERIOD     = 14        # 1-min candles
TP_ATR_MULT    = 1.5
SL_ATR_MULT    = 1.0
TRAIL_PCT      = 0.7       # %
RISK_FRAC      = 0.01      # 1% account per new entry
MAX_OPEN       = 3
POLL_INTERVAL  = 30        # sec
MIN_USD_EXPOS  = 5         # ignore < $5 wallet value when adopting
TRADE_CSV      = pathlib.Path("~/kraken-trades.csv").expanduser()
LOG_PATH       = os.path.expanduser("~/kraken-bot.log")

# ─── Logging ─────────────────────────────────────────────────────────────
logger = logging.getLogger()
logger.setLevel(logging.DEBUG)

fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s: %(message)s",
                                  "%Y-%m-%d %H:%M:%S"))
logger.addHandler(fh)

ch = logging.StreamHandler()
ch.setFormatter(logging.Formatter("%(asctime)s %(levelname)s: %(message)s",
                                  "%H:%M:%S"))
ch.setLevel(logging.INFO)
logger.addHandler(ch)

# ─── Exchange ───────────────────────────────────────────────────────────
exchange = ccxt.kraken({
    "apiKey":  API_KEY,
    "secret":  API_SECRET,
    "enableRateLimit": True
})
exchange.load_markets()

# ─── Helpers ────────────────────────────────────────────────────────────
def fetch_price(sym):   return float(exchange.fetch_ticker(sym)["last"])
def balance(asset="USD"): return float(exchange.fetch_balance()["free"].get(asset,0))

def ema(sym, n=EMA_PERIOD):
    closes=[c[4] for c in exchange.fetch_ohlcv(sym,"1h",limit=n)]
    k,e=2/(n+1),closes[0]
    for p in closes[1:]: e = p*k+e*(1-k)
    return e

def atr(sym, n=ATR_PERIOD):
    ohlc=exchange.fetch_ohlcv(sym,"1m",limit=n+1)
    trs=[max(h-l,abs(h-cp),abs(l-cp))
         for (_,_,h,l,_,_),(_,_,_,_,cp,_) in zip(ohlc[1:],ohlc)]
    return sum(trs)/len(trs) if trs else 0

def pos_size(entry, stop, bal):
    risk = bal * RISK_FRAC
    unit = abs(entry-stop)
    return 0 if unit == 0 else risk/unit

# ─── Trade-history utilities ────────────────────────────────────────────
def fetch_all_trades(sym, max_pages=20):
    trades, ofs = [], 0
    for _ in range(max_pages):
        page = exchange.fetch_my_trades(sym, params={"ofs": ofs})
        if not page: break
        trades.extend(page)
        if len(page) < 50: break
        ofs += 50
    trades.sort(key=lambda t: t["timestamp"])
    return trades

def open_position_from_history(sym):
    inv = []
    for t in fetch_all_trades(sym):
        qty = t["amount"]
        if t["side"] == "buy":
            inv.append([qty, t["price"]])
        else:
            q = qty
            while q > 0 and inv:
                if inv[0][0] > q:
                    inv[0][0] -= q
                    q = 0
                else:
                    q -= inv[0][0]
                    inv.pop(0)
    qty_net = sum(q for q,_ in inv)
    if qty_net == 0:
        return 0, 0
    cost = sum(q*p for q,p in inv)
    return qty_net, cost/qty_net

# ─── CSV ledger ─────────────────────────────────────────────────────────
def append_new_trades(last_id=None):
    recent = exchange.fetch_my_trades(limit=50)
    if not recent: return last_id
    recent.sort(key=lambda t: t["id"])
    with TRADE_CSV.open("a", newline="") as f:
        w = csv.writer(f)
        if f.tell() == 0:
            w.writerow(["id","time","symbol","side","qty","price","cost","fee","order"])
        for t in recent:
            if last_id is not None and t["id"] <= last_id:
                continue
            w.writerow([t["id"], t["datetime"], t["symbol"], t["side"],
                        t["amount"], t["price"], t["cost"],
                        t["fee"]["cost"], t["order"]])
    return recent[-1]["id"]

# ─── Adopt wallet exposure ───────────────────────────────────────────────
wallet_bal = exchange.fetch_balance()
positions  = {}
for sym in SYMBOLS:
    base = sym.split("/")[0]
    held = float(wallet_bal["total"].get(base, 0))
    usd_val = held * fetch_price(sym)
    if usd_val >= MIN_USD_EXPOS:
        qty, entry = open_position_from_history(sym)
        if qty > 0:
            a = atr(sym) or entry*0.01
            positions[sym] = {
                "entry": entry,
                "amount": qty,
                "sl": entry - a*SL_ATR_MULT,
                "tp": entry + a*TP_ATR_MULT
            }
            logger.info("↻ Adopted %s %.6f @ %.4f  (TP %.4f  SL %.4f)",
                        sym, qty, entry,
                        entry + a*TP_ATR_MULT,
                        entry - a*SL_ATR_MULT)
        else:
            positions[sym] = None
    else:
        positions[sym] = None

last_price = {s: fetch_price(s) for s in SYMBOLS}
last_trade_id = append_new_trades(None)

# ─── Main loop ──────────────────────────────────────────────────────────
logger.info("▶ bot online – risk %.2f%%/trade, max %d open",
            RISK_FRAC*100, MAX_OPEN)

while True:
    try:
        ts = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        cash = balance()
        equity = cash + sum(
            p["amount"] * fetch_price(s)
            for s,p in positions.items() if p
        )
        open_n = sum(1 for p in positions.values() if p)

        # compute ticket size and unrealized P/L
        ticket = cash * RISK_FRAC
        unreal_pnl = sum(
            p["amount"] * (fetch_price(s) - p["entry"])
            for s,p in positions.items() if p
        )

        logger.info("♥ %s | Cash $%.2f | Equity $%.2f | Open %d | "
                    "Ticket $%.2f | UnrealPnL $%.2f",
                    ts, cash, equity, open_n,
                    ticket, unreal_pnl)

        # ─── Optional: estimate next trade cost for first symbol ──────────
        for sym in SYMBOLS:
            price = fetch_price(sym)
            stop  = price - atr(sym)*SL_ATR_MULT
            qty   = pos_size(price, stop, cash)
            cost  = qty * price
            logger.info("↗ Est next %-6s: qty %.4f → cost $%.2f (risk $%.2f)",
                        sym, qty, cost, ticket)

        # ────────────────────────────────────────────────────────────────

        # book any fresh fills into CSV
        last_trade_id = append_new_trades(last_trade_id)

        for sym in SYMBOLS:
            price = fetch_price(sym)
            pos = positions[sym]

            # ENTRY logic
            if pos is None and open_n < MAX_OPEN \
               and price <= last_price[sym] * DIP_THRESHOLD \
               and price > ema(sym):
                a   = atr(sym)
                slp = price - a * SL_ATR_MULT
                tpp = price + a * TP_ATR_MULT
                qty = pos_size(price, slp, cash)
                minlot = exchange.markets[sym]["limits"]["amount"]["min"] or 0
                if qty >= minlot:
                    try:
                        exchange.create_market_buy_order(sym, qty)
                        positions[sym] = {"entry": price, "amount": qty,
                                          "sl": slp, "tp": tpp}
                        logger.info("BUY ▶ %-6s %.6f @ %.4f "
                                    "(TP %.4f  SL %.4f)",
                                    sym, qty, price, tpp, slp)
                    except Exception:
                        logger.exception("Buy failed %s", sym)

            # MANAGEMENT / EXIT
            elif pos:
                if price > pos["entry"]:
                    pos["sl"] = max(pos["sl"],
                                    price * (1 - TRAIL_PCT / 100))
                if price >= pos["tp"] or price <= pos["sl"]:
                    try:
                        tag = "TP" if price >= pos["tp"] else "SL"
                        exchange.create_market_sell_order(sym, pos["amount"])
                        logger.info("SELL ◀ %-6s %.6f @ %.4f (%s)",
                                    sym, pos["amount"], price, tag)
                        positions[sym] = None
                    except Exception:
                        logger.exception("Sell failed %s", sym)

            last_price[sym] = price

        time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        still = {s:p for s,p in positions.items() if p}
        logger.warning("⏹ stopped – open positions: %s", still)
        break
    except Exception:
        logger.exception("Main-loop error")
        time.sleep(POLL_INTERVAL)