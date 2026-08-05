import asyncio

from telethon import TelegramClient

import backtest
import config


async def main():
    client = TelegramClient(config.TELEGRAM_SESSION_NAME, config.TELEGRAM_API_ID, config.TELEGRAM_API_HASH)
    await client.start()

    signals = await backtest.fetch_signals(client)
    await client.disconnect()

    print(f"\n{len(signals)} PEAD cards found in the last {config.BACKTEST_LOOKBACK_DAYS} days\n")

    qualifying = [s for s in signals if (s["card"].get("pead_score") or 0) > config.PEAD_BUY_SCORE_MIN]
    qualifying.sort(key=lambda s: -(s["card"].get("pead_score") or 0))

    print(f"Score > {config.PEAD_BUY_SCORE_MIN}: {len(qualifying)} companies\n")
    print(f"{'Ticker':<14}{'Quarter':<10}{'Score':>7}  Date")
    for s in qualifying:
        card = s["card"]
        print(f"{card.get('nse_ticker', ''):<14}{card.get('quarter', ''):<10}{card.get('pead_score'):>7}  {s['date'].date()}")


if __name__ == "__main__":
    asyncio.run(main())
