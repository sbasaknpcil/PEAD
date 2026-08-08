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


async def fetch_excellent_ratings(client, lookback_days):
    """Return [{ticker, date (aware datetime)}] for every BUY_RATING_LABEL-rated
    message on the confirmation channel within lookback_days, oldest first."""
    channel_entity = await earnings_pulse_listener.resolve_channel(client)
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)

    ratings = []
    async for message in client.iter_messages(channel_entity, offset_date=cutoff, reverse=True):
        parsed = earnings_pulse_listener._parse_rating(message.text)
        if parsed is None:
            continue
        ticker, rating = parsed
        if rating.lower() == config.BUY_RATING_LABEL.lower():
            ratings.append({"ticker": ticker.upper(), "date": message.date})

    ratings.sort(key=lambda r: r["date"])
    log.info("Found %d '%s' ratings in the last %d days", len(ratings), config.BUY_RATING_LABEL, lookback_days)
    return ratings


def _within_market_hours(date_ist):
    open_minutes = config.MARKET_OPEN_IST_HOUR * 60 + config.MARKET_OPEN_IST_MINUTE
    close_minutes = config.MARKET_CLOSE_IST_HOUR * 60 + config.MARKET_CLOSE_IST_MINUTE
    minutes = date_ist.hour * 60 + date_ist.minute
    return open_minutes <= minutes < close_minutes


def _resolve_intraday_trade(signal_time, intraday_history):
    """Simulate one immediate-buy/trailing-stop trade from 5m bars on the signal's
    trading day. Entry is the first bar at/after the signal time (approximating an
    immediate market order); exit is a 2% trailing stop off the peak High seen since
    entry, or the day's last bar close if never triggered — no upper target."""
    day = intraday_history[intraday_history.index.date == signal_time.date()]
    day = day[day.index >= signal_time]
    if day.empty:
        return None

    entry_time = day.index[0]
    entry_price = float(day.iloc[0]["Open"])

    peak = entry_price
    for ts, row in day.iloc[1:].iterrows():
        peak = max(peak, float(row["High"]))
        trailing_stop = peak * (1 - config.TRAILING_STOP_PCT)
        if row["Low"] <= trailing_stop:
            return {
                "entry_time": entry_time, "entry_price": entry_price,
                "exit_time": ts, "exit_price": trailing_stop, "reason": "trailing-stop",
            }

    return {
        "entry_time": entry_time, "entry_price": entry_price,
        "exit_time": day.index[-1], "exit_price": float(day.iloc[-1]["Close"]), "reason": "end-of-day",
    }


def simulate_intraday(ratings, lookback_days):
    """Buy every rating immediately during market hours, no PEAD/200DMA/RSI/mcap
    filter, exit on a 2% trailing stop or forced end-of-day close — mirrors the live
    single-trigger strategy exactly, using 5m bars so 'immediately' is precise."""
    cash = config.STARTING_CAPITAL_INR
    histories = {}
    today = datetime.now(timezone.utc)
    fetch_start = today - timedelta(days=lookback_days + 1)

    candidates = []
    for rating in ratings:
        signal_ist = rating["date"].astimezone(storage.IST)
        if not _within_market_hours(signal_ist):
            log.info(
                "%s (%s IST): skipped, outside market hours",
                rating["ticker"], signal_ist.strftime("%Y-%m-%d %H:%M"),
            )
            continue

        ticker = price_feed.resolve_symbol(rating["ticker"]) or f"{rating['ticker']}.NS"
        if ticker not in histories:
            try:
                history = price_feed.get_intraday_history(ticker, fetch_start, today + timedelta(days=1))
            except Exception:
                log.warning("Could not fetch intraday history for %s", ticker)
                history = None
            histories[ticker] = history
        history = histories[ticker]
        if history is None or history.empty:
            log.info("%s: no intraday data available, skipping", ticker)
            continue

        signal_local = rating["date"].astimezone(history.index.tz)
        trade = _resolve_intraday_trade(signal_local, history)
        if trade is None:
            log.info("%s (%s IST): no intraday bars at/after signal time, skipping", ticker, signal_ist)
            continue

        candidates.append({"ticker": ticker, **trade})

    # Chronological event simulation so overlapping positions correctly compete for
    # cash and MAX_OPEN_POSITIONS slots, same as the live bot would encounter them.
    events = []
    for idx, c in enumerate(candidates):
        events.append((c["entry_time"], 1, idx))  # exits (0) before entries (1) at a tie
        events.append((c["exit_time"], 0, idx))
    events.sort(key=lambda e: (e[0], e[1]))

    trades = []
    open_count = 0
    opened = [False] * len(candidates)
    entry_cost = [0.0] * len(candidates)
    entry_qty = [0] * len(candidates)

    for ts, _, idx in events:
        c = candidates[idx]
        is_entry = ts == c["entry_time"]
        if is_entry:
            if open_count >= config.MAX_OPEN_POSITIONS:
                log.info("Skip %s: max open positions reached at %s", c["ticker"], ts)
                continue
            allocation = min(cash, config.MAX_POSITION_VALUE_INR)
            quantity = math.floor(allocation / c["entry_price"])
            cost = quantity * c["entry_price"]
            if quantity < 1 or cost > cash:
                log.info("Skip %s: insufficient cash at %s", c["ticker"], ts)
                continue
            cash -= cost
            open_count += 1
            opened[idx] = True
            entry_cost[idx] = cost
            entry_qty[idx] = quantity
        else:
            if not opened[idx]:
                continue
            quantity = entry_qty[idx]
            proceeds = quantity * c["exit_price"]
            cash += proceeds
            open_count -= 1
            pnl = proceeds - entry_cost[idx]
            pnl_pct = (c["exit_price"] / c["entry_price"] - 1) * 100
            trades.append(
                {
                    "ticker": c["ticker"],
                    "entry_time": c["entry_time"],
                    "entry_price": round(c["entry_price"], 2),
                    "exit_time": c["exit_time"],
                    "exit_price": round(c["exit_price"], 2),
                    "quantity": quantity,
                    "reason": c["reason"],
                    "pnl_pct": round(pnl_pct, 2),
                    "pnl_amount": round(pnl, 2),
                }
            )

    trades.sort(key=lambda t: t["entry_time"])
    return trades, cash


