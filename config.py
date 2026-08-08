import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _int(name, default):
    value = os.environ.get(name)
    return int(value) if value else default


def _float(name, default):
    value = os.environ.get(name)
    return float(value) if value else default


TELEGRAM_API_ID = _int("TELEGRAM_API_ID", 0)
TELEGRAM_API_HASH = os.environ.get("TELEGRAM_API_HASH", "")
TELEGRAM_SESSION_NAME = str(BASE_DIR / "telegram_session")

TELEGRAM_GROUP_ID = _int("TELEGRAM_GROUP_ID", 2413883243)
TELEGRAM_PEAD_TOPIC_ID = _int("TELEGRAM_PEAD_TOPIC_ID", 683)
TELEGRAM_SIGNAL_SENDER = os.environ.get("TELEGRAM_SIGNAL_SENDER", "FinanciallyFreeFFBot")

# Every stock this channel rates BUY_RATING_LABEL is bought immediately, during
# market hours, with no other fundamental/technical filter.
CONFIRMATION_CHANNEL = os.environ.get("CONFIRMATION_CHANNEL", "earnings_pulse")
BUY_RATING_LABEL = os.environ.get("BUY_RATING_LABEL", "Excellent")

STARTING_CAPITAL_INR = _float("STARTING_CAPITAL_INR", 1_000_000)
MAX_POSITION_VALUE_INR = _float("MAX_POSITION_VALUE_INR", 100_000)
MAX_OPEN_POSITIONS = _int("MAX_OPEN_POSITIONS", 10)

# PEAD cards are still downloaded and logged (still useful context/data) but no
# longer gate a buy — kept here only for informational/backtest-comparison use.
PEAD_BUY_SCORE_MIN = _float("PEAD_BUY_SCORE_MIN", 50)
REQUIRE_ABOVE_200DMA = os.environ.get("REQUIRE_ABOVE_200DMA", "true").lower() != "false"
MIN_MARKET_CAP_CR = _float("MIN_MARKET_CAP_CR", 2000)
RSI_PERIOD = _int("RSI_PERIOD", 14)
RSI_MIN = _float("RSI_MIN", 50)

# Exit strategy: intraday only, position always closes same day as entry, no upper
# target — ride it until a stop trailing TRAILING_STOP_PCT below the highest price
# seen since entry, or a forced close near market end, whichever comes first.
TRAILING_STOP_PCT = _float("TRAILING_STOP_PCT", 0.02)
MARKET_CLOSE_IST_HOUR = _int("MARKET_CLOSE_IST_HOUR", 15)
MARKET_CLOSE_IST_MINUTE = _int("MARKET_CLOSE_IST_MINUTE", 30)

# Buys (and the "immediately" in "buy immediately") only happen inside this window.
MARKET_OPEN_IST_HOUR = _int("MARKET_OPEN_IST_HOUR", 9)
MARKET_OPEN_IST_MINUTE = _int("MARKET_OPEN_IST_MINUTE", 15)

POSITION_CHECK_INTERVAL_SECONDS = _int("POSITION_CHECK_INTERVAL_SECONDS", 300)

DB_PATH = str(BASE_DIR / "pead_bot.db")
IMAGE_CACHE_DIR = BASE_DIR / "downloaded_cards"
IMAGE_CACHE_DIR.mkdir(exist_ok=True)

BACKTEST_LOOKBACK_DAYS = _int("BACKTEST_LOOKBACK_DAYS", 90)
BACKTEST_SIGNAL_CACHE_PATH = str(BASE_DIR / "backtest_signal_cache.json")
BACKTEST_TRADES_CSV_PATH = str(BASE_DIR / "backtest_trades.csv")

# Separate, shorter default for the intraday (5m-bar) backtest — Yahoo only retains
# ~60 days of 5m history, and the strategy itself is inherently intraday-precise.
INTRADAY_BACKTEST_LOOKBACK_DAYS = _int("INTRADAY_BACKTEST_LOOKBACK_DAYS", 7)
