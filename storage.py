import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import config

IST = timezone(timedelta(hours=5, minutes=30))


def _bare_ticker(ticker):
    return ticker.upper().replace(".NS", "").replace(".BO", "")


def _same_ist_day(iso_timestamp, now=None):
    now = now or datetime.now(timezone.utc)
    then = datetime.fromisoformat(iso_timestamp)
    return then.astimezone(IST).date() == now.astimezone(IST).date()

SCHEMA = """
CREATE TABLE IF NOT EXISTS portfolio (
    variant TEXT PRIMARY KEY,
    cash REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS positions (
    variant TEXT NOT NULL,
    ticker TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    entry_price REAL NOT NULL,
    entry_time TEXT NOT NULL,
    peak_price REAL NOT NULL,
    PRIMARY KEY (variant, ticker)
);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    variant TEXT NOT NULL,
    ticker TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    price REAL NOT NULL,
    time TEXT NOT NULL,
    reason TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS processed_messages (
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    processed_at TEXT NOT NULL,
    PRIMARY KEY (chat_id, message_id)
);
"""

# The variant every pre-existing row (from when the bot ran a single portfolio)
# is migrated into - it was running with a 2% trailing stop, so that's the
# variant its real cash/position/trade history belongs to.
LEGACY_VARIANT = "sl_2pct"


@contextmanager
def _conn():
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _migrate_portfolio_table(conn):
    """Old schema was a single row (id=1). The live bot now runs every configured
    strategy variant as its own fully independent paper portfolio, so this becomes
    variant-keyed. The old single portfolio's real cash carries over to
    LEGACY_VARIANT rather than being reset - it reflects actual trading history."""
    cols = [row[1] for row in conn.execute("PRAGMA table_info(portfolio)").fetchall()]
    if not cols:
        return  # fresh DB, table doesn't exist yet
    if "variant" in cols:
        return  # already on the current schema

    old_row = conn.execute("SELECT cash FROM portfolio WHERE id = 1").fetchone()
    conn.execute("DROP TABLE portfolio")
    conn.execute("CREATE TABLE portfolio (variant TEXT PRIMARY KEY, cash REAL NOT NULL)")
    if old_row is not None:
        conn.execute(
            "INSERT INTO portfolio (variant, cash) VALUES (?, ?)",
            (LEGACY_VARIANT, old_row["cash"]),
        )


def _migrate_positions_table(conn):
    """Old schemas had stop_loss_price/target_price (fixed exits), then
    peak_price + pead_score (PEAD-gated trailing stop), then a single-portfolio
    peak_price-only schema. The current strategy runs multiple parallel portfolio
    variants, so positions are now keyed by (variant, ticker). Migrate any existing
    open positions into LEGACY_VARIANT rather than losing them on a schema change."""
    cols = [row[1] for row in conn.execute("PRAGMA table_info(positions)").fetchall()]
    if not cols:
        return  # fresh DB, table doesn't exist yet
    if "variant" in cols:
        return  # already on the current schema

    old_rows = conn.execute("SELECT * FROM positions").fetchall()
    conn.execute("DROP TABLE positions")
    conn.execute(
        """CREATE TABLE positions (
            variant TEXT NOT NULL,
            ticker TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            entry_price REAL NOT NULL,
            entry_time TEXT NOT NULL,
            peak_price REAL NOT NULL,
            PRIMARY KEY (variant, ticker)
        )"""
    )
    for row in old_rows:
        cols_keys = row.keys()
        peak = row["peak_price"] if "peak_price" in cols_keys else row["entry_price"]
        conn.execute(
            """INSERT INTO positions (variant, ticker, quantity, entry_price, entry_time, peak_price)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (LEGACY_VARIANT, row["ticker"], row["quantity"], row["entry_price"], row["entry_time"], peak),
        )


def _migrate_trades_table(conn):
    """Old schema had no variant column - all historical trades happened under the
    single portfolio that used to exist, so they belong to LEGACY_VARIANT."""
    cols = [row[1] for row in conn.execute("PRAGMA table_info(trades)").fetchall()]
    if not cols:
        return  # fresh DB, table doesn't exist yet
    if "variant" in cols:
        return  # already on the current schema

    old_rows = conn.execute("SELECT * FROM trades").fetchall()
    conn.execute("DROP TABLE trades")
    conn.execute(
        """CREATE TABLE trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            variant TEXT NOT NULL,
            ticker TEXT NOT NULL,
            side TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            price REAL NOT NULL,
            time TEXT NOT NULL,
            reason TEXT NOT NULL
        )"""
    )
    for row in old_rows:
        conn.execute(
            """INSERT INTO trades (variant, ticker, side, quantity, price, time, reason)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (LEGACY_VARIANT, row["ticker"], row["side"], row["quantity"], row["price"], row["time"], row["reason"]),
        )


