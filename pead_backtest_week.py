"""Same simulation as pead_backtest_day.py, generalized across multiple trading days,
with two changes driven by the live-latency question:

  - check_guidance=False for the buy-decision scoring. Guidance-checking hits
    screener.in over the network (PDF download+parse) and we've measured actual
    timeouts/connection failures from it — it cannot be trusted to fit inside a
    hard latency budget, so it's excluded from the buy-critical path entirely
    (financials + technicals only, both fast/local-ish API calls).
  - Every pick's latency is checked against a 30-second SLA (card arrival ->
    score computed). A breach is flagged, not silently absorbed.
"""
import glob
import re
import time
from datetime import datetime, timedelta

import pandas as pd
import yfinance as yf

import config
import price_feed
import pead_rate_results as rate_results
import vision_parser

IST_OFFSET = timedelta(hours=5, minutes=30)
ORDER_BUFFER_SECONDS = 5
LATENCY_SLA_SECONDS = 30

SAME_DAY_TARGET_PCT = 0.05  # see pead_backtest_day.py docstring — config.py dropped this default

# Hard cutoff for both entry and exit: no buy after this, and any still-open
# position is force-closed at/before this time. Overrides config.py's 15:30
# close per explicit instruction — no trading in the last half hour.
SELL_CUTOFF_HOUR = 15
SELL_CUTOFF_MINUTE = 0

# Market-mood filter: pause new buys while the Nifty 50 is down more than this
# from the day's open. Not a permanent kill switch for the day — re-checked at
# every candidate buy time, so trading resumes as soon as the index recovers
# back above the threshold ("till market mood swing", not "for the rest of day").
MARKET_MOOD_INDEX = "^NSEI"
MARKET_MOOD_THRESHOLD_PCT = -0.3

TRADING_DAYS = ["2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07"]


def _card_timestamp(path):
    """See pead_backtest_day.py — Telethon's default filename embeds UTC, not IST."""
    match = re.search(r"(\d{4}-\d{2}-\d{2})_(\d{2})-(\d{2})-(\d{2})", path)
    date_str, h, m, s = match.groups()
    utc_naive = datetime.strptime(date_str, "%Y-%m-%d").replace(hour=int(h), minute=int(m), second=int(s))
    return utc_naive + IST_OFFSET


def _bare_ticker(symbol):
    return (symbol or "").upper().replace(".NS", "").replace(".BO", "")


def load_cards_with_paths(pattern):
    seen = set()
    cards = []
    for path in sorted(glob.glob(pattern)):
        card = vision_parser.extract_card(path)
        key = (card.get("quarter"), tuple(
            (r.get("metric"), r.get("latest_quarter")) for r in card.get("financials") or []
        ))
        if key in seen:
            continue
        seen.add(key)
        card["_path"] = path
        card["_arrival"] = _card_timestamp(path)
        cards.append(card)
    return cards


def score_all_and_pick_excellent(cards):
    picks = []
    sla_breaches = 0
    for card in cards:
        start = time.monotonic()
        result = rate_results.rate_card(card, check_guidance=False)
        latency = time.monotonic() - start

        if latency + ORDER_BUFFER_SECONDS > LATENCY_SLA_SECONDS:
            sla_breaches += 1
            print(f"  SLA BREACH: {result['company']} took {latency:.1f}s (budget {LATENCY_SLA_SECONDS}s)")

        if result["rating"] != "Excellent":
            continue

        buy_time = card["_arrival"] + timedelta(seconds=latency) + timedelta(seconds=ORDER_BUFFER_SECONDS)
        picks.append({
            "company": result["company"],
            "symbol": result["symbol"],
            "ticker": _bare_ticker(result["symbol"]),
            "financially_free_pead_score": card.get("pead_score"),
            "my_composite": result["composite"],
            "card_arrival": card["_arrival"],
            "score_latency_sec": round(latency, 1),
            "buy_time": buy_time,
        })
    return picks, sla_breaches


