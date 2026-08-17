import asyncio
import logging
import math
from datetime import datetime, timezone

import config
import price_feed
import storage

log = logging.getLogger("portfolio")

# Set once at startup (main.py) so buy()/_sell() can push a Telegram notification
# the moment a trade is recorded. None (e.g. in tests/backtest contexts) just
# means notifications are silently skipped.
_notify_client = None


def set_notify_client(client):
    global _notify_client
    _notify_client = client


def _notify(text):
    if _notify_client is None:
        return
    try:
        asyncio.create_task(_notify_client.send_message("me", text))
    except Exception:
        log.exception("Failed to send Telegram trade notification")


def _within_market_hours(now_ist):
    open_minutes = config.MARKET_OPEN_IST_HOUR * 60 + config.MARKET_OPEN_IST_MINUTE
    close_minutes = config.MARKET_CLOSE_IST_HOUR * 60 + config.MARKET_CLOSE_IST_MINUTE
    now_minutes = now_ist.hour * 60 + now_ist.minute
    return open_minutes <= now_minutes < close_minutes


def buy(variant, label, trailing_stop_pct, ticker):
    """Buy immediately on an earnings_pulse rating alone - no PEAD score, 200DMA,
    RSI, or market-cap check. Every position exits the same day it
    opens (see check_exits), so a rating outside market hours has no trading window
    left and is skipped rather than bought. Each variant is a fully independent
    paper portfolio - its own cash, its own positions - so the same signal can open
    a position in one variant while being skipped or already-open in another."""
    now_ist = datetime.now(timezone.utc).astimezone(storage.IST)
    if not _within_market_hours(now_ist):
        log.info(
            "[%s] Skipping %s: rated outside market hours (%s IST) - no same-day window left",
            variant, ticker, now_ist.strftime("%H:%M"),
        )
        return False

    if storage.get_position(variant, ticker):
        log.info("[%s] Skipping %s: position already open", variant, ticker)
        return False

    open_positions = storage.get_open_positions(variant)
    if len(open_positions) >= config.MAX_OPEN_POSITIONS:
        log.info("[%s] Skipping %s: max open positions (%d) reached", variant, ticker, config.MAX_OPEN_POSITIONS)
        return False

    cash = storage.get_cash(variant)
    try:
        price = price_feed.get_last_price(ticker)
    except Exception:
        # yfinance's response occasionally omits a key (e.g. KeyError:
        # 'currentTradingPeriod') on a transient/malformed API response - this
        # used to crash the whole watcher run, silently failing every other
        # tier/ticker in the same cycle too. Skip just this one buy instead.
        log.exception("[%s] Could not fetch price for %s, skipping this buy", variant, ticker)
        return False

    allocation = min(cash, config.MAX_POSITION_VALUE_INR)
    quantity = math.floor(allocation / price)
    if quantity < 1:
        log.info("[%s] Skipping %s: allocation %.2f too small at price %.2f", variant, ticker, allocation, price)
        return False

    cost = quantity * price
    if cost > cash:
        log.info("[%s] Skipping %s: insufficient cash (%.2f < %.2f)", variant, ticker, cash, cost)
        return False

    storage.open_position(variant, ticker, quantity, price)
    storage.set_cash(variant, cash - cost)
    storage.record_trade(
        variant, ticker, "BUY", quantity, price, f"{config.CONFIRMATION_CHANNEL}={label}"
    )

    log.info(
        "[%s] BUY %s x%d @ %.2f (no target, trailing stop -%.0f%% from peak)",
        variant, ticker, quantity, price, trailing_stop_pct * 100,
    )
    _notify(
        f"\U0001f7e2 BUY [{label}]\n{ticker}  x{quantity} @ ₹{price:.2f}\n"
        f"Cost: ₹{cost:,.2f}  |  Cash left: ₹{cash - cost:,.2f}"
    )
    return True


def buy_all_variants(ticker):
    """Fan the same signal out to every configured portfolio variant."""
    for v in config.STRATEGY_VARIANTS:
        buy(v["variant"], v["label"], v["trailing_stop_pct"], ticker)


def buy_for_tier(ticker, rating):
    """Buys into the one variant whose configured rating matches, e.g. an
    earnings_pulse "Great" message only opens a position in the "great"
    portfolio — Excellent/Great/Good stay fully separate books so their
    performance is directly comparable. No match (a rating not configured as
    a variant, e.g. "Weak"/"OK") is a silent no-op."""
    for v in config.STRATEGY_VARIANTS:
        if v.get("rating", "").lower() == rating.lower():
            buy(v["variant"], v["label"], v["trailing_stop_pct"], ticker)
            return


def _sell(variant, label, position, price, reason):
    ticker = position["ticker"]
    quantity = position["quantity"]
    proceeds = quantity * price
    entry_price = position["entry_price"]
    pnl = proceeds - quantity * entry_price
    pnl_pct = (price / entry_price - 1) * 100

    storage.close_position(variant, ticker)
    storage.set_cash(variant, storage.get_cash(variant) + proceeds)
    storage.record_trade(variant, ticker, "SELL", quantity, price, reason)

    log.info("[%s] SELL %s x%d @ %.2f (%s)", variant, ticker, quantity, price, reason)
    emoji = "\U0001f534" if pnl < 0 else "\U0001f7e2"
    _notify(
        f"{emoji} SELL [{label}]\n{ticker}  x{quantity} @ ₹{price:.2f}  ({reason})\n"
        f"P&L: ₹{pnl:,.2f}  ({pnl_pct:+.2f}%)"
    )


def _past_market_close(now_ist):
    close_minutes = config.MARKET_CLOSE_IST_HOUR * 60 + config.MARKET_CLOSE_IST_MINUTE
    return now_ist.hour * 60 + now_ist.minute >= close_minutes


def check_exits(variant, label, trailing_stop_pct):
    """Every open position is intraday-only with no upper target: sell on a stop that
    trails below the peak price since entry, or a forced close near market close —
    whichever comes first. A position somehow still open past its entry day (e.g. the
    bot was down at close) is force-closed immediately as a safety net."""
    now = datetime.now(timezone.utc)
    now_ist = now.astimezone(storage.IST)

    for position in storage.get_open_positions(variant):
        ticker = position["ticker"]
        try:
            price = price_feed.get_last_price(ticker)
        except Exception:
            log.exception("[%s] Could not fetch price for %s while checking exits", variant, ticker)
            continue

        if not storage._same_ist_day(position["entry_time"], now):
            _sell(variant, label, position, price, "stale-position-force-close")
            continue

        peak_price = max(position["peak_price"], price)
        trailing_stop = peak_price * (1 - trailing_stop_pct)
        if price <= trailing_stop:
            _sell(variant, label, position, price, "trailing-stop")
            continue

        if _past_market_close(now_ist):
            _sell(variant, label, position, price, "end-of-day")
            continue

        if peak_price != position["peak_price"]:
            storage.update_peak_price(variant, ticker, peak_price)


def check_exits_all_variants():
    for v in config.STRATEGY_VARIANTS:
        check_exits(v["variant"], v["label"], v["trailing_stop_pct"])