def report(trades, cash):
    stopped = [t for t in trades if t["reason"] == "trailing-stop"]
    flat = [t for t in trades if t["reason"] == "end-of-day"]
    total_pnl = sum(t["pnl_amount"] for t in trades)
    winners = [t for t in trades if t["pnl_amount"] > 0]

    print("\n=== Backtest Summary ===")
    print(f"Trades: {len(trades)}  (trailing-stop: {len(stopped)}, end-of-day: {len(flat)})")
    if trades:
        win_rate = len(winners) / len(trades) * 100
        avg_return = sum(t["pnl_pct"] for t in trades) / len(trades)
        print(f"Win rate (closed above entry): {win_rate:.1f}%")
        print(f"Average return per trade: {avg_return:.2f}%")
    print(f"Total P&L: Rs {total_pnl:,.2f}")
    print(f"Starting capital: Rs {config.STARTING_CAPITAL_INR:,.2f}")
    print(f"Ending cash: Rs {cash:,.2f}")
    print(f"Return on starting capital: {(cash / config.STARTING_CAPITAL_INR - 1) * 100:.2f}%")

    if trades:
        print(f"\n{'Ticker':<14}{'Entry (IST)':>20}{'Price':>10}{'Exit (IST)':>20}{'Price':>10}  {'Reason':<14}{'PnL%':>7}")
        for t in trades:
            entry_ist = t["entry_time"].astimezone(storage.IST)
            exit_ist = t["exit_time"].astimezone(storage.IST)
            print(
                f"{t['ticker']:<14}{entry_ist.strftime('%m-%d %H:%M'):>20}{t['entry_price']:>10}"
                f"{exit_ist.strftime('%m-%d %H:%M'):>20}{t['exit_price']:>10}  "
                f"{t['reason']:<14}{t['pnl_pct']:>6}%"
            )
        csv_rows = [
            {**t, "entry_time": t["entry_time"].astimezone(storage.IST).isoformat(),
             "exit_time": t["exit_time"].astimezone(storage.IST).isoformat()}
            for t in trades
        ]
        with open(config.BACKTEST_TRADES_CSV_PATH, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(csv_rows[0].keys()))
            writer.writeheader()
            writer.writerows(csv_rows)
        print(f"\nTrade detail written to {config.BACKTEST_TRADES_CSV_PATH}")


async def main():
    client = TelegramClient(config.TELEGRAM_SESSION_NAME, config.TELEGRAM_API_ID, config.TELEGRAM_API_HASH)
    await client.start()

    lookback_days = config.INTRADAY_BACKTEST_LOOKBACK_DAYS
    log.info("Fetching '%s' ratings from the last %d days...", config.BUY_RATING_LABEL, lookback_days)
    ratings = await fetch_excellent_ratings(client, lookback_days)

    trades, cash = simulate_intraday(ratings, lookback_days)
    report(trades, cash)

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