def fetch_intraday(symbol, day):
    symbol = price_feed._symbol(symbol)
    start = day
    end = day + timedelta(days=1)
    for interval in ("1m", "5m", "15m"):
        df = yf.download(symbol, start=start, end=end, interval=interval, progress=False, auto_adjust=False)
        if not df.empty:
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            return df, interval
    return pd.DataFrame(), None


def fetch_market_mood_series(day):
    """Nifty 50 intraday % change from the day's open, as a Series indexed by
    IST-naive timestamp. Empty Series if the index data isn't available.
    Fetches ^NSEI directly (not via fetch_intraday) — price_feed._symbol()
    force-appends .NS to any bare ticker, which turns the index symbol into
    the invalid '^NSEI.NS' and silently 404s every time."""
    df = pd.DataFrame()
    for interval in ("1m", "5m", "15m"):
        df = yf.download(MARKET_MOOD_INDEX, start=day, end=day + timedelta(days=1),
                          interval=interval, progress=False, auto_adjust=False)
        if not df.empty:
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            break
    if df.empty:
        return pd.Series(dtype=float)
    if df.index.tz is not None:
        df.index = df.index.tz_convert("Asia/Kolkata").tz_localize(None)
    day_open = float(df["Open"].iloc[0])
    return (df["Close"] - day_open) / day_open * 100


def market_mood_ok(mood_series, at_time):
    """True if the Nifty was at/above MARKET_MOOD_THRESHOLD_PCT from the day's
    open as of the most recent bar at-or-before at_time. Defaults to True (open
    to trade) if we have no index data at all — absence of data isn't treated
    as a bad-mood signal, only an actual observed decline is."""
    if mood_series.empty:
        return True
    prior = mood_series[mood_series.index <= at_time]
    if prior.empty:
        return True
    return float(prior.iloc[-1]) >= MARKET_MOOD_THRESHOLD_PCT


def simulate_trade(intraday_ist, buy_time):
    after_buy = intraday_ist[intraday_ist.index >= buy_time]
    if after_buy.empty:
        return None

    entry_time = after_buy.index[0]
    entry_price = float(after_buy["Open"].iloc[0])
    close_cutoff = entry_time.replace(hour=SELL_CUTOFF_HOUR, minute=SELL_CUTOFF_MINUTE, second=0)

    peak_price = entry_price
    for ts, row in after_buy.iloc[1:].iterrows():
        price = float(row["Close"])
        if price >= entry_price * (1 + SAME_DAY_TARGET_PCT):
            return {"entry_time": entry_time, "entry_price": entry_price, "exit_time": ts,
                    "exit_price": price, "reason": "same-day-target"}
        peak_price = max(peak_price, price)
        trailing_stop = peak_price * (1 - config.TRAILING_STOP_PCT)
        if price <= trailing_stop:
            return {"entry_time": entry_time, "entry_price": entry_price, "exit_time": ts,
                    "exit_price": price, "reason": "trailing-stop"}
        if ts >= close_cutoff:
            return {"entry_time": entry_time, "entry_price": entry_price, "exit_time": ts,
                    "exit_price": price, "reason": "end-of-day"}

    last_ts = after_buy.index[-1]
    last_price = float(after_buy["Close"].iloc[-1])
    return {"entry_time": entry_time, "entry_price": entry_price, "exit_time": last_ts,
            "exit_price": last_price, "reason": "end-of-day (data ended)"}


