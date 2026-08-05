import logging
import math

import config
import price_feed
import rules
import storage

log = logging.getLogger("portfolio")


def buy(ticker, pead_score):
    if storage.get_position(ticker):
        log.info("Skipping %s: position already open", ticker)
        return False

    open_positions = storage.get_open_positions()
    if len(open_positions) >= config.MAX_OPEN_POSITIONS:
        log.info("Skipping %s: max open positions (%d) reached", ticker, config.MAX_OPEN_POSITIONS)
        return False

    cash = storage.get_cash()
    price = price_feed.get_last_price(ticker)

    if config.REQUIRE_ABOVE_200DMA:
        dma_200 = price_feed.get_200dma(ticker)
        if not rules.is_above_200dma(price, dma_200):
            log.info("Skipping %s: price %.2f not above 200DMA %s", ticker, price, dma_200)
            return False

    allocation = min(cash, config.MAX_POSITION_VALUE_INR)
    quantity = math.floor(allocation / price)
    if quantity < 1:
        log.info("Skipping %s: allocation %.2f too small at price %.2f", ticker, allocation, price)
        return False

    cost = quantity * price
    if cost > cash:
        log.info("Skipping %s: insufficient cash (%.2f < %.2f)", ticker, cash, cost)
        return False

    stop_loss_price = price * (1 - config.STOP_LOSS_PCT)
    target_price = price * (1 + config.TARGET_PCT)

    storage.open_position(ticker, quantity, price, stop_loss_price, target_price, pead_score)
    storage.set_cash(cash - cost)
    storage.record_trade(ticker, "BUY", quantity, price, f"pead_score={pead_score}")

    log.info(
        "BUY %s x%d @ %.2f (stop %.2f / target %.2f)",
        ticker, quantity, price, stop_loss_price, target_price,
    )
    return True


def _sell(position, price, reason):
    ticker = position["ticker"]
    quantity = position["quantity"]
    proceeds = quantity * price

    storage.close_position(ticker)
    storage.set_cash(storage.get_cash() + proceeds)
    storage.record_trade(ticker, "SELL", quantity, price, reason)

    log.info("SELL %s x%d @ %.2f (%s)", ticker, quantity, price, reason)


def check_exits():
    """Poll all open positions and exit any that hit stop-loss or target."""
    for position in storage.get_open_positions():
        ticker = position["ticker"]
        try:
            price = price_feed.get_last_price(ticker)
        except Exception:
            log.exception("Could not fetch price for %s while checking exits", ticker)
            continue

        if price <= position["stop_loss_price"]:
            _sell(position, price, "stop-loss")
        elif price >= position["target_price"]:
            _sell(position, price, "target")
