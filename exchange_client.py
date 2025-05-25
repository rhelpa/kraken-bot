import os, logging, ccxt
from decimal import Decimal
from dotenv import load_dotenv
from config import MODE, API_KEY, API_SECRET, KRAKEN_FUTURES_API, KRAKEN_FUTURES_SECRET
from ccxt.base.errors import BadSymbol, ExchangeError   # already used in option-3 patch
from symbol_map import map_sym                          # ← NEW

logger = logging.getLogger(__name__)

# Load environment
load_dotenv()
API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")
KRAKEN_FUTURES_API = os.getenv("KRAKEN_FUTURES_API")
KRAKEN_FUTURES_SECRET = os.getenv("KRAKEN_FUTURES_SECRET")

# Initialize CCXT exchange
if MODE=="SIM":
    exchange = ccxt.krakenfutures({
        "apiKey": KRAKEN_FUTURES_API,
        "secret": KRAKEN_FUTURES_SECRET,
        "enableRateLimit": True,
        "timeout": 60000,
    })

else:
    exchange = ccxt.kraken({
        "apiKey": API_KEY,
        "secret": API_SECRET,
        "enableRateLimit": True,
        "timeout": 60000,
    })
exchange.load_markets()
logger.info("Loaded %d markets", len(exchange.markets))


# ─── FACTORY ──────────────────────────────────────────────────────────────
def _make_exchange():
    if MODE == "SIM":
        ex = ccxt.krakenfutures({
            "apiKey": KRAKEN_FUTURES_API,
            "secret": KRAKEN_FUTURES_SECRET,
            "enableRateLimit": True,
        })
        ex.set_sandbox_mode(True)          # <-- key line
        logger.info("🔧 DEMO Futures sandbox enabled")
        return ex

    elif MODE == "SIM":
        from sim_exchange import SimExchange   # your own stub (option 3)
        return SimExchange()

    # default: live spot
    return ccxt.kraken({
        "apiKey": API_KEY,
        "secret": API_SECRET,
        "enableRateLimit": True,
    })

exchange = _make_exchange()
exchange.load_markets()
logger.info("Loaded %d markets", len(exchange.markets))



def fetch_price(symbol: str) -> Decimal | None:
    """Return the last traded price, or None if the symbol is unsupported/unavailable."""
         # ← translate BTC/USD → PI_XBTUSD, etc.
    fut_sym = map_sym(symbol)    
    try:
        ticker = exchange.fetch_ticker(fut_sym)
        return Decimal(ticker.get("last") or 0)
    except BadSymbol:
        logger.warning("%s unsupported on %s – skipping", symbol, exchange.id)
        return None
    except ExchangeError as exc:
        logger.error("fetch_ticker(%s) failed: %s", fut_sym, exc)
        return None


def account_cash() -> Decimal:
    """Get free USD cash balance, with logging."""
    logger.info("account_cash called")
    bal = exchange.fetch_balance()
    free_usd = bal.get("USD", {}).get("free", 0)
    cash = Decimal(str(free_usd))
    logger.info("Free USD balance: %s", cash)
    return cash


def fetch_all_trades(symbol: str, max_pages: int = 20):
    """Retrieve full trade history via pagination, with logging."""
    #logger.info("fetch_all_trades called for %s (max_pages=%d)", symbol, max_pages)
    trades, ofs = [], 0
    for page_num in range(1, max_pages + 1):
        logger.debug("Fetching page %d for %s (ofs=%d)", page_num, symbol, ofs)
        page = exchange.fetch_my_trades(symbol, params={"ofs": ofs})
        if not page:
            logger.info("No more trades on page %d for %s", page_num, symbol)
            break
        trades.extend(page)
        ofs += len(page)
        logger.debug("Fetched %d trades; total so far: %d", len(page), len(trades))
        if len(page) < 50:
            logger.info("Last page reached at page %d for %s", page_num, symbol)
            break
    sorted_trades = sorted(trades, key=lambda t: t["timestamp"])
    logger.info("Total trades fetched for %s: %d", symbol, len(sorted_trades))
    return sorted_trades


def open_position_from_history(symbol: str):
    # In offline back-tests we start with a clean slate.
    if MODE == "SIM":
        return None, None

    """Compute net average entry for current open amount (FIFO), with logging."""
    logger.info("open_position_from_history called for %s", symbol)
    inventory = []
    for t in fetch_all_trades(symbol):
        qty, price, side = t["amount"], t["price"], t["side"]
        logger.debug("Processing trade: side=%s, qty=%s, price=%s", side, qty, price)
        if side == "buy":
            inventory.append([qty, price])
        else:
            rem = qty
            logger.debug("Matching sell qty=%s against inventory entries", rem)
            while rem > 0 and inventory:
                if inventory[0][0] > rem:
                    inventory[0][0] -= rem
                    logger.debug("Partially matched; remaining entry qty=%s", inventory[0][0])
                    rem = 0
                else:
                    rem -= inventory[0][0]
                    logger.debug("Fully consumed entry; removing inventory[0]")
                    inventory.pop(0)
    total_qty = sum(q for q, _ in inventory)
    total_cost = sum(q * p for q, p in inventory)
    avg_price = Decimal("0")
    if total_qty:
        avg_price = Decimal(str(total_cost / total_qty))
    logger.info("Open position for %s – qty=%s, avg_price=%s", symbol, total_qty, avg_price)
    return total_qty, avg_price


def lot_step(sym: str) -> Decimal:
    """Return the minimum tradable lot size for sym, with logging."""
    #logger.info("lot_step called for %s", sym)
    raw = exchange.markets[sym]["limits"]["amount"]["min"] or 0
    step = Decimal(str(raw))
    #logger.info("Min lot step for %s: %s", sym, step)
    return step