def _drop_pending_signal_tables(conn):
    """pending_pead_signals/excellent_signals backed the old two-channel confirmation
    match; the single-trigger strategy (buy on an earnings_pulse rating alone) has no
    pending state to track. Drop them - only ephemeral coordination state, not trade
    history, so there's nothing worth migrating."""
    conn.execute("DROP TABLE IF EXISTS pending_pead_signals")
    conn.execute("DROP TABLE IF EXISTS excellent_signals")


def init_db():
    with _conn() as conn:
        _migrate_portfolio_table(conn)
        _migrate_positions_table(conn)
        _migrate_trades_table(conn)
        _drop_pending_signal_tables(conn)
        conn.executescript(SCHEMA)
        for v in config.STRATEGY_VARIANTS:
            row = conn.execute(
                "SELECT cash FROM portfolio WHERE variant = ?", (v["variant"],)
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO portfolio (variant, cash) VALUES (?, ?)",
                    (v["variant"], config.STARTING_CAPITAL_INR),
                )


def get_cash(variant):
    with _conn() as conn:
        return conn.execute("SELECT cash FROM portfolio WHERE variant = ?", (variant,)).fetchone()["cash"]


def set_cash(variant, new_cash):
    with _conn() as conn:
        conn.execute("UPDATE portfolio SET cash = ? WHERE variant = ?", (new_cash, variant))


def get_open_positions(variant):
    with _conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM positions WHERE variant = ?", (variant,)).fetchall()]


def get_position(variant, ticker):
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM positions WHERE variant = ? AND ticker = ?", (variant, ticker)
        ).fetchone()
        return dict(row) if row else None


def open_position(variant, ticker, quantity, entry_price):
    with _conn() as conn:
        conn.execute(
            """INSERT INTO positions
               (variant, ticker, quantity, entry_price, entry_time, peak_price)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                variant,
                ticker,
                quantity,
                entry_price,
                datetime.now(timezone.utc).isoformat(),
                entry_price,
            ),
        )


def update_peak_price(variant, ticker, peak_price):
    with _conn() as conn:
        conn.execute(
            "UPDATE positions SET peak_price = ? WHERE variant = ? AND ticker = ?",
            (peak_price, variant, ticker),
        )


def close_position(variant, ticker):
    with _conn() as conn:
        conn.execute("DELETE FROM positions WHERE variant = ? AND ticker = ?", (variant, ticker))


def record_trade(variant, ticker, side, quantity, price, reason):
    with _conn() as conn:
        conn.execute(
            """INSERT INTO trades (variant, ticker, side, quantity, price, time, reason)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (variant, ticker, side, quantity, price, datetime.now(timezone.utc).isoformat(), reason),
        )


def is_message_processed(chat_id, message_id):
    with _conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM processed_messages WHERE chat_id = ? AND message_id = ?",
            (chat_id, message_id),
        ).fetchone()
        return row is not None


def mark_message_processed(chat_id, message_id):
    with _conn() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO processed_messages (chat_id, message_id, processed_at)
               VALUES (?, ?, ?)""",
            (chat_id, message_id, datetime.now(timezone.utc).isoformat()),
        )
