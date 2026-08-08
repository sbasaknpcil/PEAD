# PEAD Trading Bot (paper trading)

Watches the "PEAD" topic in your "Financially Free Premium Community" Telegram
group, reads the result cards posted by `FinanciallyFreeFFBot`, extracts the
PEAD score and financials with local OCR (Tesseract — free, no API key, no
rate limits), and simulates buy/sell trades against a virtual portfolio.
**No real broker is connected — this only tracks a simulated portfolio in a
local SQLite database.**

## Strategy

- **Buy**: PEAD score >= 50, price above its 200-day moving average, market
  cap >= Rs 2,000 Cr, RSI(14) > 50, and not overridden by a sanity check (skip
  if Revenue and Net Profit on the same card both declined QoQ and YoY, since
  that contradicts a bullish score). A buy also requires the `earnings_pulse`
  channel to independently rate the same stock "Excellent" the same (IST)
  calendar day — whichever of the two signals arrives second triggers the
  trade. The trade is only actually placed if that confirmation lands during
  NSE trading hours (09:15-15:25 IST) — since every position must exit the
  same day it opens, a signal confirmed after hours would have no real
  trading window and is skipped entirely rather than bought and immediately
  force-closed near the same price.
- **Ticker resolution**: cards that show an NSE ticker use it directly. Cards
  that only show a BSE code fall back to resolving the company name to a
  tradeable Yahoo Finance symbol (NSE preferred, BSE otherwise) — many
  BSE-labeled companies are actually dual-listed.
- **Position size**: fixed Rs 100,000 per trade, capped at 10 concurrent open
  positions (all configurable in `.env`).
- **Exit — intraday only, every position closes the same day it opens**,
  checked every 5 minutes: sell immediately on a same-day gain of +5% from
  entry; otherwise a stop-loss that trails 2% below the highest price seen
  since entry (so it only ever ratchets up, never down); otherwise a forced
  close near market close (15:25 IST) if neither has triggered yet.

## Setup

1. `cd ~/pead-trading-bot && source venv/bin/activate && pip install -r requirements.txt`
2. Install Tesseract (one-time, system-level): `brew install tesseract`
3. `cp .env.example .env` and fill in `TELEGRAM_API_ID` / `TELEGRAM_API_HASH`
   from https://my.telegram.org. Double check `TELEGRAM_GROUP_ID` /
   `TELEGRAM_PEAD_TOPIC_ID` match your channel (defaults are pre-filled from
   the group you showed me).
4. Log in to Telegram once, interactively, yourself:
   `python telegram_login.py`
   (enter your phone number, the code Telegram sends you, and your 2FA
   password if you have one — do this directly, not through an AI chat).
5. Run the bot: `python main.py`

It will log every card it sees, why it did or didn't buy, and every simulated
trade. Portfolio state lives in `pead_bot.db` — delete it to reset. The bot
is resilient to network outages (retries forever, restarts if the client
ever drops) but the Mac itself must stay awake — see `caffeinate` if running
unattended for extended periods.

## Other scripts

- `python backtest.py` — walks historical PEAD cards through the same rules
  with realistic capital constraints, reports win rate/return, writes
  `backtest_trades.csv`.
- `python list_signals.py` — lists historical cards above the score
  threshold. Override lookback window per-run, e.g.
  `BACKTEST_LOOKBACK_DAYS=7 python list_signals.py`.

Both cache extracted cards in `backtest_signal_cache.json` so re-runs don't
re-process cards already seen.

## Notes / caveats

- The source cards say "AI-generated — verify against source PDF before
  acting." This bot only does an automated sanity check against the
  financials table on the same card; it does not fetch or verify the source
  PDF.
- **OCR accuracy**: cards are read with local OCR against known field
  positions on this specific card template. It's been validated against 15+
  real cards with correct results, but if FinanciallyFreeFFBot changes its
  card design, extraction may silently degrade — worth spot-checking
  occasionally against what actually posts.
- Price data comes from `yfinance` (delayed, best-effort) — fine for paper
  trading, not for live execution.
- System Python here is 3.8 (EOL upstream but functional for these
  dependencies). Consider upgrading Python/Homebrew at some point, but it
  isn't required for this to run.
- This is simulation only. Wiring up a real broker (Zerodha Kite Connect,
  etc.) would be a separate, deliberate step later.
