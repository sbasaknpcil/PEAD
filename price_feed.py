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


def get_200dma(nse_ticker):
    """200-day simple moving average of Close, or None if there isn't enough history."""
    now = datetime.now()
    # ~290 calendar days comfortably covers 200 trading days (weekends + holidays).
    history = get_history(nse_ticker, now - timedelta(days=290), now + timedelta(days=1))
    if len(history) < DMA_WINDOW_DAYS:
        return None
    return float(history["Close"].tail(DMA_WINDOW_DAYS).mean())
