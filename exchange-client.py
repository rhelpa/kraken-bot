# exchange_client.py
import ccxt
import logging
from config import API_KEY, API_SECRET

logger = logging.getLogger(__name__)



# Initialize CCXT exchange
exchange = ccxt.kraken({
    "apiKey": API_KEY,
    "secret": API_SECRET,
    "enableRateLimit": True,
    "timeout": 60000,
})
exchange.load_markets()


def fetch_price(symbol: str) -> float:
    try:
        ticker = exchange.fetch_ticker(symbol)
        return float(ticker["last"])
    except Exception as e:
        logger.error(f"fetch_price({symbol}) failed: {e}")
        raise

def account_cash() -> float:
    try:
        balance = exchange.fetch_balance()
        return float(balance["USD"]["free"])
    except Exception as e:
        logger.error(f"account_cash() failed: {e}")
        raise

def fetch_all_trades(symbol: str, max_pages: int = 20):
    """Retrieve full trade history via pagination."""
    trades, ofs = [], 0
    for _ in range(max_pages):
        page = exchange.fetch_my_trades(symbol, params={"ofs": ofs})
        if not page:
            break
        trades.extend(page)
        ofs += len(page)
        if len(page) < 50:
            break
    return sorted(trades, key=lambda t: t["timestamp"])


def open_position_from_history(symbol: str):
    """Compute net avg entry for current open amount (FIFO)."""
    inventory = []
    for t in fetch_all_trades(symbol):
        qty, price, side = t["amount"], t["price"], t["side"]
        if side == "buy":
            inventory.append([qty, price])
        else:
            rem = qty
            while rem > 0 and inventory:
                if inventory[0][0] > rem:
                    inventory[0][0] -= rem
                    rem = 0
                else:
                    rem -= inventory[0][0]
                    inventory.pop(0)
    total_qty = sum(q for q, _ in inventory)
    if total_qty == 0:
        return 0, 0
    total_cost = sum(q * p for q, p in inventory)
    return total_qty, total_cost / total_qty
