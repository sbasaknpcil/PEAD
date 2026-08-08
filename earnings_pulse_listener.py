import logging
import re

from telethon import events

import config
import portfolio
import storage

log = logging.getLogger("earnings_pulse_listener")

# e.g. "#ELANTAS - 🏆🔥 Excellent Results - 12 seconds ago" -> ("ELANTAS", "Excellent")
RATING_RE = re.compile(r"^#(\w+)\s*-\s*.*?([A-Za-z]+)\s+Results", re.UNICODE)


async def resolve_channel(client):
    return await client.get_entity(config.CONFIRMATION_CHANNEL)


def _parse_rating(text):
    if not text:
        return None
    match = RATING_RE.search(text)
    if not match:
        return None
    return match.group(1).upper(), match.group(2)


async def _handle_message(message):
    if storage.is_message_processed(message.chat_id, message.id):
        return
    storage.mark_message_processed(message.chat_id, message.id)

    parsed = _parse_rating(message.text)
    if parsed is None:
        return
    ticker, rating = parsed

    log.info("%s: %s", ticker, rating)
    if rating.lower() == config.BUY_RATING_LABEL.lower():
        portfolio.buy(ticker)


def register(client, channel_entity):
    @client.on(events.NewMessage(chats=channel_entity))
    async def _handler(event):
        try:
            await _handle_message(event.message)
        except Exception:
            log.exception("Error handling message %s", event.message.id)
