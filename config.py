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

# Second channel used to confirm PEAD signals — a stock is only bought once
# both channels have flagged it on the same (IST) day.
CONFIRMATION_CHANNEL = os.environ.get("CONFIRMATION_CHANNEL", "earnings_pulse")
CONFIRMATION_RATING_LABEL = os.environ.get("CONFIRMATION_RATING_LABEL", "Excellent")

STARTING_CAPITAL_INR = _float("STARTING_CAPITAL_INR", 1_000_000)
MAX_POSITION_VALUE_INR = _float("MAX_POSITION_VALUE_INR", 100_000)
MAX_OPEN_POSITIONS = _int("MAX_OPEN_POSITIONS", 10)

PEAD_BUY_SCORE_MIN = _float("PEAD_BUY_SCORE_MIN", 50)

# Exit strategy: intraday only, position always closes same day as entry.
# Sell immediately on a same-day gain of this size...
SAME_DAY_TARGET_PCT = _float("SAME_DAY_TARGET_PCT", 0.05)
# ...otherwise a stop that trails 2% below the highest price seen since entry...
TRAILING_STOP_PCT = _float("TRAILING_STOP_PCT", 0.02)
# ...otherwise force-close at whatever price is available once the market's about to shut.
MARKET_CLOSE_IST_HOUR = _int("MARKET_CLOSE_IST_HOUR", 15)
MARKET_CLOSE_IST_MINUTE = _int("MARKET_CLOSE_IST_MINUTE", 25)

# A signal confirmed outside NSE trading hours gets no real intraday window under the
# same-day-only rule (it would just buy and immediately force-close near the same
# price) - so buys are only taken between market open and MARKET_CLOSE_IST_HOUR:MINUTE.
MARKET_OPEN_IST_HOUR = _int("MARKET_OPEN_IST_HOUR", 9)
MARKET_OPEN_IST_MINUTE = _int("MARKET_OPEN_IST_MINUTE", 15)

REQUIRE_ABOVE_200DMA = os.environ.get("REQUIRE_ABOVE_200DMA", "true").lower() != "false"
MIN_MARKET_CAP_CR = _float("MIN_MARKET_CAP_CR", 2000)
RSI_PERIOD = _int("RSI_PERIOD", 14)
RSI_MIN = _float("RSI_MIN", 50)

POSITION_CHECK_INTERVAL_SECONDS = _int("POSITION_CHECK_INTERVAL_SECONDS", 300)

DB_PATH = str(BASE_DIR / "pead_bot.db")
IMAGE_CACHE_DIR = BASE_DIR / "downloaded_cards"
IMAGE_CACHE_DIR.mkdir(exist_ok=True)

BACKTEST_LOOKBACK_DAYS = _int("BACKTEST_LOOKBACK_DAYS", 90)
BACKTEST_SIGNAL_CACHE_PATH = str(BASE_DIR / "backtest_signal_cache.json")
BACKTEST_TRADES_CSV_PATH = str(BASE_DIR / "backtest_trades.csv")
