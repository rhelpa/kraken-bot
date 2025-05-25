# execution.py
import logging
import ccxt
from exchange_client import exchange, fetch_price
logger = logging.getLogger(__name__)

def safe_limit_sell(symbol: str, qty: float) -> bool:
    """Place a maker sell order or skip if conditions fail."""
    market = exchange.market(symbol)
    if not market.get("active"):  # delisted/inactive
        logger.warning("Skip %s – inactive/delisted", symbol)
        return False
    price = fetch_price(symbol)
    if price <= 0:
        logger.warning("Skip %s – price 0", symbol)
        return False

    base = market["base"]
    bal = exchange.fetch_balance()
    free = bal.get(base, {}).get("free", 0)
    qty = min(qty, free)
    min_lot = market["limits"]["amount"]["min"] or 0
    if qty < min_lot:
        logger.warning("Skip sell %s – free %.8f < min_lot %.8f", symbol, free, min_lot)
        return False

    try:
        book = exchange.fetch_order_book(symbol)
        best_bid = book["bids"][0][0]
        price = float(exchange.price_to_precision(symbol, best_bid))
        order = exchange.create_limit_sell_order(
            symbol, qty, price, {"postOnly": True}
        )
        logger.info("✅ Sold %.8f %s (order %s)", qty, symbol, order["id"])
        return True
    except ccxt.InsufficientFunds:
        logger.warning("Sell failed %s – insufficient funds", symbol)
    except Exception as e:
        logger.error("Sell failed %s – %s", symbol, e)
    return False


def place_mm_orders(symbol: str, stake_usd: float, spread_pct: float = 0.002):
    """Place paired post-only bids and asks around mid-price."""
    book = exchange.fetch_order_book(symbol)
    mid = (book["bids"][0][0] + book["asks"][0][0]) / 2
    size = stake_usd / mid
    buy_p  = float(exchange.price_to_precision(symbol, mid * (1 - spread_pct/2)))
    sell_p = float(exchange.price_to_precision(symbol, mid * (1 + spread_pct/2)))
    exchange.create_limit_buy_order(symbol,  size, buy_p,  {"postOnly": True})
    exchange.create_limit_sell_order(symbol, size, sell_p, {"postOnly": True})
