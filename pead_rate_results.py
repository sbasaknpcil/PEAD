"""Generate a PEAD score for earnings-result cards using our own data, not the
source channels' PEAD score or Earnings Pulse rating. Standard process:

  - Financials trend (50%): Revenue / EBITDA-margin (proxy for OPM) / Net
    Profit (PAT) / EPS, each categorized Weak..Excellent on its QoQ growth
    and its YoY growth separately, then combined per-metric with a bonus for
    *consistent* growers (good in both QoQ and YoY) and a penalty for names
    that grew one period and declined the other (lumpy/decelerating, not a
    real trend).
  - Guidance met (25%): actual vs. what management guided on the prior concall
    (pead_guidance_check.py — regex/keyword extraction, not an LLM read).
  - Technicals (25%): RSI and position vs 200DMA, as a trend-confirmation
    signal — good technicals raise the score, not just good fundamentals.

Deliberately NOT included: raw short-window price reaction. A regression
against Earnings Pulse's own ratings (2026-08-07 batch) showed it has ~zero
correlation with result quality and was the exact reason a couple of
mediocre-fundamentals names (Greenlam, Renaissance Global) scored too high —
it mostly picks up broader stock moves unrelated to the result. Still computed
and shown for reference, just not weighted into the composite.

A pillar that can't be computed (no concall coverage, unresolved ticker, no
guidance statements matched) is dropped and the remaining weights renormalized
— it's excluded, not scored as neutral, so the composite only reflects what we
actually know.

Final score is bucketed into the same 5 tiers Earnings Pulse uses, low to
high: Weak, OK, Good, Great, Excellent.
"""
import argparse
import glob
import logging
from datetime import datetime, timedelta

import pandas as pd

import pead_guidance_check as guidance_check
import price_feed
import vision_parser

log = logging.getLogger("rate_results")

PILLAR_WEIGHTS = {"financials": 0.50, "guidance": 0.25, "technical": 0.25}

# Growth-rate bands (%) for Revenue/PAT/EPS: Weak(<0) / OK[0,5) / Good[5,15) /
# Great[15,25) / Excellent(>=25). Margin bands are percentage-POINT change
# since EBITDA margin is already a ratio, not a growth rate.
GROWTH_BANDS = (0, 5, 15, 25)
MARGIN_BANDS = (-1, 0, 1, 3)


def _clamp(value, lo, hi):
    return max(lo, min(hi, value))


def _pct_growth(latest, base):
    if latest is None or base is None or base == 0:
        return None
    return (latest - base) / abs(base) * 100


def _find(financials, name):
    return next((r for r in financials or [] if (r.get("metric") or "").lower() == name.lower()), None)


def _categorize(value, bands):
    """1 (Weak) .. 5 (Excellent) band index, or None if value is missing."""
    if value is None:
        return None
    if value < bands[0]:
        return 1
    if value < bands[1]:
        return 2
    if value < bands[2]:
        return 3
    if value < bands[3]:
        return 4
    return 5


def _metric_score(latest, prior, year_ago, bands, is_margin=False):
    """0-100 score for one metric, valuing consistency across QoQ and YoY
    rather than just averaging them — a name good in both periods scores
    higher than one that averages the same but is lumpy (great YoY, weak QoQ
    or vice versa)."""
    if is_margin:
        qoq_raw = None if latest is None or prior is None else latest - prior
        yoy_raw = None if latest is None or year_ago is None else latest - year_ago
    else:
        qoq_raw = _pct_growth(latest, prior)
        yoy_raw = _pct_growth(latest, year_ago)

    qoq_cat = _categorize(qoq_raw, bands)
    yoy_cat = _categorize(yoy_raw, bands)
    cats = [c for c in (qoq_cat, yoy_cat) if c is not None]
    if not cats:
        return 50.0  # no data either period — neutral, not penalized

    score = ((sum(cats) / len(cats)) - 1) / 4 * 100  # band 1..5 -> 0..100

    if qoq_cat is not None and yoy_cat is not None:
        if qoq_cat >= 3 and yoy_cat >= 3:
            score = min(100, score + 10)  # consistent grower bonus
        elif (qoq_cat == 1) != (yoy_cat == 1):
            score = max(0, score - 10)  # declined one period, grew the other — lumpy

    return score


def financials_score(card):
    revenue = _find(card["financials"], "Revenue") or {}
    ebitda = _find(card["financials"], "EBITDA") or {}
    profit = _find(card["financials"], "Net Profit") or {}
    eps = _find(card["financials"], "EPS") or {}

    metric_scores = [
        _metric_score(revenue.get("latest_quarter"), revenue.get("prior_quarter"), revenue.get("year_ago_quarter"), GROWTH_BANDS),
        _metric_score(ebitda.get("latest_quarter"), ebitda.get("prior_quarter"), ebitda.get("year_ago_quarter"), MARGIN_BANDS, is_margin=True),
        _metric_score(profit.get("latest_quarter"), profit.get("prior_quarter"), profit.get("year_ago_quarter"), GROWTH_BANDS),
        _metric_score(eps.get("latest_quarter"), eps.get("prior_quarter"), eps.get("year_ago_quarter"), GROWTH_BANDS),
    ]
    return sum(metric_scores) / len(metric_scores)


