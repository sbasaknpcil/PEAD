import asyncio
import csv
import json
import logging
import math
from datetime import datetime, timedelta, timezone

from telethon import TelegramClient

import config
import earnings_pulse_listener
import price_feed
import rules
import storage
import telegram_listener
import vision_parser

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("backtest")


def _load_signal_cache():
    try:
        with open(config.BACKTEST_SIGNAL_CACHE_PATH) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def _save_signal_cache(cache):
    with open(config.BACKTEST_SIGNAL_CACHE_PATH, "w") as f:
        json.dump(cache, f, indent=2, default=str)


async def fetch_signals(client):
    """Return [{message_id, date (aware datetime), card}] for historical PEAD cards,
    oldest first. Extracted cards are cached to disk by message id so re-running the
    backtest doesn't re-call Gemini for cards already seen."""
    cache = _load_signal_cache()
    group_entity = await telegram_listener.resolve_group(client)
    cutoff = datetime.now(timezone.utc) - timedelta(days=config.BACKTEST_LOOKBACK_DAYS)

    signals = []
    async for message in client.iter_messages(
        group_entity,
        reply_to=config.TELEGRAM_PEAD_TOPIC_ID,
        offset_date=cutoff,
        reverse=True,
    ):
        if not message.photo:
            continue
        if not await telegram_listener.is_from_signal_source(client, message):
            continue

        key = str(message.id)
        if key in cache:
            card = cache[key]
        else:
            try:
                image_path = await client.download_media(
                    message, file=str(config.IMAGE_CACHE_DIR) + "/"
                )
                card = vision_parser.extract_card(image_path)
                cache[key] = card
                _save_signal_cache(cache)
                log.info("Extracted %s (msg %s, %s)", card.get("nse_ticker"), key, message.date)
            except Exception:
                log.exception("Failed to extract card for message %s", key)
                continue

        has_identifier = card.get("nse_ticker") or card.get("company_name")
        if has_identifier and card.get("pead_score") is not None:
            signals.append({"message_id": key, "date": message.date, "card": card})

    signals.sort(key=lambda s: s["date"])
    return signals


async def fetch_excellent_signals(client):
    """Return {(bare_ticker, ist_date)} for every historical 'excellent'-rated message
    on the confirmation channel, within the same lookback window as fetch_signals."""
    channel_entity = await earnings_pulse_listener.resolve_channel(client)
    cutoff = datetime.now(timezone.utc) - timedelta(days=config.BACKTEST_LOOKBACK_DAYS)

    excellent = set()
    async for message in client.iter_messages(channel_entity, offset_date=cutoff, reverse=True):
        parsed = earnings_pulse_listener._parse_rating(message.text)
        if parsed is None:
            continue
        ticker, rating = parsed
        if rating.lower() == config.CONFIRMATION_RATING_LABEL.lower():
            excellent.add((ticker.upper(), message.date.astimezone(storage.IST).date()))

    log.info("Found %d historical 'excellent' signals", len(excellent))
    return excellent


def _entry_date_and_row(history, signal_date):
    """First trading day strictly after signal_date, and that day's OHLC row."""
    signal_day = signal_date.date()
    for idx in history.index:
        if idx.date() > signal_day:
            return idx, history.loc[idx]
    return None, None


def _resolve_same_day(entry_price, day_row):
    """Apply the intraday exit rules to a single day's OHLC and return (exit_price,
    reason). Every position closes the same day it opens — same-day target first,
    then a stop trailing below the day's peak, else forced-close at day's close."""
    target_price = entry_price * (1 + config.SAME_DAY_TARGET_PCT)
    if day_row["High"] >= target_price:
        return target_price, "same-day-target"

    peak = max(entry_price, float(day_row["High"]))
    trailing_stop = peak * (1 - config.TRAILING_STOP_PCT)
    if day_row["Low"] <= trailing_stop:
        return trailing_stop, "trailing-stop"

    return float(day_row["Close"]), "end-of-day"


