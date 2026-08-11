import asyncio
import logging

from telethon import TelegramClient

import config
import earnings_pulse_listener
import portfolio
import storage
import telegram_listener

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("main")


async def position_check_loop():
    while True:
        try:
            portfolio.check_exits_all_variants()
        except Exception:
            log.exception("Error while checking open positions for exits")
        await asyncio.sleep(config.POSITION_CHECK_INTERVAL_SECONDS)


RECONNECT_WAIT_SECONDS = 30


async def run_client():
    client = TelegramClient(
        config.TELEGRAM_SESSION_NAME,
        config.TELEGRAM_API_ID,
        config.TELEGRAM_API_HASH,
        connection_retries=None,  # retry forever instead of giving up after 5 attempts
        retry_delay=15,
    )
    await client.start()
    portfolio.set_notify_client(client)

    group_entity = await telegram_listener.resolve_group(client)
    telegram_listener.register(client, group_entity)

    confirmation_entity = await earnings_pulse_listener.resolve_channel(client)
    earnings_pulse_listener.register(client, confirmation_entity)

    variant_summary = ", ".join(
        f"{v['label']}=₹{storage.get_cash(v['variant']):,.2f}" for v in config.STRATEGY_VARIANTS
    )
    tier_ratings = ", ".join(v["rating"] for v in config.STRATEGY_VARIANTS)
    log.info(
        "Buying each of '%s' from '%s' into its own tier portfolio during market hours "
        "(%02d:%02d-%02d:%02d IST); PEAD topic %s of group %s logged for reference only "
        "(paper trading; %s)",
        tier_ratings, config.CONFIRMATION_CHANNEL,
        config.MARKET_OPEN_IST_HOUR, config.MARKET_OPEN_IST_MINUTE,
        config.MARKET_CLOSE_IST_HOUR, config.MARKET_CLOSE_IST_MINUTE,
        config.TELEGRAM_PEAD_TOPIC_ID, config.TELEGRAM_GROUP_ID, variant_summary,
    )

    await client.run_until_disconnected()


async def main():
    if not config.TELEGRAM_API_ID or not config.TELEGRAM_API_HASH:
        raise SystemExit("Set TELEGRAM_API_ID / TELEGRAM_API_HASH in .env")

    storage.init_db()
    asyncio.create_task(position_check_loop())

    # Outer resilience loop: connection_retries=None handles ordinary outages
    # internally, but if the client ever gives up and disconnects anyway
    # (e.g. an outage longer than expected), restart instead of exiting —
    # this process needs to survive unattended for days at a time.
    while True:
        try:
            await run_client()
        except Exception:
            log.exception(
                "Telegram client stopped unexpectedly, reconnecting in %ds",
                RECONNECT_WAIT_SECONDS,
            )
            await asyncio.sleep(RECONNECT_WAIT_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