def market_and_technical_scores(symbol):
    """Returns (market_reaction_score, technical_score, detail_dict) using a single
    ~300-day price history fetch — or (None, None, {}) if the symbol can't be resolved
    or has no tradeable history (illiquid/newly-listed/wrong ticker)."""
    if symbol is None:
        return None, None, {}

    now = datetime.now()
    history = price_feed.get_history(symbol, now - timedelta(days=300), now + timedelta(days=1))
    if history.empty or len(history) < 5:
        return None, None, {}

    closes = history["Close"]
    last_close = float(closes.iloc[-1])
    reaction_base = float(closes.iloc[-4]) if len(closes) >= 4 else float(closes.iloc[0])
    reaction_pct = (last_close - reaction_base) / reaction_base * 100
    market_score = 50 + _clamp(reaction_pct * 5, -50, 50)

    rsi = price_feed.rsi_from_closes(closes, period=14)
    dma_200 = float(closes.tail(200).mean()) if len(closes) >= 200 else None
    dma_pct = ((last_close / dma_200) - 1) * 100 if dma_200 else None

    tech_components = []
    if rsi is not None:
        tech_components.append(_clamp(rsi, 0, 100))
    if dma_pct is not None:
        tech_components.append(50 + _clamp(dma_pct * 2, -50, 50))
    technical_score = sum(tech_components) / len(tech_components) if tech_components else None

    return market_score, technical_score, {
        "last_close": last_close,
        "reaction_pct_3d": reaction_pct,
        "rsi": rsi,
        "dma_200": dma_200,
    }


def _label(score):
    if score >= 80:
        return "Excellent"
    if score >= 65:
        return "Great"
    if score >= 50:
        return "Good"
    if score >= 35:
        return "OK"
    return "Weak"


def rate_card(card, check_guidance=True):
    fin_score = financials_score(card)
    symbol = card.get("nse_ticker") or price_feed.resolve_symbol(card.get("company_name"))
    market_score, tech_score, detail = market_and_technical_scores(symbol)

    guidance_score = guidance_period = None
    guidance_notes = []
    if check_guidance:
        guidance_score, guidance_period, guidance_notes = guidance_check.guidance_score_for_card(symbol, card)

    # market_score is computed (and shown) for reference only — not weighted
    # into the composite. See module docstring for why.
    pillars = {
        "financials": fin_score,
        "guidance": guidance_score,
        "technical": tech_score,
    }
    available = {name: score for name, score in pillars.items() if score is not None}
    weight_sum = sum(PILLAR_WEIGHTS[name] for name in available)
    composite = sum(PILLAR_WEIGHTS[name] * score for name, score in available.items()) / weight_sum

    return {
        "company": card.get("company_name"),
        "symbol": symbol,
        "financials_score": round(fin_score, 1),
        "guidance_score": round(guidance_score, 1) if guidance_score is not None else None,
        "guidance_period": guidance_period,
        "technical_score": round(tech_score, 1) if tech_score is not None else None,
        "market_score_fyi": round(market_score, 1) if market_score is not None else None,
        "composite": round(composite, 1),
        "rating": _label(composite),
        "data_complete": len(available) == len(pillars),
        "detail": detail,
        "guidance_notes": guidance_notes,
    }


def load_cards(pattern):
    seen = set()
    cards = []
    for path in sorted(glob.glob(pattern)):
        card = vision_parser.extract_card(path)
        # Key on financials + quarter alone, not the ticker/name (OCR sometimes
        # catches the ticker on one pass of the same card and misses it on another).
        key = (card.get("quarter"), tuple(
            (r.get("metric"), r.get("latest_quarter")) for r in card.get("financials") or []
        ))
        if key in seen:
            continue
        seen.add(key)
        # Prefer the pass that actually caught the ticker, if a later duplicate has one.
        cards.append(card)
    return cards


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pattern", help="glob for card images, e.g. 'downloaded_cards/photo_2026-08-07_*.jpg'")
    parser.add_argument("--no-guidance", action="store_true", help="skip the concall guidance-check pillar (faster)")
    parser.add_argument("--notes", action="store_true", help="print matched guidance sentences per company")
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING)

    cards = load_cards(args.pattern)
    ratings = [rate_card(c, check_guidance=not args.no_guidance) for c in cards]
    ratings.sort(key=lambda r: r["composite"], reverse=True)

    columns = ["company", "symbol", "financials_score", "guidance_score", "guidance_period",
               "technical_score", "market_score_fyi", "composite", "rating", "data_complete"]
    df = pd.DataFrame(ratings)[columns]
    pd.set_option("display.max_rows", None)
    pd.set_option("display.width", 200)
    print(df.to_string(index=False))

    if args.notes:
        for r in ratings:
            if r["guidance_notes"]:
                print(f"\n--- {r['company']} ({r['guidance_period']}) ---")
                for note in r["guidance_notes"]:
                    print(f"  {note}")


if __name__ == "__main__":
    main()
