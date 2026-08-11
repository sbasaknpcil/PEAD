"""Posts a side-by-side comparison of the three tier portfolios (Excellent/
Great/Good) to Telegram — total equity, P&L vs starting capital, open
positions, and trade count per tier, so performance is comparable at a
glance. Safe to run anytime; reflects whatever's in the DB at that moment
(unrealized P&L on open positions uses the latest fetchable price).
"""
import asyncio
import logging

from telethon import TelegramClient

import config
import price_feed
import storage

log = logging.getLogger("portfolio_summary")

TELEGRAM_TARGET = "peadtest_sb"


def tier_stats(variant):
    cash = storage.get_cash(variant)
    positions = storage.get_open_positions(variant)

    open_value = 0.0
    position_lines = []
    for p in positions:
        try:
            price = price_feed.get_last_price(p["ticker"])
        except Exception:
            price = p["entry_price"]  # stale but better than dropping it from the total
        value = p["quantity"] * price
        open_value += value
        pnl_pct = (price / p["entry_price"] - 1) * 100
        position_lines.append(f"  {p['ticker']} x{p['quantity']} @ {p['entry_price']:.2f} -> {price:.2f} ({pnl_pct:+.1f}%)")

    equity = cash + open_value
    pnl = equity - config.STARTING_CAPITAL_INR
    pnl_pct = pnl / config.STARTING_CAPITAL_INR * 100

    with storage._conn() as conn:
        trade_count = conn.execute(
            "SELECT COUNT(*) AS c FROM trades WHERE variant = ?", (variant,)
        ).fetchone()["c"]

    return {
        "cash": cash, "open_value": open_value, "equity": equity,
        "pnl": pnl, "pnl_pct": pnl_pct, "open_count": len(positions),
        "trade_count": trade_count, "position_lines": position_lines,
    }


def build_message():
    rows = [(v["label"], tier_stats(v["variant"])) for v in config.STRATEGY_VARIANTS]
    rows.sort(key=lambda r: r[1]["pnl_pct"], reverse=True)

    lines = ["TIER PORTFOLIO COMPARISON", ""]
    for label, s in rows:
        lines.append(
            f"{label}: Rs.{s['equity']:,.0f} ({s['pnl_pct']:+.2f}%, Rs.{s['pnl']:+,.0f}) "
            f"| {s['open_count']} open, {s['trade_count']} trades total"
        )
        lines.extend(s["position_lines"])
        lines.append("")

    lines.append(f"Starting capital per tier: Rs.{config.STARTING_CAPITAL_INR:,.0f}")
    return "\n".join(lines)


async def post_to_telegram(text):
    client = TelegramClient(config.TELEGRAM_SESSION_NAME, config.TELEGRAM_API_ID, config.TELEGRAM_API_HASH)
    await client.start()
    entity = await client.get_entity(TELEGRAM_TARGET)
    await client.send_message(entity, text)
    await client.disconnect()


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    message = build_message()
    print(message)
    asyncio.run(post_to_telegram(message))


if __name__ == "__main__":
    main()
