# PEAD Trading Bot (paper trading)

Watches the "PEAD" topic in your "Financially Free Premium Community" Telegram
group, reads the result cards posted by `FinanciallyFreeFFBot`, extracts the
PEAD score and financials with local OCR (Tesseract — free, no API key, no
rate limits), and simulates buy/sell trades against a virtual portfolio.
**No real broker is connected — this only tracks a simulated portfolio in a
local SQLite database.**

## Strategy

- **Buy**: any stock the `earnings_pulse` Telegram channel rates "Excellent"
  gets bought immediately — no PEAD score, 200DMA, RSI, or market-cap check.
  The buy only actually places if the rating lands during NSE trading hours
  (09:15-15:30 IST) — since every position must exit the same day it opens, a
  rating outside that window has no real trading window and is skipped
  entirely rather than bought and immediately force-closed near the same
  price. PEAD cards from the "PEAD" topic are still downloaded and logged for
  reference, but don't gate anything.
- **Ticker resolution**: the channel posts a bare ticker (`#TICKER`); resolved
  to a tradeable Yahoo Finance symbol (NSE preferred, BSE otherwise).
- **Position size**: fixed Rs 100,000 per trade, capped at 10 concurrent open
  positions (all configurable in `.env`).
- **Exit — intraday only, every position closes the same day it opens, no
  upper target**, checked every 5 minutes: a stop-loss that trails 2% below
  the highest price seen since entry (so it only ever ratchets up, never
  down); otherwise a forced close near market close (15:30 IST) if it hasn't
  triggered yet.

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

- `python backtest.py` — walks historical `earnings_pulse` "Excellent" ratings
  through the same immediate-buy/trailing-stop rules using 5-minute intraday
  bars (last `INTRADAY_BACKTEST_LOOKBACK_DAYS`, default 7 — Yahoo only keeps
  ~60 days of 5m history), with realistic capital constraints, reports win
  rate/return, writes `backtest_trades.csv`.
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
