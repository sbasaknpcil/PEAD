import logging
import math
from datetime import datetime, timezone

import config
import price_feed
import rules
import storage

log = logging.getLogger("portfolio")


def buy(ticker, pead_score):
    if storage.get_position(ticker):
        log.info("Skipping %s: position already open", ticker)
        return False

    open_positions = storage.get_open_positions()
    if len(open_positions) >= config.MAX_OPEN_POSITIONS:
        log.info("Skipping %s: max open positions (%d) reached", ticker, config.MAX_OPEN_POSITIONS)
        return False

    cash = storage.get_cash()
    price = price_feed.get_last_price(ticker)

    if config.REQUIRE_ABOVE_200DMA:
        dma_200 = price_feed.get_200dma(ticker)
        if not rules.is_above_200dma(price, dma_200):
            log.info("Skipping %s: price %.2f not above 200DMA %s", ticker, price, dma_200)
            return False

    rsi = price_feed.get_rsi(ticker, period=config.RSI_PERIOD)
    if not rules.passes_rsi(rsi):
        log.info("Skipping %s: RSI %s below minimum %s", ticker, rsi, config.RSI_MIN)
        return False

    allocation = min(cash, config.MAX_POSITION_VALUE_INR)
    quantity = math.floor(allocation / price)
    if quantity < 1:
        log.info("Skipping %s: allocation %.2f too small at price %.2f", ticker, allocation, price)
        return False

    cost = quantity * price
    if cost > cash:
        log.info("Skipping %s: insufficient cash (%.2f < %.2f)", ticker, cash, cost)
        return False

    storage.open_position(ticker, quantity, price, pead_score)
    storage.set_cash(cash - cost)
    storage.record_trade(ticker, "BUY", quantity, price, f"pead_score={pead_score}")

    log.info(
        "BUY %s x%d @ %.2f (same-day target +%.0f%%, trailing stop -%.0f%% from peak)",
        ticker, quantity, price, config.SAME_DAY_TARGET_PCT * 100, config.TRAILING_STOP_PCT * 100,
    )
    return True


def handle_pead_signal(ticker, pead_score):
    """A PEAD card passed the score/financials/200DMA checks. Buy immediately if the
    other channel already flagged this ticker 'excellent' today; otherwise wait."""
    if storage.has_excellent_today(ticker):
        log.info("%s: PEAD signal confirmed by existing 'excellent' signal, buying", ticker)
        storage.remove_pending_pead(ticker)
        buy(ticker, pead_score)
    else:
        storage.add_pending_pead(ticker, pead_score)
        log.info("%s: PEAD signal recorded, awaiting confirmation from %s", ticker, config.CONFIRMATION_CHANNEL)


def handle_excellent_signal(ticker):
    """The confirmation channel flagged this ticker 'excellent'. Buy immediately if a
    PEAD signal is already pending for it today; otherwise just record it."""
    storage.record_excellent_signal(ticker)
    pending = storage.get_pending_pead_today(ticker)
    if pending:
        log.info("%s: 'excellent' signal confirms pending PEAD signal, buying", ticker)
        storage.remove_pending_pead(ticker)
        buy(ticker, pending["pead_score"])
    else:
        log.info("%s: 'excellent' signal recorded, awaiting a PEAD signal", ticker)


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
    """Every open position is intraday-only: sell on a same-day target, a stop that
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

        if price >= position["entry_price"] * (1 + config.SAME_DAY_TARGET_PCT):
            _sell(position, price, "same-day-target")
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
