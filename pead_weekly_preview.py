"""Runs every Sunday: finds companies scheduled to declare results in the
coming Mon-Fri (NSE board-meeting purpose = Financial Results), screens each
one on fundamentals (Screener.in quarterly trend, same categorical growth
banding as pead_rate_results.py) and technicals (price_feed RSI/200DMA), and
posts the strongest few to Telegram with a short story for each.

Reality check baked into the design, not an afterthought: NSE's current
results-purpose board-meeting filings skew heavily toward very recently listed
companies (IPO boom), which by definition have 0-2 quarters of Screener
history - not enough to judge a trend. Rather than force a score onto thin
data, candidates are split into "scored" (>=3 quarters + enough price history)
and "too new to assess" - and if NOTHING clears that bar in a given week, the
Telegram post says so plainly instead of picking weak names anyway.
"""
import logging
import re
from datetime import datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup
from telethon import TelegramClient

import config
import price_feed

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("weekly_preview")

IST = timezone(timedelta(hours=5, minutes=30))
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
MIN_QUARTERS_FOR_SCORING = 3
TOP_N = 5


def _next_week_range(today=None):
    """Given "today" is a Sunday (when this runs), returns (Monday, Friday) of
    the week that starts tomorrow. Also works sanely if run on another day for
    manual testing - just uses the next Mon-Fri from today."""
    today = today or datetime.now(IST).date()
    days_to_monday = (7 - today.weekday()) % 7 or 7  # next Monday, never today
    monday = today + timedelta(days=days_to_monday)
    friday = monday + timedelta(days=4)
    return monday, friday


