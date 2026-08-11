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


def _resolve_trade(signal_time, intraday_history, stop_pct, target_pct):
    """Simulate one immediate-buy trade from intraday bars on the signal's trading
    day. Entry is the first bar at/after the signal time (approximating an immediate
    market order); exit is target_pct above entry if hit first, else a trailing stop
    stop_pct below the peak High seen since entry, else the day's last bar close.
    target_pct=None means no upper target (the live default). Also reports peak_pct
    - the highest intraday gain actually reached above entry, independent of which
    exit fired - so it's possible to tell whether a given target level ever had a
    chance to trigger versus simply never being reached by any trade."""
    day = intraday_history[intraday_history.index.date == signal_time.date()]
    day = day[day.index >= signal_time]
    if day.empty:
        return None

    entry_time = day.index[0]
    entry_price = float(day.iloc[0]["Open"])
    target_price = entry_price * (1 + target_pct) if target_pct else None

    peak = entry_price
    for ts, row in day.iloc[1:].iterrows():
        peak = max(peak, float(row["High"]))
        peak_pct = round((peak / entry_price - 1) * 100, 2)
        if target_price is not None and row["High"] >= target_price:
            return {
                "entry_time": entry_time, "entry_price": entry_price,
                "exit_time": ts, "exit_price": target_price, "reason": "target",
                "peak_pct": peak_pct,
            }
        trailing_stop = peak * (1 - stop_pct)
        if row["Low"] <= trailing_stop:
            return {
                "entry_time": entry_time, "entry_price": entry_price,
                "exit_time": ts, "exit_price": trailing_stop, "reason": "trailing-stop",
                "peak_pct": peak_pct,
            }

    return {
        "entry_time": entry_time, "entry_price": entry_price,
        "exit_time": day.index[-1], "exit_price": float(day.iloc[-1]["Close"]), "reason": "end-of-day",
        "peak_pct": round((peak / entry_price - 1) * 100, 2),
    }


def fetch_candidates(ratings, lookback_days):
    """Market-hours-filtered ratings with their intraday bars fetched once each, so
    the same candidate set can be resolved under any exit-parameter combination
    without re-fetching (used by both the default report and the regression sweep).
    Uses 1m bars (the finest Yahoo provides for NSE intraday data) when the window
    fits its ~8-day cap, else falls back to 5m (~60-day retention) so longer
    lookbacks still work, at coarser fill precision."""
    histories = {}
    today = datetime.now(timezone.utc)
    interval = "1m" if lookback_days <= 6 else "5m"
    # Yahoo hard-caps 1m history at 8 days per request, 5m at ~60; the end boundary
    # already reaches 1 day past "today", so the start side stays under that cap.
    fetch_start = today - timedelta(days=min(lookback_days, 6 if interval == "1m" else 58))

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
                history = price_feed.get_intraday_history(ticker, fetch_start, today + timedelta(days=1), interval=interval)
            except Exception:
                log.warning("Could not fetch intraday history for %s", ticker)
                history = None
            histories[ticker] = history
        history = histories[ticker]
        if history is None or history.empty:
            log.info("%s: no intraday data available, skipping", ticker)
            continue

        signal_local = rating["date"].astimezone(history.index.tz)
        candidates.append({"ticker": ticker, "signal_time": rating["date"], "signal_local": signal_local, "history": history})

    return candidates


def simulate_candidates(candidates, stop_pct, target_pct):
    """Resolve every candidate under the given exit params, then run a chronological
    event simulation so overlapping positions correctly compete for cash and
    MAX_OPEN_POSITIONS slots, same as the live bot would encounter them."""
    resolved = []
    for c in candidates:
        trade = _resolve_trade(c["signal_local"], c["history"], stop_pct, target_pct)
        if trade is None:
            continue
        resolved.append({"ticker": c["ticker"], "signal_time": c["signal_time"], **trade})

    events = []
    for idx, c in enumerate(resolved):
        events.append((c["entry_time"], 1, idx))  # exits (0) before entries (1) at a tie
        events.append((c["exit_time"], 0, idx))
    events.sort(key=lambda e: (e[0], e[1]))

    cash = config.STARTING_CAPITAL_INR
    trades = []
    open_count = 0
    opened = [False] * len(resolved)
    entry_cost = [0.0] * len(resolved)
    entry_qty = [0] * len(resolved)

    for ts, _, idx in events:
        c = resolved[idx]
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
                    "signal_time": c["signal_time"],
                    "entry_time": c["entry_time"],
                    "entry_price": round(c["entry_price"], 2),
                    "exit_time": c["exit_time"],
                    "exit_price": round(c["exit_price"], 2),
                    "quantity": quantity,
                    "reason": c["reason"],
                    "pnl_pct": round(pnl_pct, 2),
                    "pnl_amount": round(pnl, 2),
                    "peak_pct": c["peak_pct"],
                }
            )

    trades.sort(key=lambda t: t["entry_time"])
    return trades, cash


def simulate_intraday(ratings, lookback_days):
    """Convenience wrapper: fetch candidates and simulate at the live default
    (config.TRAILING_STOP_PCT, no target). Kept for existing callers (e.g.
    pead_market_correlation.py) that don't need the regression sweep below."""
    candidates = fetch_candidates(ratings, lookback_days)
    return simulate_candidates(candidates, config.TRAILING_STOP_PCT, None)


