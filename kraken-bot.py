import time
import datetime
from decimal import Decimal
from dataclasses import dataclass
from typing import Dict, List, Tuple

from config import LOG_PATH, SYMBOLS, POLL_INTERVAL, RISK_FRAC, DIP_THRESHOLD, MIN_ORDER_USD
from logger_setup import setup_logger
from exchange_client import fetch_price, account_cash, exchange, lot_step
from ledger import append_new_trades, initialize_positions
from strategy import TradeAction, generate_actions
from indicators import ema

# ──────────────────────────────────────────────────────────────────────────
# LOGGER SETUP
logger = setup_logger(log_path=LOG_PATH)
logger.info("🟢 Kraken bot starting…")

# ──────────────────────────────────────────────────────────────────────────
# DATA CLASSES
@dataclass
class PortfolioMetrics:
    cash: Decimal
    equity: Decimal
    cost_basis: Decimal
    unreal: Decimal
    ticket: Decimal
    open_n: int
    wallet_qty: Dict[str, Decimal]

# ──────────────────────────────────────────────────────────────────────────
# INITIALIZE PEAK CACHE
def initialize_peak_cache(positions: Dict[str, dict]) -> Dict[str, Decimal]:
    cache: Dict[str, Decimal] = {}
    for sym, pos in positions.items():
        if not pos:
            continue
        entry_price = pos.get("entry") or pos.get("avg_price") or pos.get("blended_price")
        if entry_price is None:
            logger.warning("Skipping peak_cache for %s: no entry price in %r", sym, pos)
            continue
        cache[sym] = entry_price
    return cache

# ──────────────────────────────────────────────────────────────────────────
# SNAPSHOT METRICS
def snapshot_metrics(positions: Dict[str, dict]) -> Tuple[Dict[str, Decimal], PortfolioMetrics]:
    start = time.time()
    # price snapshot
    price_snapshot = {sym: Decimal(fetch_price(sym)) for sym in SYMBOLS}
    # balances
    bal = exchange.fetch_balance()
    cash = Decimal(str(account_cash()))
    # wallet quantities
    wallet_qty = {
        sym: Decimal(str(bal.get(sym.split("/")[0], {}).get("total", 0)))
        for sym in SYMBOLS
    }
    # cost basis and portfolio value
    cost_basis = sum(pos["amount"] * pos.get("avg_price", Decimal(0)) for pos in positions.values() if pos)
    portfolio_value = sum(wallet_qty[sym] * price_snapshot[sym] for sym in SYMBOLS)
    equity = cash + portfolio_value
    unreal = equity - (cash + cost_basis)
    ticket = max(RISK_FRAC * equity, Decimal(str(MIN_ORDER_USD)))
    open_n = sum(1 for p in positions.values() if p)
    elapsed = time.time() - start
    logger.debug("snapshot_metrics took %.3f s", elapsed)

    metrics = PortfolioMetrics(
        cash=cash,
        equity=equity,
        cost_basis=cost_basis,
        unreal=unreal,
        ticket=ticket,
        open_n=open_n,
        wallet_qty=wallet_qty,
    )
    return price_snapshot, metrics

# ──────────────────────────────────────────────────────────────────────────
# LOGGING FUNCTIONS
def log_heartbeat(metrics: PortfolioMetrics):
    now = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    logger.info(
        "HEARTBEAT | %s UTC | Cash $%.2f | Equity $%.2f | Open %d | Ticket $%.2f | UnrealPnL $%.2f",
        now, metrics.cash, metrics.equity, metrics.open_n, metrics.ticket, metrics.unreal
    )

def log_dip_details(price_snapshot: Dict[str, Decimal]):
    start = time.time()
    for sym in SYMBOLS:
        ref_price = ema(sym)
        if ref_price > 0:
            ratio = price_snapshot[sym] / ref_price
        else:
            ratio = Decimal('NaN')
        logger.info(
            "DIPLOG | %-6s current=%.2f | ref=%.2f | ratio=%.4f",
            sym, price_snapshot[sym], ref_price, ratio
        )
    elapsed = time.time() - start
    logger.debug("log_dip_details took %.3f s", elapsed)

def log_action_summary(
    actions: List[TradeAction],
    filter_reasons: Dict[str, List[str]],
    gen_reasons: Dict[str, List[str]]
):
    # build a map from symbol → human-readable “outcome”
    summary: Dict[str, str] = {}
    # first, mark every symbol “filtered” or “no-signal” by default
    for sym in SYMBOLS:
        if sym in filter_reasons:
            summary[sym] = f"FILTERED ({','.join(filter_reasons[sym])})"
        else:
            # if it was tradeable but generate_actions returned nothing
            summary[sym] = (
                f"NO-SIGNAL ({','.join(gen_reasons.get(sym, ['?']))})"
                if sym in gen_reasons
                else "—"
            )

    # now overwrite with any actions you actually placed
    for act in actions:
        summary[act.symbol] = f"{act.side.upper()} {float(act.qty)} @ {act.price}"

    # log it as one tidy line (or break it out however you like)
    rows = [f"{sym}: {summary[sym]}" for sym in SYMBOLS]
    logger.info("SUMMARY BY SYMBOL | %s", " | ".join(rows))