def run_day(date_str):
    print(f"\n########## {date_str} ##########")
    cards = load_cards_with_paths(f"downloaded_cards/photo_{date_str}_*.jpg")
    if not cards:
        print("  no cards on disk for this date")
        return [], 0.0, 0

    picks, sla_breaches = score_all_and_pick_excellent(cards)
    print(f"  {len(cards)} cards scored, {len(picks)} rated Excellent, {sla_breaches} latency-SLA breaches")

    market_open = (config.MARKET_OPEN_IST_HOUR, config.MARKET_OPEN_IST_MINUTE)
    market_close = (SELL_CUTOFF_HOUR, SELL_CUTOFF_MINUTE)  # no buy/sell after 15:00, per instruction
    day = datetime.strptime(date_str, "%Y-%m-%d")
    mood_series = fetch_market_mood_series(day)

    day_rows = []
    day_pnl = 0.0
    for pick in picks:
        buy_hm = (pick["buy_time"].hour, pick["buy_time"].minute)
        if not (market_open <= buy_hm < market_close):
            print(f"  {pick['company']}: buy at {pick['buy_time'].strftime('%H:%M:%S')} IST outside market hours — no trade")
            day_rows.append({**pick, "trade": None})
            continue

        if not market_mood_ok(mood_series, pick["buy_time"]):
            nifty_pct = float(mood_series[mood_series.index <= pick["buy_time"]].iloc[-1])
            print(f"  {pick['company']}: buy at {pick['buy_time'].strftime('%H:%M:%S')} IST held — "
                  f"Nifty {nifty_pct:+.2f}% from open, below {MARKET_MOOD_THRESHOLD_PCT}% mood threshold — no trade")
            day_rows.append({**pick, "trade": None})
            continue

        df, interval = fetch_intraday(pick["symbol"], day)
        if df.empty:
            print(f"  {pick['company']}: no intraday data — skipped")
            day_rows.append({**pick, "trade": None})
            continue

        if df.index.tz is not None:
            df.index = df.index.tz_convert("Asia/Kolkata").tz_localize(None)
        trade = simulate_trade(df, pick["buy_time"])
        if trade is None:
            print(f"  {pick['company']}: buy time after last bar ({interval}) — skipped")
            day_rows.append({**pick, "trade": None})
            continue

        quantity = int(config.MAX_POSITION_VALUE_INR // trade["entry_price"])
        pnl = quantity * (trade["exit_price"] - trade["entry_price"])
        day_pnl += pnl
        trade["quantity"] = quantity
        trade["pnl"] = pnl
        print(f"  {pick['company']:35s} buy {trade['entry_time'].strftime('%H:%M:%S')} @ {trade['entry_price']:.2f} "
              f"-> {trade['reason']:22s} {trade['exit_time'].strftime('%H:%M:%S')} @ {trade['exit_price']:.2f} "
              f"qty {quantity:5d} P&L Rs.{pnl:+.0f}")
        day_rows.append({**pick, "trade": trade})

    print(f"  --- {date_str} day P&L: Rs.{day_pnl:+,.0f} across {sum(1 for r in day_rows if r['trade'])} trades ---")
    return day_rows, day_pnl, sla_breaches


def main():
    all_rows = []
    total_pnl = 0.0
    total_sla_breaches = 0

    for date_str in TRADING_DAYS:
        rows, day_pnl, sla_breaches = run_day(date_str)
        all_rows.extend([{**r, "date": date_str} for r in rows])
        total_pnl += day_pnl
        total_sla_breaches += sla_breaches

    print("\n\n=== WEEK SUMMARY ===")
    print(f"Total latency-SLA breaches (>{LATENCY_SLA_SECONDS}s) across the week: {total_sla_breaches}")
    print(f"Total P&L across {sum(1 for r in all_rows if r['trade'])} executed trades: Rs.{total_pnl:+,.0f}")

    print("\n=== Full trade table ===")
    table_rows = []
    for r in all_rows:
        t = r["trade"]
        table_rows.append({
            "date": r["date"],
            "company": r["company"],
            "ff_score": r["financially_free_pead_score"],
            "buy_time": r["buy_time"].strftime("%H:%M:%S"),
            "latency_sec": r["score_latency_sec"],
            "pnl": round(t["pnl"], 0) if t else None,
            "status": t["reason"] if t else "no trade",
        })
    df = pd.DataFrame(table_rows)
    pd.set_option("display.max_rows", None)
    pd.set_option("display.width", 160)
    print(df.to_string(index=False))


if __name__ == "__main__":
    main()
