"""Heuristic guidance-met check: did a company's actual quarter beat, meet, or
miss the numeric guidance it gave on its *previous* concall?

No LLM/API involved by design (project preference — see pead_concall_fetcher.py's
switch back to local OCR). This is a regex/keyword extraction over the
transcript text, so it will miss guidance phrased in ways the patterns don't
cover, and can't handle guidance given as an absolute rupee/dollar target
(those are surfaced as informational notes only, not scored) since comparing
them needs a base figure this tool doesn't try to infer. Treat the score as a
rough signal, not a verdict — always sanity-check the matched sentences.

Timing assumption: this assumes you're checking guidance shortly after a
quarter's results are out, before that quarter's *own* concall transcript is
uploaded — i.e. the most recent transcript available is the prior quarter's
guidance-setting call, not a call that already discusses the quarter you're
checking. If both concalls have transcripts by the time you run this, it'll
grab the wrong one.
"""
import logging
import re
from datetime import datetime

from pypdf import PdfReader

import pead_concall_fetcher as concall_fetcher

log = logging.getLogger("guidance_check")

# A company that hasn't held a concall in over a year almost certainly isn't
# holding one at all (SME/small-cap) — treat any transcript older than this as
# stale rather than silently guidance-checking a 2020 call against a 2026 quarter.
MAX_GUIDANCE_AGE_DAYS = 460

GUIDANCE_KEYWORDS = re.compile(r"\b(guidance|guided|guide us|targeting|target)\b", re.IGNORECASE)
PCT_RANGE = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*%?\s*(?:to|-)\s*(\d{1,3}(?:\.\d+)?)\s*%")
PCT_SINGLE = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*%")
AMOUNT = re.compile(r"(?:INR|Rs\.?|\$)\s?[\d,]+(?:\.\d+)?[\s\w-]{0,15}?(?:crore|crores|million|per ton)", re.IGNORECASE)

MARGIN_HINTS = ("margin",)
GROWTH_HINTS = ("growth", "cagr", "revenue", "booking", "bookings", "sales", "topline", "top line")


def extract_text(pdf_path):
    reader = PdfReader(str(pdf_path))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def _classify(sentence_lower):
    if any(h in sentence_lower for h in MARGIN_HINTS):
        return "margin_pct"
    if any(h in sentence_lower for h in GROWTH_HINTS):
        return "growth_pct"
    return None


def extract_guidance_statements(text):
    """Returns a list of dicts: {sentence, kind, low, high} for numerically
    comparable (percentage) guidance, plus {sentence, kind: 'amount'} entries
    for guidance stated as a rupee/dollar figure (informational only)."""
    normalized = re.sub(r"\s+", " ", text)
    sentences = re.split(r"(?<=[.!?])\s+(?=[A-Z])", normalized)

    statements = []
    for sentence in sentences:
        if not GUIDANCE_KEYWORDS.search(sentence):
            continue

        range_match = PCT_RANGE.search(sentence)
        if range_match:
            kind = _classify(sentence.lower())
            if kind:
                low, high = sorted(float(x) for x in range_match.groups())
                statements.append({"sentence": sentence.strip(), "kind": kind, "low": low, "high": high})
                continue

        single_match = PCT_SINGLE.search(sentence)
        if single_match:
            kind = _classify(sentence.lower())
            if kind:
                value = float(single_match.group(1))
                statements.append({"sentence": sentence.strip(), "kind": kind, "low": value, "high": value})
                continue

        if AMOUNT.search(sentence):
            statements.append({"sentence": sentence.strip(), "kind": "amount"})

    return statements


def _find(financials, name):
    return next((r for r in financials or [] if (r.get("metric") or "").lower() == name.lower()), None)


def _pct_growth(latest, base):
    if latest is None or base is None or base == 0:
        return None
    return (latest - base) / abs(base) * 100


def _verdict_score(actual, low, high):
    if actual is None:
        return None
    if actual >= high:
        return 90.0
    if actual >= low:
        return 70.0
    if actual >= 0:
        return 35.0
    return 10.0


def score_against_actuals(statements, card):
    """Scores growth_pct/margin_pct statements against the card's actual numbers.
    Returns (score or None, notes) — notes list every matched statement (scored
    or not) with what it was compared against, for a human to sanity-check."""
    revenue = _find(card.get("financials"), "Revenue") or {}
    ebitda = _find(card.get("financials"), "EBITDA") or {}
    revenue_yoy = _pct_growth(revenue.get("latest_quarter"), revenue.get("year_ago_quarter"))
    ebitda_margin_latest = ebitda.get("latest_quarter")

    sub_scores = []
    notes = []
    for stmt in statements:
        if stmt["kind"] == "amount":
            notes.append(f"[not scored, absolute figure] {stmt['sentence']}")
            continue

        actual = ebitda_margin_latest if stmt["kind"] == "margin_pct" else revenue_yoy
        actual_label = "actual EBITDA margin" if stmt["kind"] == "margin_pct" else "actual revenue YoY growth"
        score = _verdict_score(actual, stmt["low"], stmt["high"])
        if score is None:
            notes.append(f"[no actual to compare — {actual_label} unavailable] {stmt['sentence']}")
            continue

        sub_scores.append(score)
        actual_str = f"{actual:.1f}%"
        guided_str = f"{stmt['low']:.0f}-{stmt['high']:.0f}%" if stmt["low"] != stmt["high"] else f"{stmt['low']:.0f}%"
        notes.append(f"[{actual_label} {actual_str} vs guided {guided_str}, score {score:.0f}] {stmt['sentence']}")

    if not sub_scores:
        return None, notes
    return sum(sub_scores) / len(sub_scores), notes


def _screener_ticker(ticker):
    """concall_fetcher/Screener expect the bare NSE symbol (e.g. 'ARVSMART'), not
    the yfinance-style symbol with a .NS/.BO suffix that rate_results resolves."""
    return re.sub(r"\.(NS|BO)$", "", ticker or "", flags=re.IGNORECASE)


def get_latest_guidance_transcript(ticker):
    """Most recent concall with an uploaded transcript, skipping quarters that
    don't have one yet. Returns (concall_dict, text) or (None, None) — including
    when the newest available transcript is stale (see MAX_GUIDANCE_AGE_DAYS):
    if the most recent one is too old, every older one is worse, so we stop
    rather than silently guidance-checking against a years-old call."""
    ticker = _screener_ticker(ticker)
    try:
        concalls = concall_fetcher.fetch_concalls(ticker)
    except Exception:
        log.exception("Could not fetch concall list for %s", ticker)
        return None, None

    for concall in concalls:
        if not concall["transcript_url"]:
            continue

        period_date = None
        try:
            period_date = datetime.strptime(concall["period"], "%b %Y")
        except ValueError:
            pass
        if period_date is not None and (datetime.now() - period_date).days > MAX_GUIDANCE_AGE_DAYS:
            log.info("Newest transcript for %s is %s, too stale to use as guidance", ticker, concall["period"])
            return None, None

        try:
            path = concall_fetcher.download_transcript(ticker, concall)
            return concall, extract_text(path)
        except Exception:
            log.exception("Could not download/parse transcript for %s (%s)", ticker, concall["period"])
            continue
    return None, None


def guidance_score_for_card(ticker, card):
    """Returns (score or None, period_checked or None, notes)."""
    if not ticker:
        return None, None, []

    concall, text = get_latest_guidance_transcript(ticker)
    if text is None:
        return None, None, []

    statements = extract_guidance_statements(text)
    score, notes = score_against_actuals(statements, card)
    return score, concall["period"], notes
