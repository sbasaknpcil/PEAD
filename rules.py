import config


def _find_metric(financials, *name_fragments):
    for row in financials or []:
        metric = (row.get("metric") or "").lower()
        if any(fragment in metric for fragment in name_fragments):
            return row
    return None


def _is_declining(row):
    if not row:
        return False
    latest, prior, year_ago = row.get("latest_quarter"), row.get("prior_quarter"), row.get("year_ago_quarter")
    if latest is None:
        return False
    declined_qoq = prior is not None and latest < prior
    declined_yoy = year_ago is not None and latest < year_ago
    # Only flag a conflict when we have both comparisons and both are declines.
    return (prior is not None and year_ago is not None) and declined_qoq and declined_yoy


def decide_buy(card):
    """Returns (should_buy: bool, reason: str) for a parsed PEAD card."""
    score = card.get("pead_score")
    if score is None:
        return False, "no PEAD score found on card"
    if score < config.PEAD_BUY_SCORE_MIN:
        return False, f"score {score} below buy threshold {config.PEAD_BUY_SCORE_MIN}"

    market_cap = card.get("market_cap_cr")
    if market_cap is None:
        return False, "market cap not found on card, skipping (fail-closed)"
    if market_cap < config.MIN_MARKET_CAP_CR:
        return False, f"market cap {market_cap} Cr below minimum {config.MIN_MARKET_CAP_CR} Cr"

    revenue = _find_metric(card.get("financials"), "revenue")
    profit = _find_metric(card.get("financials"), "net profit", "pat")

    if _is_declining(revenue) and _is_declining(profit):
        return False, (
            f"score {score} is bullish but Revenue and Net Profit both declined "
            "QoQ and YoY on the same card — skipping as a sanity-check conflict"
        )

    return True, f"score {score} >= {config.PEAD_BUY_SCORE_MIN}, MCap {market_cap} Cr, financials do not conflict"


def is_above_200dma(price, dma_200):
    """True if price has crossed above the 200-day moving average. False (with no data
    to compare) is treated as a fail-closed 'don't buy' rather than an open pass."""
    return dma_200 is not None and price > dma_200


def passes_rsi(rsi):
    """True if RSI clears the configured minimum. None (insufficient price history)
    is treated as a fail-closed 'don't buy' rather than an open pass."""
    return rsi is not None and rsi > config.RSI_MIN
