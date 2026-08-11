"""Fetch concall transcript links (and PDFs) for a ticker from Screener.in.

Screener's company page lists concalls newest-first, each with an optional
"Transcript" link (raw PDF, usually hosted on bseindia.com) plus PPT/REC/AI
Summary links. Not every quarter has a raw transcript uploaded yet — in that
case Screener only shows the AI Summary and the transcript link is absent.
"""
import argparse
import logging
import re
from pathlib import Path

import requests
from bs4 import BeautifulSoup

import config

log = logging.getLogger("concall_fetcher")

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
TRANSCRIPT_DIR = config.BASE_DIR / "concall_transcripts"


def _page_url(ticker, consolidated=True):
    suffix = "consolidated/" if consolidated else ""
    return f"https://www.screener.in/company/{ticker.upper()}/{suffix}"


def fetch_concalls(ticker, consolidated=True):
    """Returns a list of dicts (newest first): period, transcript_url, ppt_url, rec_url."""
    url = _page_url(ticker, consolidated)
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=15)
    if resp.status_code == 404 and consolidated:
        # Some companies (no consolidated financials) only have the standalone page.
        return fetch_concalls(ticker, consolidated=False)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    container = soup.find("div", class_="concalls")
    if container is None:
        log.warning("No concalls section found for %s (%s)", ticker, url)
        return []

    concalls = []
    for item in container.select("ul.list-links > li"):
        period_tag = item.find("div", class_="ink-600")
        if period_tag is None:
            continue
        period = period_tag.get_text(strip=True)

        transcript_url = None
        ppt_url = None
        rec_url = None
        for link in item.find_all(["a", "div", "button"], class_="concall-link"):
            label = link.get_text(strip=True)
            href = link.get("href")
            if label == "Transcript" and href:
                transcript_url = href
            elif label == "PPT" and href:
                ppt_url = href
            elif label == "REC" and href:
                rec_url = href

        concalls.append(
            {
                "period": period,
                "transcript_url": transcript_url,
                "ppt_url": ppt_url,
                "rec_url": rec_url,
            }
        )
    return concalls


def _safe_filename(period):
    return re.sub(r"[^A-Za-z0-9]+", "_", period).strip("_")


def download_transcript(ticker, concall, dest_dir=None):
    """Downloads a concall's transcript PDF to disk. Returns the path, or None if
    this quarter has no raw transcript uploaded to Screener yet."""
    if not concall["transcript_url"]:
        return None

    dest_dir = Path(dest_dir) if dest_dir else TRANSCRIPT_DIR / ticker.upper()
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / f"{_safe_filename(concall['period'])}.pdf"

    if dest_path.exists():
        return dest_path

    resp = requests.get(concall["transcript_url"], headers={"User-Agent": USER_AGENT}, timeout=30)
    resp.raise_for_status()
    dest_path.write_bytes(resp.content)
    log.info("Saved %s transcript (%s) to %s", ticker, concall["period"], dest_path)
    return dest_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ticker", help="NSE ticker, e.g. UNIPARTS")
    parser.add_argument("--limit", type=int, default=4, help="how many recent quarters to fetch (default 4)")
    parser.add_argument("--download", action="store_true", help="download transcript PDFs, not just list them")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    concalls = fetch_concalls(args.ticker)[: args.limit]
    if not concalls:
        print(f"No concalls found for {args.ticker}")
        return

    for concall in concalls:
        status = concall["transcript_url"] or "no transcript uploaded yet"
        print(f"{concall['period']}: {status}")
        if args.download and concall["transcript_url"]:
            download_transcript(args.ticker, concall)


if __name__ == "__main__":
    main()
