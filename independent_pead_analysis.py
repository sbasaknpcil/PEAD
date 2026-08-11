"""Independent PEAD analysis: no Telegram, no FinanciallyFreeFFBot, no earnings_pulse.
Scans NSE 500 constituents directly against Yahoo Finance's own earnings-date/EPS-
surprise data, scores each reporter with a self-contained PEAD formula (documented
below), categorizes into Excellent/Good/Average/Poor, and simulates buying every
"Excellent" name within seconds of its result - same market-hours-only rule and
same 1-minute-bar immediate-entry mechanism as the live Telegram-driven strategy
(see backtest.py's _within_market_hours/_resolve_intraday_trade, reused here). This
file is self-contained for this session only - it does not import or reuse any of
the other in-progress session's scripts/files (backtest.py is this repo's own core
module, not another session's file).

Scoring methodology (mine, not FinanciallyFreeFFBot's - their formula isn't visible
to me, so this is an independent, from-first-principles approximation using the same
core PEAD driver academic literature uses: the earnings surprise itself):

    score = clip(50 + surprise_pct * 1.2, 0, 100)

50 is "neutral" (a name that met estimates exactly), scaled so a +25% surprise reaches
~80 and a -25% surprise falls to ~20. Categories: Excellent >=70, Good 55-69,
Average 40-54, Poor <40.

Window is capped at ~7 days, not 30: 1-minute bars (needed to enter within seconds
of the result, not just "sometime that day") are only retained by Yahoo for a
trailing ~7-8 days. Reports outside NSE market hours (09:15-15:30 IST) are skipped
entirely, matching the live buy() gate - not deferred to a later session.
"""
import logging
import math
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests
import yfinance as yf

import backtest
import config
import price_feed
import storage

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("independent_pead")

NIFTY500_URL = "https://archives.nseindia.com/content/indices/ind_nifty500list.csv"
LOOKBACK_DAYS = 7  # capped by 1-minute-bar retention, see module docstring

EXCELLENT_MIN, GOOD_MIN, AVERAGE_MIN = 70, 55, 40


