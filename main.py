import asyncio
import logging

from telethon import TelegramClient

import config
import portfolio
import storage
import telegram_listener

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("main")


async def position_check_loop():
    while True:
        try:
            portfolio.check_exits()
        except Exception:
            log.exception("Error while checking open positions for exits")
        await asyncio.sleep(config.POSITION_CHECK_INTERVAL_SECONDS)


async def main():
    if not config.TELEGRAM_API_ID or not config.TELEGRAM_API_HASH:
        raise SystemExit("Set TELEGRAM_API_ID / TELEGRAM_API_HASH in .env")
    if not config.GEMINI_API_KEY:
        raise SystemExit("Set GEMINI_API_KEY in .env")

    storage.init_db()

    client = TelegramClient(config.TELEGRAM_SESSION_NAME, config.TELEGRAM_API_ID, config.TELEGRAM_API_HASH)
    await client.start()

    group_entity = await telegram_listener.resolve_group(client)
    telegram_listener.register(client, group_entity)
    log.info(
        "Listening for PEAD cards in topic %s of group %s (paper trading, cash=%.2f)",
        config.TELEGRAM_PEAD_TOPIC_ID, config.TELEGRAM_GROUP_ID, storage.get_cash(),
    )

    asyncio.create_task(position_check_loop())
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
