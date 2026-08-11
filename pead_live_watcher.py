"""Poll NSE for new "Outcome of Board Meeting" filings, score each one directly
from the actual filing (no card, no FinanciallyFreeBot), and post the outcome
to Telegram — a PEAD rating if extraction passes pead_nse_result_parser's sanity
gate, or an honest "skipped" post with the reason if it doesn't. Every post
includes elapsed time since the result was actually declared.

Run once per invocation (`python live_watcher.py`) — intended to be called on
a recurring interval (e.g. via /loop), not to loop internally itself.
"""
import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from telethon import TelegramClient

import config
import pead_nse_result_parser as nrp
import price_feed
import pead_rate_results as rate_results

log = logging.getLogger("live_watcher")

IST = timezone(timedelta(hours=5, minutes=30))
NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "application/json, text/plain, */*",
}
TELEGRAM_TARGET = "peadtest_sb"
POSTED_LOG_PATH = config.BASE_DIR / "posted_nse_results.json"


def _load_posted():
    if not POSTED_LOG_PATH.exists():
        return set()
    return set(json.loads(POSTED_LOG_PATH.read_text()))


def _save_posted(seq_ids):
    POSTED_LOG_PATH.write_text(json.dumps(sorted(seq_ids)))


def fetch_todays_results_filings():
    session = requests.Session()
    today = datetime.now(IST).strftime("%d-%m-%Y")
    session.get("https://www.nseindia.com/companies-listing/corporate-filings-announcements",
                headers=NSE_HEADERS, timeout=15)
    r = session.get(
        f"https://www.nseindia.com/api/corporate-announcements?index=equities&from_date={today}&to_date={today}",
        headers={**NSE_HEADERS, "Referer": "https://www.nseindia.com/companies-listing/corporate-filings-announcements"},
        timeout=15,
    )
    r.raise_for_status()
    return [d for d in r.json() if d.get("desc") == "Outcome of Board Meeting"]


def _download_pdf(url, dest):
    resp = requests.get(url, headers=NSE_HEADERS, timeout=30)
    resp.raise_for_status()
    dest.write_bytes(resp.content)


def build_message(filing, outcome):
    result_time = datetime.strptime(filing["an_dt"], "%d-%b-%Y %H:%M:%S").replace(tzinfo=IST)
    post_time = datetime.now(IST)
    elapsed_sec = int((post_time - result_time).total_seconds())
    header = f"{filing['sm_name']} ({filing['symbol']})\nResult declared: {result_time.strftime('%H:%M:%S')} IST"
    timing = (f"Posted {post_time.strftime('%H:%M:%S')} IST — {elapsed_sec}s "
              f"({elapsed_sec // 60}m {elapsed_sec % 60}s) after declaration")

    if outcome["ok"]:
        r = outcome["rating"]
        body = (
            f"PEAD RATING: {r['rating']} (composite {r['composite']})\n"
            f"Financials: {r['financials_score']}  |  Technical: {r['technical_score']}\n"
            f"Source: raw NSE filing, extracted+sanity-checked live"
        )
    else:
        body = f"SKIPPED — {outcome['reason']}"

    return f"{header}\n\n{body}\n\n{timing}"


def score_filing(filing):
    """Returns {"ok": True, "rating": {...}} or {"ok": False, "reason": str}."""
    pdf_path = Path(f"/tmp/nse_live_{filing['symbol']}_{filing['seq_id']}.pdf")
    try:
        _download_pdf(filing["attchmntFile"], pdf_path)
    except Exception as e:
        return {"ok": False, "reason": f"could not download filing PDF: {e}"}

    try:
        financials = nrp.extract_quarters(str(pdf_path))
    except ValueError as e:
        return {"ok": False, "reason": f"extraction/sanity check failed: {str(e)[:400]}"}

    card = {"company_name": filing["sm_name"], "nse_ticker": filing["symbol"], "financials": financials}
    try:
        result = rate_results.rate_card(card, check_guidance=False)
    except Exception as e:
        return {"ok": False, "reason": f"scoring failed (likely no tradeable symbol/price data): {e}"}

    return {"ok": True, "rating": result}


async def post_to_telegram(text):
    client = TelegramClient(config.TELEGRAM_SESSION_NAME, config.TELEGRAM_API_ID, config.TELEGRAM_API_HASH)
    await client.start()
    entity = await client.get_entity(TELEGRAM_TARGET)
    await client.send_message(entity, text)
    await client.disconnect()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    posted = _load_posted()

    filings = fetch_todays_results_filings()
    new_filings = [f for f in filings if f["seq_id"] not in posted]
    log.info("%d results filed today, %d new", len(filings), len(new_filings))

    for filing in new_filings:
        log.info("Processing %s (seq_id=%s, filed %s)", filing["symbol"], filing["seq_id"], filing["an_dt"])
        outcome = score_filing(filing)
        message = build_message(filing, outcome)
        asyncio.run(post_to_telegram(message))
        log.info("Posted %s: %s", filing["symbol"], "RATED" if outcome["ok"] else "SKIPPED")
        posted.add(filing["seq_id"])
        _save_posted(posted)


if __name__ == "__main__":
    main()