def fetch_week_results_calendar(monday, friday):
    """Returns [{symbol, name}] - unique companies with a Financial Results
    board meeting between monday and friday inclusive."""
    session = requests.Session()
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json, text/plain, */*"}
    session.get("https://www.nseindia.com/companies-listing/corporate-filings-board-meetings", headers=headers, timeout=15)
    r = session.get(
        "https://www.nseindia.com/api/corporate-board-meetings"
        f"?index=equities&from_date={monday.strftime('%d-%m-%Y')}&to_date={friday.strftime('%d-%m-%Y')}",
        headers={**headers, "Referer": "https://www.nseindia.com/companies-listing/corporate-filings-board-meetings"},
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()

    seen = {}
    for d in data:
        is_results = "result" in d["bm_purpose"].lower() or "financial result" in d["bm_desc"].lower()
        if is_results and d["bm_symbol"] not in seen:
            seen[d["bm_symbol"]] = d["sm_name"]
    return [{"symbol": s, "name": n} for s, n in seen.items()]


def fetch_screener_page(symbol):
    for variant in ("consolidated", ""):
        url = f"https://www.screener.in/company/{symbol}/{variant + '/' if variant else ''}"
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=15)
        if resp.status_code == 200 and "Quarterly Results" in resp.text:
            return BeautifulSoup(resp.text, "html.parser")
    return None


def parse_quarterly_table(soup):
    """Returns (quarter_labels, {metric_name: [values oldest..newest]}) or
    (None, None) if the table has no quarter columns at all (brand-new
    listing Screener hasn't backfilled)."""
    section = soup.find("h2", string="Quarterly Results")
    if section is None:
        return None, None
    table = section.find_next("table")
    rows = table.find_all("tr")
    header_cells = [c.get_text(strip=True) for c in rows[0].find_all(["th", "td"])][1:]
    if not header_cells:
        return None, None

    metrics = {}
    for row in rows[1:]:
        cells = [c.get_text(strip=True) for c in row.find_all(["th", "td"])]
        if len(cells) < 2:
            continue
        label = cells[0].rstrip("+")
        values = []
        for v in cells[1:]:
            cleaned = v.replace(",", "").replace("%", "").strip()
            try:
                values.append(float(cleaned))
            except ValueError:
                values.append(None)
        metrics[label] = values
    return header_cells, metrics


def _about_text(soup):
    about = soup.find("div", class_="sub show-more-box about")
    if not about:
        return None
    text = about.get_text(" ", strip=True)
    return re.sub(r"\[\d+\]\s*$", "", text).strip()


def _categorize_growth(pct, bands=(0, 5, 15, 25)):
    if pct is None:
        return None
    if pct < bands[0]:
        return 1
    if pct < bands[1]:
        return 2
    if pct < bands[2]:
        return 3
    if pct < bands[3]:
        return 4
    return 5


def _metric_score(values, is_margin=False):
    """values: oldest..newest, at least 3 quarters. Same categorical banding +
    consistency logic as pead_rate_results.py, adapted to a Screener row
    instead of an OCR'd card row - QoQ = last vs second-last, YoY = last vs
    4-quarters-back if available."""
    clean = [v for v in values if v is not None]
    if len(clean) < MIN_QUARTERS_FOR_SCORING:
        return None

    latest, prior = values[-1], values[-2]
    year_ago = values[-5] if len(values) >= 5 else None

    if is_margin:
        qoq = (latest - prior) if None not in (latest, prior) else None
        yoy = (latest - year_ago) if None not in (latest, year_ago) else None
        bands = (-1, 0, 1, 3)
    else:
        qoq = (latest - prior) / abs(prior) * 100 if prior else None
        yoy = (latest - year_ago) / abs(year_ago) * 100 if year_ago else None
        bands = (0, 5, 15, 25)

    qoq_cat = _categorize_growth(qoq, bands)
    yoy_cat = _categorize_growth(yoy, bands)
    cats = [c for c in (qoq_cat, yoy_cat) if c is not None]
    if not cats:
        return 50.0

    score = (sum(cats) / len(cats) - 1) / 4 * 100
    if qoq_cat is not None and yoy_cat is not None:
        if qoq_cat >= 3 and yoy_cat >= 3:
            score = min(100, score + 10)
        elif (qoq_cat == 1) != (yoy_cat == 1):
            score = max(0, score - 10)
    return score


def financials_score(metrics):
    sales = metrics.get("Sales") or metrics.get("Revenue")
    opm = metrics.get("OPM %")
    profit = metrics.get("Net Profit")
    eps = metrics.get("EPS in Rs")

    scores = []
    for values, is_margin in ((sales, False), (opm, True), (profit, False), (eps, False)):
        if values is None:
            continue
        s = _metric_score(values, is_margin=is_margin)
        if s is not None:
            scores.append(s)
    if not scores:
        return None
    return sum(scores) / len(scores)


def technical_score(symbol):
    try:
        dma_200 = price_feed.get_200dma(symbol)
        rsi = price_feed.get_rsi(symbol)
        price = price_feed.get_last_price(symbol)
    except Exception:
        return None, {}
    if dma_200 is None and rsi is None:
        return None, {}

    components = []
    if rsi is not None:
        components.append(min(100, max(0, rsi)))
    if dma_200 is not None:
        dma_pct = (price / dma_200 - 1) * 100
        components.append(50 + min(50, max(-50, dma_pct * 2)))
    if not components:
        return None, {}
    return sum(components) / len(components), {"price": price, "dma_200": dma_200, "rsi": rsi}


def screen_candidate(symbol, name):
    soup = fetch_screener_page(symbol)
    if soup is None:
        return {"symbol": symbol, "name": name, "status": "no_screener_page"}

    quarters, metrics = parse_quarterly_table(soup)
    about = _about_text(soup)
    n_quarters = len([v for v in (metrics or {}).get("Sales", []) if v is not None]) if metrics else 0

    if n_quarters < MIN_QUARTERS_FOR_SCORING:
        return {
            "symbol": symbol, "name": name, "status": "too_new",
            "quarters_available": n_quarters, "about": about,
        }

    fin_score = financials_score(metrics)
    tech_score, tech_detail = technical_score(symbol)

    if fin_score is None:
        return {"symbol": symbol, "name": name, "status": "insufficient_metrics", "about": about}

    weights = {"financials": 0.7, "technical": 0.3} if tech_score is not None else {"financials": 1.0}
    composite = fin_score * weights["financials"] + (tech_score * weights.get("technical", 0) if tech_score is not None else 0)

    return {
        "symbol": symbol, "name": name, "status": "scored",
        "financials_score": round(fin_score, 1),
        "technical_score": round(tech_score, 1) if tech_score is not None else None,
        "composite": round(composite, 1),
        "about": about,
        "latest_sales": (metrics.get("Sales") or [None])[-1],
        "latest_opm": (metrics.get("OPM %") or [None])[-1],
        "tech_detail": tech_detail,
    }


def build_message(monday, friday, candidates):
    scored = sorted([c for c in candidates if c["status"] == "scored"], key=lambda c: c["composite"], reverse=True)
    too_new = [c for c in candidates if c["status"] == "too_new"]
    no_data = [c for c in candidates if c["status"] in ("no_screener_page", "insufficient_metrics")]

    lines = [f"WEEKLY RESULTS PREVIEW: {monday.strftime('%d %b')} - {friday.strftime('%d %b %Y')}"]
    lines.append(f"\n{len(candidates)} companies scheduled to declare results this week.")

    if not scored:
        lines.append(
            "\nNo candidates cleared the data bar this week (need >=3 quarters of "
            "history to judge a trend) - this week's results calendar is dominated by "
            "very recently listed companies. Nothing confidently strong to flag; "
            "sitting this week out rather than forcing a pick."
        )
    else:
        lines.append(f"\nTop {min(TOP_N, len(scored))} by fundamentals + technicals:\n")
        for c in scored[:TOP_N]:
            tech_str = f", technical {c['technical_score']}" if c["technical_score"] is not None else " (no technical data - too illiquid/short history)"
            lines.append(f"#{c['symbol']} - {c['name']}")
            lines.append(f"  Composite {c['composite']}/100 (fundamentals {c['financials_score']}{tech_str})")
            if c["about"]:
                lines.append(f"  {c['about'][:200]}")
            lines.append("")

    if too_new:
        lines.append(f"Too new to assess (<{MIN_QUARTERS_FOR_SCORING} quarters on record): " + ", ".join(c["symbol"] for c in too_new))
    if no_data:
        lines.append(f"No usable data at all: " + ", ".join(c["symbol"] for c in no_data))

    return "\n".join(lines)


async def post_to_telegram(text):
    client = TelegramClient(config.TELEGRAM_SESSION_NAME, config.TELEGRAM_API_ID, config.TELEGRAM_API_HASH)
    await client.start()
    entity = await client.get_entity("peadtest_sb")
    await client.send_message(entity, text)
    await client.disconnect()


def main():
    import asyncio

    monday, friday = _next_week_range()
    log.info("Fetching results calendar for %s to %s", monday, friday)
    week_companies = fetch_week_results_calendar(monday, friday)
    log.info("%d companies scheduled to declare results", len(week_companies))

    candidates = []
    for wc in week_companies:
        log.info("Screening %s (%s)", wc["symbol"], wc["name"])
        candidates.append(screen_candidate(wc["symbol"], wc["name"]))

    message = build_message(monday, friday, candidates)
    print(message)
    asyncio.run(post_to_telegram(message))
    log.info("Posted to Telegram")


if __name__ == "__main__":
    main()