def simulate(signals, excellent_signals=None):
    excellent_signals = excellent_signals if excellent_signals is not None else set()
    cash = config.STARTING_CAPITAL_INR
    trades = []
    histories = {}  # ticker -> DataFrame, fetched once per ticker
    today = datetime.now(timezone.utc)

    def get_history(ticker):
        if ticker not in histories:
            # Extra lookback so a 200-day moving average can be computed as of each
            # signal's entry date, not just from the start of the backtest window.
            start = today - timedelta(days=config.BACKTEST_LOOKBACK_DAYS + 290)
            histories[ticker] = price_feed.get_history(ticker, start, today + timedelta(days=1))
        return histories[ticker]

    def dma_200_before(ticker, entry_date):
        history = histories[ticker]
        prior_closes = history.loc[history.index < entry_date, "Close"]
        if len(prior_closes) < price_feed.DMA_WINDOW_DAYS:
            return None
        return float(prior_closes.tail(price_feed.DMA_WINDOW_DAYS).mean())

    def rsi_before(ticker, entry_date):
        history = histories[ticker]
        prior_closes = history.loc[history.index < entry_date, "Close"]
        return price_feed.rsi_from_closes(prior_closes, config.RSI_PERIOD)

    # Resolve a candidate entry (date, price) for every signal that passes the buy rule.
    candidates = []
    for signal in signals:
        card = signal["card"]
        should_buy, reason = rules.decide_buy(card)
        log.info("%s (%s): %s", card.get("nse_ticker") or card.get("company_name"), signal["date"].date(), reason)
        if not should_buy:
            continue

        ticker = card.get("nse_ticker") or price_feed.resolve_symbol(card.get("company_name"))
        if not ticker:
            log.warning("Could not resolve a ticker for %s, skipping signal", card.get("company_name"))
            continue

        bare_ticker = storage._bare_ticker(ticker)
        signal_ist_date = signal["date"].astimezone(storage.IST).date()
        if (bare_ticker, signal_ist_date) not in excellent_signals:
            log.info(
                "%s (%s): skipped, not confirmed by an 'excellent' signal the same day",
                ticker, signal_ist_date,
            )
            continue

        history = get_history(ticker)
        if history.empty:
            log.warning("No price history for %s, skipping signal", ticker)
            continue

        entry_date, entry_row = _entry_date_and_row(history, signal["date"])
        if entry_date is None:
            continue  # signal too recent to have a next trading day yet
        entry_price = float(entry_row["Open"])

        if config.REQUIRE_ABOVE_200DMA:
            dma_200 = dma_200_before(ticker, entry_date)
            if not rules.is_above_200dma(entry_price, dma_200):
                log.info(
                    "%s (%s): skipped, price %.2f not above 200DMA %s",
                    ticker, entry_date.date(), entry_price, dma_200,
                )
                continue

        rsi = rsi_before(ticker, entry_date)
        if not rules.passes_rsi(rsi):
            log.info("%s (%s): skipped, RSI %s below minimum %s", ticker, entry_date.date(), rsi, config.RSI_MIN)
            continue

        candidates.append(
            {
                "ticker": ticker,
                "pead_score": card["pead_score"],
                "entry_date": entry_date,
                "entry_row": entry_row,
                "entry_price": entry_price,
            }
        )

    candidates.sort(key=lambda c: c["entry_date"])

    # Every position resolves the same day it opens, so the only thing that carries
    # from one entry date to the next is cash — no multi-day position tracking needed.
    open_count_by_date = {}
    for candidate in candidates:
        entry_date = candidate["entry_date"]
        ticker = candidate["ticker"]
        open_count_by_date.setdefault(entry_date, 0)

        if open_count_by_date[entry_date] >= config.MAX_OPEN_POSITIONS:
            log.info("Skip %s on %s: max open positions reached", ticker, entry_date.date())
            continue

        entry_price = candidate["entry_price"]
        allocation = min(cash, config.MAX_POSITION_VALUE_INR)
        quantity = math.floor(allocation / entry_price)
        cost = quantity * entry_price
        if quantity < 1 or cost > cash:
            log.info("Skip %s on %s: insufficient cash", ticker, entry_date.date())
            continue

        cash -= cost
        open_count_by_date[entry_date] += 1

        exit_price, reason = _resolve_same_day(entry_price, candidate["entry_row"])
        cash += quantity * exit_price

        pnl = quantity * (exit_price - entry_price)
        pnl_pct = (exit_price / entry_price - 1) * 100
        trades.append(
            {
                "ticker": ticker,
                "pead_score": candidate["pead_score"],
                "entry_date": entry_date,
                "entry_price": round(entry_price, 2),
                "exit_price": round(exit_price, 2),
                "reason": reason,
                "pnl_pct": round(pnl_pct, 2),
                "pnl_amount": round(pnl, 2),
            }
        )

    return trades, cash


def report(trades, cash):
    wins = [t for t in trades if t["reason"] == "same-day-target"]
    losses = [t for t in trades if t["reason"] == "trailing-stop"]
    flat = [t for t in trades if t["reason"] == "end-of-day"]
    total_pnl = sum(t["pnl_amount"] for t in trades)

    print("\n=== Backtest Summary ===")
    print(f"Trades: {len(trades)}  (target hit: {len(wins)}, trailing-stop: {len(losses)}, end-of-day: {len(flat)})")
    if trades:
        win_rate = len(wins) / len(trades) * 100
        avg_return = sum(t["pnl_pct"] for t in trades) / len(trades)
        print(f"Win rate (target hit): {win_rate:.1f}%")
        print(f"Average return per trade: {avg_return:.2f}%")
    print(f"Total P&L: Rs {total_pnl:,.2f}")
    print(f"Starting capital: Rs {config.STARTING_CAPITAL_INR:,.2f}")
    print(f"Ending cash: Rs {cash:,.2f}")
    print(f"Return on starting capital: {(cash / config.STARTING_CAPITAL_INR - 1) * 100:.2f}%")

    if trades:
        with open(config.BACKTEST_TRADES_CSV_PATH, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(trades[0].keys()))
            writer.writeheader()
            writer.writerows(trades)
        print(f"\nTrade detail written to {config.BACKTEST_TRADES_CSV_PATH}")


async def main():
    client = TelegramClient(config.TELEGRAM_SESSION_NAME, config.TELEGRAM_API_ID, config.TELEGRAM_API_HASH)
    await client.start()

    log.info("Fetching PEAD cards from the last %d days...", config.BACKTEST_LOOKBACK_DAYS)
    signals = await fetch_signals(client)
    log.info("Found %d historical cards", len(signals))

    excellent_signals = await fetch_excellent_signals(client)

    trades, cash = simulate(signals, excellent_signals)
    report(trades, cash)

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
