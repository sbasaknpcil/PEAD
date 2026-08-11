"""One-off analysis: does the strategy do worse on days the broader market is down,
and would skipping trades on days Nifty is down >=1% improve returns? Reuses
backtest.py's exact simulation (same immediate-buy/2%-trailing-stop/no-target rules)
over a longer lookback, then joins each trading day's P&L against Nifty 50's daily
return. Precision note: >6 days forces backtest.py's 5m-bar fallback (1m only
covers ~8 days) - see backtest.py's simulate_intraday docstring.
"""
import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import pandas as pd
import yfinance as yf
from telethon import TelegramClient

import backtest
import config
import storage

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("market_correlation")

LOOKBACK_DAYS = 30
DOWN_DAY_THRESHOLD_PCT = -1.0  # "market is 1% down"


def fetch_nifty_daily_returns(lookback_days):
    """{date (IST calendar date): pct_change_close_to_prev_close} for NIFTY 50."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=lookback_days + 10)  # buffer for the first day's prior-close
    df = yf.download("^NSEI", start=start, end=end, progress=False, auto_adjust=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    closes = df["Close"]
    returns = {}
    prev_close = None
    for date, close in closes.items():
        if prev_close is not None:
            returns[date.date()] = (float(close) / float(prev_close) - 1) * 100
        prev_close = close
    return returns


def main():
    client = TelegramClient(config.TELEGRAM_SESSION_NAME, config.TELEGRAM_API_ID, config.TELEGRAM_API_HASH)

    async def _run():
        await client.start()
        log.info("Fetching '%s' ratings from the last %d days...", config.BUY_RATING_LABEL, LOOKBACK_DAYS)
        ratings = await backtest.fetch_excellent_ratings(client, LOOKBACK_DAYS)
        await client.disconnect()
        return ratings

    ratings = asyncio.run(_run())
    trades, ending_cash = backtest.simulate_intraday(ratings, LOOKBACK_DAYS)

    print(f"\n{len(trades)} trades over the last {LOOKBACK_DAYS} days\n")
    print(f"{'Ticker':<14}{'Signal (IST)':>20}{'Entry (IST)':>20}{'Entry Px':>10}{'Exit (IST)':>20}{'Exit Px':>10}  {'Reason':<18}{'PnL%':>7}")
    for t in trades:
        signal_ist = t["signal_time"].astimezone(storage.IST)
        entry_ist = t["entry_time"].astimezone(storage.IST)
        exit_ist = t["exit_time"].astimezone(storage.IST)
        print(
            f"{t['ticker']:<14}{signal_ist.strftime('%Y-%m-%d %H:%M:%S'):>20}"
            f"{entry_ist.strftime('%Y-%m-%d %H:%M:%S'):>20}{t['entry_price']:>10}"
            f"{exit_ist.strftime('%Y-%m-%d %H:%M:%S'):>20}{t['exit_price']:>10}  "
            f"{t['reason']:<18}{t['pnl_pct']:>6}%"
        )

    nifty_returns = fetch_nifty_daily_returns(LOOKBACK_DAYS)

    by_day = defaultdict(list)
    for t in trades:
        day = t["entry_time"].astimezone(storage.IST).date()
        by_day[day].append(t)

    print(f"\n{'Date':<12}{'Nifty %':>9}{'Trades':>8}{'Wins':>6}{'Day PnL':>14}{'Down>=1%?':>11}")
    total_all = 0.0
    total_excl_down_days = 0.0
    excluded_trades = 0
    for day in sorted(by_day):
        day_trades = by_day[day]
        day_pnl = sum(t["pnl_amount"] for t in day_trades)
        wins = sum(1 for t in day_trades if t["pnl_amount"] > 0)
        nifty_pct = nifty_returns.get(day)
        is_down_day = nifty_pct is not None and nifty_pct <= DOWN_DAY_THRESHOLD_PCT
        total_all += day_pnl
        if not is_down_day:
            total_excl_down_days += day_pnl
        else:
            excluded_trades += len(day_trades)
        nifty_str = f"{nifty_pct:+.2f}%" if nifty_pct is not None else "n/a"
        print(
            f"{day.isoformat():<12}{nifty_str:>9}{len(day_trades):>8}{wins:>6}"
            f"{'Rs ' + format(day_pnl, ',.2f'):>14}{('YES' if is_down_day else ''):>11}"
        )

    print(f"\n=== Market-sentiment correlation ===")
    print(f"Actual P&L (all {len(trades)} trades): Rs {total_all:,.2f}")
    print(
        f"P&L excluding the {excluded_trades} trades on Nifty-down->={abs(DOWN_DAY_THRESHOLD_PCT)}% days: "
        f"Rs {total_excl_down_days:,.2f}"
    )
    print(f"Difference: Rs {total_excl_down_days - total_all:,.2f}")


if __name__ == "__main__":
    main()
