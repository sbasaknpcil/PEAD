"""Run this once, yourself, in a real terminal to create the Telegram session file.

It will ask for your phone number, the login code Telegram sends you, and your
2FA password if you have one set. Do this interactively — never paste your
code/password into a chat with an AI assistant.
"""

from telethon.sync import TelegramClient

import config

if __name__ == "__main__":
    if not config.TELEGRAM_API_ID or not config.TELEGRAM_API_HASH:
        raise SystemExit("Set TELEGRAM_API_ID and TELEGRAM_API_HASH in .env first.")

    with TelegramClient(config.TELEGRAM_SESSION_NAME, config.TELEGRAM_API_ID, config.TELEGRAM_API_HASH) as client:
        me = client.get_me()
        print(f"Logged in as {me.first_name} (@{me.username}). Session saved to "
              f"{config.TELEGRAM_SESSION_NAME}.session — you can now run main.py.")
