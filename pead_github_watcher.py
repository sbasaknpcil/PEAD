"""Polling equivalent of main.py's always-on Telegram listener, for
environments that can't hold a persistent connection open between runs — a
GitHub Actions scheduled workflow starts a fresh runner each fire and tears it
down when the job exits, so client.run_until_disconnected() has nothing to
run inside. This connects, catches up on anything missed, checks exits, then
disconnects. Safe to call repeatedly on a schedule — dedup reuses the same
storage.is_message_processed() the event-driven listener already relies on.
"""
import asyncio
import logging

from telethon import TelegramClient

import config
import earnings_pulse_listener
import portfolio
import storage

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("github_watcher")

# Recent messages to scan per run — comfortably above what a 10-minute polling
# interval should ever need to catch up on, even after a missed run or two.
MESSAGE_SCAN_LIMIT = 200


async def poll_once():
    client = TelegramClient(config.TELEGRAM_SESSION_NAME, config.TELEGRAM_API_ID, config.TELEGRAM_API_HASH)
    await client.start()
    # Without this, buy()/_sell() have no client to notify through and silently
    # skip the Telegram push — main.py wires this on startup, but this script
    # creates its own short-lived client each run and was missing it.
    portfolio.set_notify_client(client)

    channel = await earnings_pulse_listener.resolve_channel(client)
    messages = [m async for m in client.iter_messages(channel, limit=MESSAGE_SCAN_LIMIT)]

    # Oldest first, so tickers that got rated more than once recently still buy
    # in the same order the ratings actually posted, not scan order (newest-first).
    new_count = 0
    for message in reversed(messages):
        if not storage.is_message_processed(message.chat_id, message.id):
            new_count += 1
        await earnings_pulse_listener._handle_message(message)

    log.info("Scanned %d recent messages, %d were new", len(messages), new_count)

    # Exit checks (and their SELL notifications) need to run while the client
    # is still connected — _notify() pushes through it, so this can't happen
    # after disconnect the way it did before.
    portfolio.check_exits_all_variants()
    # _notify() fires notifications as fire-and-forget asyncio tasks (asyncio.create_task),
    # not awaited inline — give them a beat to actually send before the client tears down.
    await asyncio.sleep(2)

    await client.disconnect()


def main():
    storage.init_db()
    asyncio.run(poll_once())


if __name__ == "__main__":
    main()
