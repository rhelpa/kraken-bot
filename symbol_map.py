# symbol_map.py
SPOT_TO_FUT = {
    "BTC/USD":  "PI_XBTUSD",   # inverse perpetual
    "ETH/USD":  "PI_ETHUSD",
    "SOL/USD":  "PF_SOLUSD",   # linear perpetual
    "XRP/USD":  "PF_XRPUSD",
    "DOGE/USD": "PF_DOGEUSD"
    # add the contracts you want
}

def map_sym(sym: str) -> str:
    from config import MODE
    if MODE == "SIM":
        # return explicit mapping or auto-generate PF_<BASE>USD
        return SPOT_TO_FUT.get(sym, f"PF_{sym.split('/')[0]}USD")
    return sym
