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
STOP_LOSS_PCT = _float("STOP_LOSS_PCT", 0.08)
TARGET_PCT = _float("TARGET_PCT", 0.15)
REQUIRE_ABOVE_200DMA = os.environ.get("REQUIRE_ABOVE_200DMA", "true").lower() != "false"

POSITION_CHECK_INTERVAL_SECONDS = _int("POSITION_CHECK_INTERVAL_SECONDS", 300)

DB_PATH = str(BASE_DIR / "pead_bot.db")
IMAGE_CACHE_DIR = BASE_DIR / "downloaded_cards"
IMAGE_CACHE_DIR.mkdir(exist_ok=True)

BACKTEST_LOOKBACK_DAYS = _int("BACKTEST_LOOKBACK_DAYS", 90)
BACKTEST_SIGNAL_CACHE_PATH = str(BASE_DIR / "backtest_signal_cache.json")
BACKTEST_TRADES_CSV_PATH = str(BASE_DIR / "backtest_trades.csv")