def regression_grid(candidates):
    """Sweep stop-loss % x target % (None = no target) against the same candidate
    set, return every combination's total P&L so the best one can be picked."""
    stop_options = [0.01, 0.015, 0.02, 0.025, 0.03, 0.04, 0.05]
    target_options = [None, 0.02, 0.03, 0.05, 0.07, 0.10, 0.15]

    grid = []
    for target_pct in target_options:
        for stop_pct in stop_options:
            trades, cash = simulate_candidates(candidates, stop_pct, target_pct)
            total_pnl = cash - config.STARTING_CAPITAL_INR
            wins = sum(1 for t in trades if t["pnl_amount"] > 0)
            grid.append(
                {
                    "stop_pct": stop_pct,
                    "target_pct": target_pct,
                    "trades": len(trades),
                    "wins": wins,
                    "win_rate": (wins / len(trades) * 100) if trades else 0.0,
                    "total_pnl": round(total_pnl, 2),
                    "return_pct": round(total_pnl / config.STARTING_CAPITAL_INR * 100, 3),
                }
            )
    grid.sort(key=lambda g: -g["total_pnl"])
    return grid


def report(trades, cash):
    stopped = [t for t in trades if t["reason"] == "trailing-stop"]
    flat = [t for t in trades if t["reason"] == "end-of-day"]
    targeted = [t for t in trades if t["reason"] == "target"]
    total_pnl = sum(t["pnl_amount"] for t in trades)
    winners = [t for t in trades if t["pnl_amount"] > 0]

    print("\n=== Backtest Summary ===")
    print(
        f"Trades: {len(trades)}  (trailing-stop: {len(stopped)}, "
        f"end-of-day: {len(flat)}, target: {len(targeted)})"
    )
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
        peaks = [t["peak_pct"] for t in trades]
        print(
            f"\nPeak intraday gain reached (independent of exit reason): "
            f"max {max(peaks):.2f}%, median {sorted(peaks)[len(peaks)//2]:.2f}%"
        )
        for threshold in (5, 7, 10, 15):
            n = sum(1 for p in peaks if p >= threshold)
            print(f"  Trades that ever reached >={threshold}%: {n}/{len(trades)}")

        print(
            f"\n{'Ticker':<14}{'Signal (IST)':>18}{'Entry (IST)':>18}{'Lag':>7}"
            f"{'Exit (IST)':>18}  {'Reason':<14}{'PnL%':>7}{'Peak%':>7}"
        )
        for t in trades:
            signal_ist = t["signal_time"].astimezone(storage.IST)
            entry_ist = t["entry_time"].astimezone(storage.IST)
            exit_ist = t["exit_time"].astimezone(storage.IST)
            lag_seconds = (entry_ist - signal_ist).total_seconds()
            lag_str = f"{lag_seconds/60:.1f}m" if lag_seconds >= 60 else f"{lag_seconds:.0f}s"
            print(
                f"{t['ticker']:<14}{signal_ist.strftime('%m-%d %H:%M:%S'):>18}"
                f"{entry_ist.strftime('%m-%d %H:%M:%S'):>18}{lag_str:>7}"
                f"{exit_ist.strftime('%m-%d %H:%M:%S'):>18}  "
                f"{t['reason']:<14}{t['pnl_pct']:>6}%{t['peak_pct']:>6}%"
            )
        csv_rows = [
            {**t, "signal_time": t["signal_time"].astimezone(storage.IST).isoformat(),
             "entry_time": t["entry_time"].astimezone(storage.IST).isoformat(),
             "exit_time": t["exit_time"].astimezone(storage.IST).isoformat()}
            for t in trades
        ]
        with open(config.BACKTEST_TRADES_CSV_PATH, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(csv_rows[0].keys()))
            writer.writeheader()
            writer.writerows(csv_rows)
        print(f"\nTrade detail written to {config.BACKTEST_TRADES_CSV_PATH}")


def report_regression(grid, top_n=15):
    print(f"\n=== Regression: target% x stop-loss% grid ===")
    print(f"{'Target':>8}{'Stop':>7}{'Trades':>8}{'Wins':>6}{'WinRate':>9}{'Total PnL':>14}{'Return%':>9}")
    for g in grid[:top_n]:
        target_str = f"{g['target_pct']*100:.0f}%" if g["target_pct"] else "none"
        print(
            f"{target_str:>8}{g['stop_pct']*100:>6.1f}%{g['trades']:>8}{g['wins']:>6}"
            f"{g['win_rate']:>8.1f}%{'Rs '+format(g['total_pnl'], ',.2f'):>14}{g['return_pct']:>8.2f}%"
        )
    if grid:
        best = grid[0]
        target_str = f"{best['target_pct']*100:.0f}%" if best["target_pct"] else "none"
        print(
            f"\nBest combination: target={target_str}, stop={best['stop_pct']*100:.1f}% "
            f"-> Rs {best['total_pnl']:,.2f} ({best['return_pct']:.2f}%)"
        )


async def main():
    import sys

    client = TelegramClient(config.TELEGRAM_SESSION_NAME, config.TELEGRAM_API_ID, config.TELEGRAM_API_HASH)
    await client.start()

    lookback_days = int(sys.argv[1]) if len(sys.argv) > 1 else config.INTRADAY_BACKTEST_LOOKBACK_DAYS
    log.info("Fetching '%s' ratings from the last %d days...", config.BUY_RATING_LABEL, lookback_days)
    ratings = await fetch_excellent_ratings(client, lookback_days)

    candidates = fetch_candidates(ratings, lookback_days)
    log.info("%d candidates with a market-hours signal and intraday data", len(candidates))

    trades, cash = simulate_candidates(candidates, config.TRAILING_STOP_PCT, None)
    report(trades, cash)

    grid = regression_grid(candidates)
    report_regression(grid)

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