def fetch_nifty500_symbols():
    """Live NSE-published constituent list - fetched fresh, not cached from any
    other session's file."""
    resp = requests.get(NIFTY500_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
    resp.raise_for_status()
    from io import StringIO
    df = pd.read_csv(StringIO(resp.text))
    return df["Symbol"].tolist()


def score_from_surprise(surprise_pct):
    score = 50 + surprise_pct * 1.2
    return max(0.0, min(100.0, score))


def category_from_score(score):
    if score >= EXCELLENT_MIN:
        return "Excellent"
    if score >= GOOD_MIN:
        return "Good"
    if score >= AVERAGE_MIN:
        return "Average"
    return "Poor"


def scan_symbols(symbols, lookback_days):
    """For each symbol, pull recent earnings dates and keep any report that fell
    inside the lookback window with a reported EPS (i.e., results are actually out,
    not just an estimate/upcoming date)."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    results = []

    for i, symbol in enumerate(symbols):
        ticker = f"{symbol}.NS"
        try:
            ed = yf.Ticker(ticker).get_earnings_dates(limit=4)
        except Exception:
            continue
        if ed is None or ed.empty:
            continue

        for report_date, row in ed.iterrows():
            report_utc = report_date.tz_convert("UTC") if report_date.tzinfo else report_date.tz_localize("UTC")
            if report_utc < cutoff or report_utc > datetime.now(timezone.utc):
                continue
            reported_eps = row.get("Reported EPS")
            surprise = row.get("Surprise(%)")
            if reported_eps is None or (isinstance(reported_eps, float) and math.isnan(reported_eps)):
                continue
            if surprise is None or (isinstance(surprise, float) and math.isnan(surprise)):
                continue

            score = score_from_surprise(float(surprise))
            results.append(
                {
                    "ticker": ticker,
                    "report_date": report_utc,
                    "eps_estimate": row.get("EPS Estimate"),
                    "reported_eps": float(reported_eps),
                    "surprise_pct": float(surprise),
                    "pead_score": round(score, 1),
                    "category": category_from_score(score),
                }
            )

        if (i + 1) % 50 == 0:
            log.warning("Scanned %d/%d symbols, %d reporters found so far", i + 1, len(symbols), len(results))

    return results


def _resolve_trade(signal_time, intraday_history, stop_pct, target_pct):
    """Same immediate-entry mechanics as backtest._resolve_intraday_trade, but
    parameterized by stop/target so the regression grid search can replay the same
    bars under different exit rules without re-fetching data. target_pct=None means
    no upper target (the current live default)."""
    day = intraday_history[intraday_history.index.date == signal_time.date()]
    day = day[day.index >= signal_time]
    if day.empty:
        return None

    entry_time = day.index[0]
    entry_price = float(day.iloc[0]["Open"])
    target_price = entry_price * (1 + target_pct) if target_pct else None

    peak = entry_price
    for ts, row in day.iloc[1:].iterrows():
        if target_price is not None and row["High"] >= target_price:
            return {
                "entry_time": entry_time, "entry_price": entry_price,
                "exit_time": ts, "exit_price": target_price, "reason": "target",
            }
        peak = max(peak, float(row["High"]))
        trailing_stop = peak * (1 - stop_pct)
        if row["Low"] <= trailing_stop:
            return {
                "entry_time": entry_time, "entry_price": entry_price,
                "exit_time": ts, "exit_price": trailing_stop, "reason": "trailing-stop",
            }

    return {
        "entry_time": entry_time, "entry_price": entry_price,
        "exit_time": day.index[-1], "exit_price": float(day.iloc[-1]["Close"]), "reason": "end-of-day",
    }


def fetch_candidate_bars(excellent_reporters, lookback_days):
    """Filter to reports that fell within NSE market hours (same gate buy() enforces
    live - anything else has no same-day trading window and is skipped, not
    deferred), fetch each ticker's 1-minute bars once, and return the list of
    candidates ready to be resolved under any exit-parameter combination."""
    today = datetime.now(timezone.utc)
    fetch_start = today - timedelta(days=min(lookback_days, 6))
    histories = {}
    candidates = []

    for r in excellent_reporters:
        report_ist = r["report_date"].astimezone(storage.IST)
        if not backtest._within_market_hours(report_ist):
            log.info(
                "%s (%s IST): skipped, result declared outside market hours",
                r["ticker"], report_ist.strftime("%Y-%m-%d %H:%M"),
            )
            continue

        ticker = r["ticker"]
        if ticker not in histories:
            try:
                histories[ticker] = price_feed.get_intraday_history(
                    ticker, fetch_start, today + timedelta(days=1), interval="1m"
                )
            except Exception:
                log.warning("Could not fetch 1m intraday history for %s", ticker)
                histories[ticker] = None
        history = histories[ticker]
        if history is None or history.empty:
            continue

        signal_local = r["report_date"].astimezone(history.index.tz)
        candidates.append({"reporter": r, "signal_time": signal_local, "history": history})

    return candidates


def simulate(candidates, stop_pct, target_pct):
    cash = config.STARTING_CAPITAL_INR
    trades = []

    for c in candidates:
        resolved = _resolve_trade(c["signal_time"], c["history"], stop_pct, target_pct)
        if resolved is None:
            continue

        entry_price = resolved["entry_price"]
        allocation = min(cash, config.MAX_POSITION_VALUE_INR)
        quantity = math.floor(allocation / entry_price)
        cost = quantity * entry_price
        if quantity < 1 or cost > cash:
            continue

        cash -= cost
        proceeds = quantity * resolved["exit_price"]
        cash += proceeds
        pnl = proceeds - cost
        pnl_pct = (resolved["exit_price"] / entry_price - 1) * 100

        r = c["reporter"]
        trades.append(
            {
                "ticker": r["ticker"],
                "report_date": r["report_date"],
                "pead_score": r["pead_score"],
                "surprise_pct": r["surprise_pct"],
                "entry_time": resolved["entry_time"],
                "entry_price": round(entry_price, 2),
                "exit_time": resolved["exit_time"],
                "exit_price": round(resolved["exit_price"], 2),
                "quantity": quantity,
                "reason": resolved["reason"],
                "pnl_pct": round(pnl_pct, 2),
                "pnl_amount": round(pnl, 2),
            }
        )

    return trades, cash


def regression_grid(candidates):
    """Sweep stop-loss % x target % (None = no target) against the same candidate
    set, return every combination's total P&L so the best one can be picked."""
    stop_options = [0.01, 0.015, 0.02, 0.025, 0.03, 0.04, 0.05]
    target_options = [None, 0.02, 0.03, 0.05, 0.07, 0.10, 0.15]

    grid = []
    for target_pct in target_options:
        for stop_pct in stop_options:
            trades, cash = simulate(candidates, stop_pct, target_pct)
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


def main():
    import json
    import sys

    lookback_days = int(sys.argv[1]) if len(sys.argv) > 1 else LOOKBACK_DAYS
    if lookback_days > 7:
        log.warning(
            "Requested %d days, capping at 7 - 1-minute bars (needed for few-seconds "
            "entry precision) aren't available further back than that.",
            lookback_days,
        )
        lookback_days = 7

    log.warning("Fetching Nifty 500 constituent list from NSE (live, independent source)...")
    symbols = fetch_nifty500_symbols()
    log.warning("%d symbols. Scanning for reports in the last %d days...", len(symbols), lookback_days)

    reporters = scan_symbols(symbols, lookback_days)
    reporters.sort(key=lambda r: -r["pead_score"])

    print(f"\n{len(reporters)} companies reported results in the last {lookback_days} days\n")
    excellent = [r for r in reporters if r["category"] == "Excellent"]
    print(f"{len(excellent)} rated 'Excellent' (score >= {EXCELLENT_MIN})\n")

    candidates = fetch_candidate_bars(excellent, lookback_days)
    skipped_after_hours = len(excellent) - len(candidates)
    print(f"{skipped_after_hours} skipped: result declared outside market hours (09:15-15:30 IST)")
    print(f"{len(candidates)} candidates with a market-hours result and 1m intraday data\n")

    default_trades, default_cash = simulate(candidates, config.TRAILING_STOP_PCT, None)

    print(f"=== Default params: {config.TRAILING_STOP_PCT*100:.0f}% trailing stop, no target ===")
    print(f"{'Ticker':<16}{'Result (IST)':>20}{'Buy (IST)':>20}{'Lag':>7}{'Entry Px':>10}{'Exit Px':>10}  {'Reason':<14}{'PnL%':>7}")
    for t in default_trades:
        result_ist = t["report_date"].astimezone(storage.IST)
        entry_ist = t["entry_time"].astimezone(storage.IST)
        lag_s = (entry_ist - result_ist).total_seconds()
        lag_str = f"{lag_s/60:.1f}m" if lag_s >= 60 else f"{lag_s:.0f}s"
        print(
            f"{t['ticker']:<16}{result_ist.strftime('%Y-%m-%d %H:%M:%S'):>20}"
            f"{entry_ist.strftime('%Y-%m-%d %H:%M:%S'):>20}{lag_str:>7}"
            f"{t['entry_price']:>10}{t['exit_price']:>10}  {t['reason']:<14}{t['pnl_pct']:>6}%"
        )

    total_pnl = sum(t["pnl_amount"] for t in default_trades)
    wins = [t for t in default_trades if t["pnl_amount"] > 0]
    print(f"\nTrades: {len(default_trades)}")
    if default_trades:
        print(f"Win rate: {len(wins)/len(default_trades)*100:.1f}%")
    print(f"Total P&L: Rs {total_pnl:,.2f}")
    print(f"Return: {(default_cash/config.STARTING_CAPITAL_INR - 1)*100:.2f}%")

    print(f"\n=== Regression: target% x stop-loss% grid (same {len(candidates)} candidates) ===")
    grid = regression_grid(candidates)
    print(f"{'Target':>8}{'Stop':>7}{'Trades':>8}{'Wins':>6}{'WinRate':>9}{'Total PnL':>14}{'Return%':>9}")
    for g in grid[:15]:
        target_str = f"{g['target_pct']*100:.0f}%" if g["target_pct"] else "none"
        print(
            f"{target_str:>8}{g['stop_pct']*100:>6.1f}%{g['trades']:>8}{g['wins']:>6}"
            f"{g['win_rate']:>8.1f}%{'Rs '+format(g['total_pnl'], ',.2f'):>14}{g['return_pct']:>8.2f}%"
        )
    best = grid[0]
    print(
        f"\nBest combination: target="
        f"{(str(best['target_pct']*100)+'%') if best['target_pct'] else 'none'}, "
        f"stop={best['stop_pct']*100:.1f}% -> Rs {best['total_pnl']:,.2f} ({best['return_pct']:.2f}%)"
    )

    dump = {
        "lookback_days": lookback_days,
        "reporters": [{**r, "report_date": r["report_date"].isoformat()} for r in reporters],
        "excellent_count": len(excellent),
        "skipped_after_hours": skipped_after_hours,
        "default_trades": [
            {
                **t,
                "report_date": t["report_date"].isoformat(),
                "entry_time": t["entry_time"].isoformat(),
                "exit_time": t["exit_time"].isoformat(),
            }
            for t in default_trades
        ],
        "default_ending_cash": default_cash,
        "regression_grid": grid,
    }
    with open("independent_pead_results.json", "w") as f:
        json.dump(dump, f, indent=2, default=str)
    print("\nFull results written to independent_pead_results.json")


if __name__ == "__main__":
    main()
