import logging

from telethon import events
from telethon.tl.types import PeerChannel

import config
import portfolio
import rules
import storage
import vision_parser

log = logging.getLogger("telegram_listener")


async def resolve_group(client):
    return await client.get_entity(PeerChannel(config.TELEGRAM_GROUP_ID))


def _topic_id_of(message):
    reply = message.reply_to
    if reply is None:
        return None
    return getattr(reply, "reply_to_top_id", None) or reply.reply_to_msg_id


async def is_from_signal_source(client, message):
    if _topic_id_of(message) != config.TELEGRAM_PEAD_TOPIC_ID:
        return False
    sender = await message.get_sender()
    username = getattr(sender, "username", None) or ""
    return username.lower() == config.TELEGRAM_SIGNAL_SENDER.lower()


async def _handle_card(client, message):
    if storage.is_message_processed(message.id):
        return
    storage.mark_message_processed(message.id)

    if not message.photo:
        return
    if not await is_from_signal_source(client, message):
        return

    image_path = await client.download_media(message, file=str(config.IMAGE_CACHE_DIR) + "/")
    log.info("Downloaded PEAD card: %s", image_path)

    try:
        card = vision_parser.extract_card(image_path)
    except Exception:
        log.exception("Failed to parse card %s", image_path)
        return

    ticker = card.get("nse_ticker")
    if not ticker:
        log.warning("Card had no NSE ticker, skipping: %s", card)
        return

    should_buy, reason = rules.decide_buy(card)
    log.info("%s: %s", ticker, reason)
    if should_buy:
        portfolio.buy(ticker, card.get("pead_score"))


def register(client, group_entity):
    @client.on(events.NewMessage(chats=group_entity))
    async def _handler(event):
        try:
            await _handle_card(client, event.message)
        except Exception:
            log.exception("Error handling message %s", event.message.id)
