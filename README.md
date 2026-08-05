# PEAD Trading Bot (paper trading)

Watches the "PEAD" topic in your "Financially Free Premium Community" Telegram
group, reads the result cards posted by `FinanciallyFreeBot`, extracts the
PEAD score and financials with Google's Gemini vision API (free tier), and
simulates buy/sell trades against a virtual portfolio. **No real broker is
connected — this only tracks a simulated portfolio in a local SQLite
database.**

## Strategy

- **Buy**: PEAD score >= 60, and not overridden by a sanity check (skip if
  Revenue and Net Profit on the same card both declined QoQ and YoY, since
  that contradicts a bullish score).
- **Position size**: 10% of starting capital per trade, capped at 10
  concurrent open positions (all configurable in `.env`).
- **Exit**: stop-loss at -8% or target at +15% from entry, checked every 5
  minutes.

## Setup

1. `cd ~/pead-trading-bot && source venv/bin/activate && pip install -r requirements.txt`
2. `cp .env.example .env` and fill in:
   - `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` from https://my.telegram.org
   - `GEMINI_API_KEY` — a free key from https://aistudio.google.com/apikey
     (click "Create API key"; no billing setup required for the free tier)
   - Double check `TELEGRAM_GROUP_ID` / `TELEGRAM_PEAD_TOPIC_ID` match your
     channel (defaults are pre-filled from the group you showed me).
3. Log in to Telegram once, interactively, yourself:
   `python telegram_login.py`
   (enter your phone number, the code Telegram sends you, and your 2FA
   password if you have one — do this directly, not through an AI chat).
4. Run the bot: `python main.py`

It will log every card it sees, why it did or didn't buy, and every simulated
trade. Portfolio state lives in `pead_bot.db` — delete it to reset.

## Notes / caveats

- The source cards say "AI-generated — verify against source PDF before
  acting." This bot only does an automated sanity check against the
  financials table on the same card; it does not fetch or verify the source
  PDF.
- Price data comes from `yfinance` (delayed, best-effort) — fine for paper
  trading, not for live execution.
- System Python here is 3.8 (EOL upstream but functional for these
  dependencies). Consider upgrading Python/Homebrew at some point, but it
  isn't required for this to run.
- This is simulation only. Wiring up a real broker (Zerodha Kite Connect,
  etc.) would be a separate, deliberate step later.
