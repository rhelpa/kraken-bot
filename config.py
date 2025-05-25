# config.py
from pathlib import Path
from dotenv import load_dotenv
from decimal import Decimal
# point explicitly at your .env file (in the same dir as config.py)
env_path = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=env_path)
import os


from constants import LOG_PATH, SYMBOLS, POLL_INTERVAL, RISK_FRAC

print("ENV LOADED:", dict(
    API_KEY=os.getenv("API_KEY"),
    API_SECRET=os.getenv("API_SECRET"),
    GIT_PAT = os.getenv("GIT_PAT")

))

API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")
GIT_PAT = os.getenv("GIT_PAT")


if not API_KEY or not API_SECRET:
    raise RuntimeError("❌ Missing API_KEY or API_SECRET in .env")

# Strategy constants
SYMBOLS       = [
    "SOL/USD", "ETH/USD", "BTC/USD", "XRP/USD",
    "DOGE/USD", "TIA/USD", "FARTCOIN/USD", "GHIBLI/USD",
    "BAL/USD", "LOFI/USD", "ZEC/USD", "ELX/USD", "BODEN/USD"
]
DIP_THRESHOLD = 0.98    # deeper pullbacks only
EMA_PERIOD    = 20      # 20‑hour EMA
ATR_PERIOD    = 14      # 14 × 1‑min bars
TP_ATR_MULT   = Decimal("3.0")     # profit target: 3× ATR
SL_ATR_MULT   = Decimal("1.5")     # stop loss: 1.5× ATR
TRAIL_PCT     = 1.0     # 1 % trailing stop
RISK_FRAC     = Decimal("0.05")    # 2 % of equity per entry
MAX_OPEN      = 2
POLL_INTERVAL = 30      # seconds
MIN_USD_EXPOS = 10      # adopt only positions ≥ $10
MIN_24H_VOL   = 50_000  # minimum $50 k of daily volume
MIN_BOOK_UNITS= 50      # min base‑asset units in top book
MIN_ORDER_USD = Decimal("5")	# skip any pair whose minlot x price < MIN_ORDER_USD


# Paths (NAS share)
MOUNT_DIR = Path("/mnt/bot-log-share")
MOUNT_DIR.mkdir(parents=True, exist_ok=True)
TRADE_CSV = MOUNT_DIR / "kraken-trades.csv"
LOG_PATH  = MOUNT_DIR / "kraken-bot.log"
