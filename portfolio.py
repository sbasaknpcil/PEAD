import logging
import math
from datetime import datetime, timezone

import config
import price_feed
import storage

log = logging.getLogger("portfolio")


def _within_market_hours(now_ist):
    open_minutes = config.MARKET_OPEN_IST_HOUR * 60 + config.MARKET_OPEN_IST_MINUTE
    close_minutes = config.MARKET_CLOSE_IST_HOUR * 60 + config.MARKET_CLOSE_IST_MINUTE
    now_minutes = now_ist.hour * 60 + now_ist.minute
    return open_minutes <= now_minutes < close_minutes


def buy(ticker):
    """Buy immediately on an earnings_pulse BUY_RATING_LABEL rating alone - no PEAD
    score, 200DMA, RSI, or market-cap check. Every position exits the same day it
    opens (see check_exits), so a rating outside market hours has no trading window
    left and is skipped rather than bought."""
    now_ist = datetime.now(timezone.utc).astimezone(storage.IST)
    if not _within_market_hours(now_ist):
        log.info(
            "Skipping %s: rated outside market hours (%s IST) - no same-day window left",
            ticker, now_ist.strftime("%H:%M"),
        )
        return False

    if storage.get_position(ticker):
        log.info("Skipping %s: position already open", ticker)
        return False

    open_positions = storage.get_open_positions()
    if len(open_positions) >= config.MAX_OPEN_POSITIONS:
        log.info("Skipping %s: max open positions (%d) reached", ticker, config.MAX_OPEN_POSITIONS)
        return False

    cash = storage.get_cash()
    price = price_feed.get_last_price(ticker)

    allocation = min(cash, config.MAX_POSITION_VALUE_INR)
    quantity = math.floor(allocation / price)
    if quantity < 1:
        log.info("Skipping %s: allocation %.2f too small at price %.2f", ticker, allocation, price)
        return False

    cost = quantity * price
    if cost > cash:
        log.info("Skipping %s: insufficient cash (%.2f < %.2f)", ticker, cash, cost)
        return False

    storage.open_position(ticker, quantity, price)
    storage.set_cash(cash - cost)
    storage.record_trade(ticker, "BUY", quantity, price, f"{config.CONFIRMATION_CHANNEL}={config.BUY_RATING_LABEL}")

    log.info(
        "BUY %s x%d @ %.2f (no target, trailing stop -%.0f%% from peak)",
        ticker, quantity, price, config.TRAILING_STOP_PCT * 100,
    )
    return True


def _sell(position, price, reason):
    ticker = position["ticker"]
    quantity = position["quantity"]
    proceeds = quantity * price

    storage.close_position(ticker)
    storage.set_cash(storage.get_cash() + proceeds)
    storage.record_trade(ticker, "SELL", quantity, price, reason)

    log.info("SELL %s x%d @ %.2f (%s)", ticker, quantity, price, reason)


def _past_market_close(now_ist):
    close_minutes = config.MARKET_CLOSE_IST_HOUR * 60 + config.MARKET_CLOSE_IST_MINUTE
    return now_ist.hour * 60 + now_ist.minute >= close_minutes


def check_exits():
    """Every open position is intraday-only with no upper target: sell on a stop that
    trails below the peak price since entry, or a forced close near market close —
    whichever comes first. A position somehow still open past its entry day (e.g. the
    bot was down at close) is force-closed immediately as a safety net."""
    now = datetime.now(timezone.utc)
    now_ist = now.astimezone(storage.IST)

    for position in storage.get_open_positions():
        ticker = position["ticker"]
        try:
            price = price_feed.get_last_price(ticker)
        except Exception:
            log.exception("Could not fetch price for %s while checking exits", ticker)
            continue

        if not storage._same_ist_day(position["entry_time"], now):
            _sell(position, price, "stale-position-force-close")
            continue

        peak_price = max(position["peak_price"], price)
        trailing_stop = peak_price * (1 - config.TRAILING_STOP_PCT)
        if price <= trailing_stop:
            _sell(position, price, "trailing-stop")
            continue

        if _past_market_close(now_ist):
            _sell(position, price, "end-of-day")
            continue

        if peak_price != position["peak_price"]:
            storage.update_peak_price(ticker, peak_price)
