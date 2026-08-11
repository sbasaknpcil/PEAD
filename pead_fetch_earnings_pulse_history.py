"""One-off: pull raw Earnings Pulse channel history for a given IST date and
parse every ticker's rating (not just 'Excellent' — that's all storage.py
persists during live operation). Read-only, uses the existing logged-in
Telegram session.
"""
import argparse
import asyncio
from datetime import datetime, timedelta

from telethon import TelegramClient

import config
from earnings_pulse_listener import _parse_rating


async def fetch(date_str):
    target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    start = datetime.combine(target_date, datetime.min.time()).astimezone(tz=None)
    end = start + timedelta(days=1)

    client = TelegramClient(config.TELEGRAM_SESSION_NAME, config.TELEGRAM_API_ID, config.TELEGRAM_API_HASH)
    await client.start()

    channel = await client.get_entity(config.CONFIRMATION_CHANNEL)

    results = []
    async for message in client.iter_messages(channel, offset_date=end, reverse=False):
        if message.date.astimezone().date() < target_date:
            break
        if message.date.astimezone().date() > target_date:
            continue
        parsed = _parse_rating(message.text)
        if parsed:
            ticker, rating = parsed
            results.append((message.date.isoformat(), ticker, rating))

    await client.disconnect()
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("date", help="IST date, YYYY-MM-DD")
    args = parser.parse_args()

    results = asyncio.run(fetch(args.date))
    results.sort()
    for ts, ticker, rating in results:
        print(f"{ts}\t{ticker}\t{rating}")
    print(f"\n{len(results)} rated messages found for {args.date}", flush=True)


if __name__ == "__main__":
    main()
