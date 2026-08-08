from datetime import datetime, timedelta

import pandas as pd
import yfinance as yf

DMA_WINDOW_DAYS = 200


def _symbol(ticker):
    if ticker.endswith(".NS") or ticker.endswith(".BO"):
        return ticker
    return f"{ticker}.NS"


def _search_india(query):
    try:
        results = yf.Search(query, max_results=5).quotes
    except Exception:
        return None
    india_results = [q for q in results if q.get("symbol", "").endswith((".NS", ".BO"))]
    nse = next((q for q in india_results if q["symbol"].endswith(".NS")), None)
    if nse:
        return nse["symbol"]
    bse = next((q for q in india_results if q["symbol"].endswith(".BO")), None)
    return bse["symbol"] if bse else None


def resolve_symbol(company_name):
    """Resolve a company name to a tradeable Yahoo symbol, preferring NSE over BSE.
    Used when a card shows a BSE code instead of an NSE ticker — many BSE-labeled
    companies are actually dual-listed and have an NSE symbol too."""
    if not company_name:
        return None

    symbol = _search_india(company_name)
    if symbol:
        return symbol

    # The trailing corporate suffix (LTD/LIMITED) is the most OCR-corruption-prone
    # word on the card (large stylized font, often clipped) — retry without it.
    words = company_name.split()
    if len(words) > 1:
        return _search_india(" ".join(words[:-1]))
    return None


def get_last_price(nse_ticker):
    """Latest traded/close price for an NSE symbol (e.g. 'UNIPARTS' -> UNIPARTS.NS)."""
    fast_info = yf.Ticker(_symbol(nse_ticker)).fast_info
    price = fast_info.get("lastPrice") or fast_info.get("last_price")
    if price is None:
        raise ValueError(f"Could not fetch a live price for {_symbol(nse_ticker)}")
    return float(price)


def get_history(nse_ticker, start, end):
    """Daily OHLC for an NSE symbol between start and end (date/datetime, end exclusive)."""
    df = yf.download(_symbol(nse_ticker), start=start, end=end, progress=False, auto_adjust=False)
    if df.empty:
        return df
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df


def get_intraday_history(nse_ticker, start, end, interval="5m"):
    """Intraday OHLC for an NSE symbol between start and end (tz-aware datetimes).
    Yahoo only retains intraday bars for a trailing window (~60 days for 5m), so
    this is for recent/backtest-a-few-weeks use, not the multi-month daily history."""
    df = yf.download(_symbol(nse_ticker), start=start, end=end, interval=interval, progress=False, auto_adjust=False)
    if df.empty:
        return df
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df


def get_200dma(nse_ticker):
    """200-day simple moving average of Close, or None if there isn't enough history."""
    now = datetime.now()
    # ~290 calendar days comfortably covers 200 trading days (weekends + holidays).
    history = get_history(nse_ticker, now - timedelta(days=290), now + timedelta(days=1))
    if len(history) < DMA_WINDOW_DAYS:
        return None
    return float(history["Close"].tail(DMA_WINDOW_DAYS).mean())


def rsi_from_closes(closes, period=14):
    """Wilder's RSI computed from a Series of closing prices, or None if there aren't
    enough rows. Shared by get_rsi() (live) and backtest.py (point-in-time)."""
    if len(closes) < period + 1:
        return None

    delta = closes.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()

    last_avg_gain = float(avg_gain.iloc[-1])
    last_avg_loss = float(avg_loss.iloc[-1])
    if last_avg_loss == 0:
        return 100.0 if last_avg_gain > 0 else 50.0

    rs = last_avg_gain / last_avg_loss
    return 100 - (100 / (1 + rs))


def get_rsi(nse_ticker, period=14):
    """Wilder's RSI over the given period, or None if there isn't enough history."""
    now = datetime.now()
    # A few extra periods of buffer so the smoothing has settled by the last row.
    history = get_history(nse_ticker, now - timedelta(days=period * 5 + 30), now + timedelta(days=1))
    if history.empty:
        return None
    return rsi_from_closes(history["Close"], period)
