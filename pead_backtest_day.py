"""Simulate 2026-08-07 as if the bot had traded it live: buy signal = my PEAD
score at 'Excellent', buy timestamp = actual card-arrival time (from the
Telegram media filename) + the real wall-clock time it took this session to
generate that card's score + a few seconds order-placement buffer. Exit logic
matches portfolio.py exactly: same-day target, trailing stop from peak, or
force-close near market close — whichever comes first.
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

# config.py's live exit strategy has since moved to trailing-stop-only, no target
# (see git log: "Switch exit strategy to intraday-only trailing stop") — this
# backtest was explicitly asked to use target + trailing SL, so these are hardcoded
# here rather than read from config, matching the historical SAME_DAY_TARGET_PCT
# default from before that change.
SAME_DAY_TARGET_PCT = 0.05

EARNINGS_PULSE_RATINGS = {
    "ARVSMART": "Excellent", "EMIL": "Good", "CUPID": "Good", "POKARNA": "Excellent",
    "PIXTRANS": "Good", "UNIVCABLES": "Excellent", "IMAGICAA": "Excellent",
    "PASUPTAC": "Good", "ULTRAMAR": "Excellent", "GOLDIAM": "Great", "HINDALCO": "Excellent",
    "GREENLAM": "Weak", "ELLEN": "Excellent", "GOPAL": "Good", "RGL": "Weak",
    "OIL": "Excellent", "AARTIPHARM": "Good", "PRSMJOHNSN": "Good", "PRADPME": "OK",
    "ADVAIT": "OK",
}


def _card_timestamp(path):
    """Telethon's default download filename embeds message.date, which Telethon
    always normalizes to UTC — NOT IST, despite looking like a plain local
    timestamp. Verified directly against the live Telegram API: message 4846
    (Arvind Smartspaces) has message.date = 2026-08-07T08:01:04+00:00, i.e. the
    card actually arrived at 13:31 IST, not 08:01 IST. Convert here rather than
    silently trusting the filename digits as already-local."""
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
    for card in cards:
        start = time.monotonic()
        result = rate_results.rate_card(card, check_guidance=True)
        latency = time.monotonic() - start

        if result["rating"] != "Excellent":
            continue

        buy_time = card["_arrival"] + timedelta(seconds=latency) + timedelta(seconds=ORDER_BUFFER_SECONDS)
        picks.append({
            "company": result["company"],
            "symbol": result["symbol"],
            "ticker": _bare_ticker(result["symbol"]),
            "financially_free_pead_score": card.get("pead_score"),
            "my_pead_rating": result["rating"],
            "my_composite": result["composite"],
            "earnings_pulse": EARNINGS_PULSE_RATINGS.get(_bare_ticker(result["symbol"]), "unrated"),
            "card_arrival": card["_arrival"],
            "score_latency_sec": round(latency, 1),
            "buy_time": buy_time,
        })
        print(f"  {result['company']}: Excellent, latency {latency:.1f}s, buy at {buy_time.strftime('%H:%M:%S')} IST")
    return picks


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


def simulate_trade(intraday_ist, buy_time):
    """intraday_ist: DataFrame indexed by IST-naive timestamps. Mirrors
    portfolio.check_exits()'s target/trailing-stop/force-close logic exactly."""
    after_buy = intraday_ist[intraday_ist.index >= buy_time]
    if after_buy.empty:
        return None

    entry_time = after_buy.index[0]
    entry_price = float(after_buy["Open"].iloc[0])

    close_cutoff = entry_time.replace(
        hour=config.MARKET_CLOSE_IST_HOUR, minute=config.MARKET_CLOSE_IST_MINUTE, second=0
    )

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


def main():
    print("Scoring all 2026-08-07 cards live (timing each) and picking 'Excellent'...\n")
    cards = load_cards_with_paths("downloaded_cards/photo_2026-08-07_*.jpg")
    picks = score_all_and_pick_excellent(cards)

    print(f"\n{len(picks)} buy signals. Simulating same-day trades (market hours only)...\n")

    market_open = (config.MARKET_OPEN_IST_HOUR, config.MARKET_OPEN_IST_MINUTE)
    market_close = (config.MARKET_CLOSE_IST_HOUR, config.MARKET_CLOSE_IST_MINUTE)

    day = datetime(2026, 8, 7)
    results = []
    total_pnl = 0.0
    for pick in picks:
        buy_hm = (pick["buy_time"].hour, pick["buy_time"].minute)
        if not (market_open <= buy_hm < market_close):
            print(f"  {pick['company']}: buy at {pick['buy_time'].strftime('%H:%M:%S')} IST is outside "
                  f"market hours ({market_open[0]:02d}:{market_open[1]:02d}-{market_close[0]:02d}:{market_close[1]:02d}) — no trade")
            pick.update({"trade": None})
            results.append(pick)
            continue

        df, interval = fetch_intraday(pick["symbol"], day)
        if df.empty:
            print(f"  {pick['company']}: no intraday data available (yfinance) — skipped")
            pick.update({"trade": None})
            results.append(pick)
            continue

        # yfinance intraday timestamps come back UTC-aware; convert to IST-naive
        # to compare against buy_time (which is naive IST from the card filename).
        if df.index.tz is not None:
            df.index = df.index.tz_convert("Asia/Kolkata").tz_localize(None)
        trade = simulate_trade(df, pick["buy_time"])
        if trade is None:
            print(f"  {pick['company']}: buy time is after the day's last bar ({interval}) — skipped")
            pick.update({"trade": None})
            results.append(pick)
            continue

        quantity = int(config.MAX_POSITION_VALUE_INR // trade["entry_price"])
        pnl = quantity * (trade["exit_price"] - trade["entry_price"])
        total_pnl += pnl
        trade["quantity"] = quantity
        trade["pnl"] = pnl
        trade["interval"] = interval
        pick["trade"] = trade
        results.append(pick)

    print("\n=== Trade log ===")
    for r in results:
        t = r["trade"]
        if t is None:
            print(f"{r['company']:35s} NO TRADE (no data)")
            continue
        print(f"{r['company']:35s} buy {t['entry_time'].strftime('%H:%M:%S')} @ {t['entry_price']:.2f}  "
              f"-> {t['reason']:22s} {t['exit_time'].strftime('%H:%M:%S')} @ {t['exit_price']:.2f}  "
              f"qty {t['quantity']:5d}  P&L Rs.{t['pnl']:+.0f}")

    print(f"\n=== Total day P&L across {sum(1 for r in results if r['trade'])} trades: Rs.{total_pnl:+,.0f} ===")

    print("\n=== Full comparison table ===")
    rows = []
    for r in results:
        t = r["trade"]
        rows.append({
            "company": r["company"],
            "financially_free_score": r["financially_free_pead_score"],
            "my_rating": r["my_pead_rating"],
            "earnings_pulse": r["earnings_pulse"],
            "buy_time": r["buy_time"].strftime("%H:%M:%S"),
            "pnl": round(t["pnl"], 0) if t else None,
        })
    print(pd.DataFrame(rows).to_string(index=False))


if __name__ == "__main__":
    main()