# ──────────────────────────────────────────────────────────────────────────
# FILTER TRADEABLE SYMBOLS
def find_tradeable(
    price_snapshot: Dict[str, Decimal],
    cash: Decimal
) -> Tuple[List[str], Dict[str, List[str]]]:
    tradeable: List[str] = []
    skipped_reasons: Dict[str, List[str]] = {}
    threshold = Decimal(str(DIP_THRESHOLD))

    for sym in SYMBOLS:
        price = price_snapshot[sym]
        ref = ema(sym)
        mkt = exchange.markets[sym]
        minlot = Decimal(str(lot_step(sym)))
        req = minlot * price

        reasons: List[str] = []
        if not mkt.get("active", False) or price == 0:
            reasons.append("inactive")
        if cash < req:
            reasons.append("cash<min")
        if ref <= 0:
            reasons.append("no-ref")
        else:
            ratio = price / ref
            if ratio > threshold:
                reasons.append("no-dip")

        if reasons:
            skipped_reasons[sym] = reasons
            status, note = "❌", f" ({', '.join(reasons)})"
        else:
            tradeable.append(sym)
            status, note = "✅", ""

        logger.info(
            "FILTER | %-6s | $%.4f | minlot=%.4f (~$%.2f) %s%s",
            sym, price, minlot, req, status, note
        )

    logger.info("TRADEABLE | %d symbols: %s", len(tradeable), tradeable)
    return tradeable, skipped_reasons


# ──────────────────────────────────────────────────────────────────────────
# GENERATE ALL TRADE ACTIONS
def generate_all_actions(
    tradeable: List[str],
    positions: Dict[str, dict],
    last_price: Dict[str, Decimal],
    metrics: PortfolioMetrics,
    peak_cache: Dict[str, Decimal]
) -> Tuple[List[TradeAction], Dict[str, List[str]]]:
    actions: List[TradeAction] = []
    gen_skipped: Dict[str, List[str]] = {}

    for sym in tradeable:
        acts, reasons = generate_actions(
            sym=sym,
            positions=positions,
            last_price=last_price,
            open_n=metrics.open_n,
            cash=metrics.cash,
            equity=metrics.equity,
            peak_cache=peak_cache,
        )

        if acts:
            actions.extend(acts)
        else:
            # carry forward the actual rule-names
            gen_skipped[sym] = reasons or ["unknown"]

    return actions, gen_skipped

# ──────────────────────────────────────────────────────────────────────────
# EXECUTE TRADE ACTIONS
def execute_actions(actions: List[TradeAction]):
    for act in actions:
        try:
            order = exchange.create_order(
                symbol=act.symbol,
                type="market",
                side=act.side,
                amount=float(act.qty)
            )
            order_id = order.get("id")
            status = order.get("status")
            logger.info(
                "ORDER | %s %s @ %.4f × %.4f → id=%s status=%s",
                act.side.upper(), act.symbol, act.price, act.qty, order_id, status
            )
        except Exception as e:
            logger.exception("Order failed for %s %s: %r", act.side, act.symbol, e)

# ──────────────────────────────────────────────────────────────────────────
# HOUSEKEEPING: RECORD AND UPDATE POSITIONS
def housekeeping(
    actions: List[TradeAction],
    last_trade_id: int,
    positions: Dict[str, dict],
    peak_cache: Dict[str, Decimal]
) -> int:
    new_trade_id = append_new_trades(last_trade_id)
    for act in actions:
        if act.side == "buy":
            positions[act.symbol] = {
                "amount": act.qty,
                "entry": act.price,
                "sl": act.sl,
                "tp": act.tp,
            }
            peak_cache[act.symbol] = act.price
        elif act.side == "sell":
            positions.pop(act.symbol, None)
            peak_cache.pop(act.symbol, None)
    return new_trade_id

# ──────────────────────────────────────────────────────────────────────────
# MAIN LOOP

def main_loop():
    positions = initialize_positions()
    peak_cache = initialize_peak_cache(positions)
    last_price = {s: Decimal(str(fetch_price(s))) for s in SYMBOLS}
    last_trade_id = append_new_trades(None)

    logger.info("▶ bot online – risk %.2f%%/trade", RISK_FRAC * 100)

    while True:
        loop_start = time.time()
        try:
            # 1) Metrics snapshot
            price_snapshot, metrics = snapshot_metrics(positions)
            # 2) Heartbeat + dip details
            log_heartbeat(metrics)
            log_dip_details(price_snapshot)
            logger.debug("Last prices: %s", last_price)

            # 3) Filter 
            tradeable, filter_reasons = find_tradeable(price_snapshot, metrics.cash)

            # 4) Generate
            actions, gen_reasons = generate_all_actions(
                tradeable, positions, last_price, metrics, peak_cache
                )
            logger.info("RAW ACTION COUNT | %d", len(actions))
            log_action_summary(actions, filter_reasons, gen_reasons)

            # 5) Execute
            execute_actions(actions)

            # 6) Record and update
            last_trade_id = housekeeping(actions, last_trade_id, positions, peak_cache)

            # 7) Prepare for next cycle
            last_price = price_snapshot.copy()
            elapsed = time.time() - loop_start
            logger.info("Cycle complete in %.2f s; sleeping %d s", elapsed, POLL_INTERVAL)
            time.sleep(POLL_INTERVAL)

        except KeyboardInterrupt:
            logger.warning("⏹ stopped – open positions: %s", {s: p for s, p in positions.items() if p})
            break
        except Exception:
            logger.exception("Unhandled error in main loop")
            time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main_loop()
