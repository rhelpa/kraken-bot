import ccxt
from decimal import Decimal
from exchange_client import exchange, fetch_price
import logging

logger = logging.getLogger(__name__)


def safe_limit_sell(symbol: str, qty: float) -> bool:
    """Place a maker sell order or skip if conditions fail, with detailed logging."""
    logger.info("safe_limit_sell called for %s with requested qty=%.8f", symbol, qty)
    market = exchange.market(symbol)
    active = market.get("active", False)
    if not active:
        logger.warning("Skip %s – inactive/delisted", symbol)
        return False
    logger.debug("Market %s is active", symbol)

    price = fetch_price(symbol)
    logger.debug("Fetched current price for %s: %s", symbol, price)
    if price <= 0:
        logger.warning("Skip %s – non-positive price %s", symbol, price)
        return False

    base = market["base"]
    bal = exchange.fetch_balance()
    free = bal.get(base, {}).get("free", 0)
    total = bal.get(base, {}).get("total", 0)
    logger.info("Balance for %s – free: %.8f, total: %.8f", base, free, total)
    qty = min(qty, free)

    min_lot = market.get("limits", {}).get("amount", {}).get("min") or 0
    logger.debug("Min lot for %s: %.8f", symbol, min_lot)
    if qty < min_lot:
        logger.warning("Skip sell %s – adjusted qty %.8f < min_lot %.8f", symbol, qty, min_lot)
        return False

    try:
        logger.info("Fetching order book for %s", symbol)
        book = exchange.fetch_order_book(symbol)
        best_bid = book["bids"][0][0]
        logger.debug("Best bid price for %s: %s", symbol, best_bid)

        price_str = exchange.price_to_precision(symbol, best_bid)
        price = float(price_str)
        logger.debug("Rounded price for order: %s", price)

        logger.info("Creating post-only limit sell order for %s: qty=%.8f at price=%.8f", symbol, qty, price)
        order = exchange.create_limit_sell_order(symbol, qty, price, {"postOnly": True})
        logger.info("✅ Sold %.8f %s – order id %s", qty, symbol, order.get("id"))
        return True
    except ccxt.InsufficientFunds:
        logger.warning("Sell failed %s – insufficient funds", symbol)
    except Exception as e:
        logger.error("Sell failed %s – error: %s", symbol, e)
    return False


def place_mm_orders(symbol: str, stake_usd: float, spread_pct: float = 0.002):
    """Place paired post-only bids and asks around mid-price, with detailed logging."""
    logger.info("place_mm_orders called for %s with stake_usd=%.2f, spread_pct=%.3f", symbol, stake_usd, spread_pct)
    book = exchange.fetch_order_book(symbol)
    mid = (book["bids"][0][0] + book["asks"][0][0]) / 2
    logger.debug("Calculated mid price for %s: %s", symbol, mid)

    size = stake_usd / mid
    logger.debug("Calculated order size for %s: %s", symbol, size)

    buy_price_str = exchange.price_to_precision(symbol, mid * (1 - spread_pct / 2))
    sell_price_str = exchange.price_to_precision(symbol, mid * (1 + spread_pct / 2))
    buy_p = Decimal(buy_price_str)
    sell_p = Decimal(sell_price_str)
    logger.info("Placing buy order for %s: size=%s at price=%s", symbol, size, buy_p)
    try:
        buy_order = exchange.create_limit_buy_order(symbol, size, buy_p, {"postOnly": True})
        logger.info("✅ Buy order created for %s – id %s", symbol, buy_order.get("id"))
    except Exception as e:
        logger.error("Buy order failed for %s – error: %s", symbol, e)

    logger.info("Placing sell order for %s: size=%s at price=%s", symbol, size, sell_p)
    try:
        sell_order = exchange.create_limit_sell_order(symbol, size, sell_p, {"postOnly": True})
        logger.info("✅ Sell order created for %s – id %s", symbol, sell_order.get("id"))
    except Exception as e:
        logger.error("Sell order failed for %s – error: %s", symbol, e)
