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
    id INTEGER PRIMARY KEY CHECK (id = 1),
    cash REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS positions (
    ticker TEXT PRIMARY KEY,
    quantity INTEGER NOT NULL,
    entry_price REAL NOT NULL,
    entry_time TEXT NOT NULL,
    peak_price REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
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


@contextmanager
def _conn():
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _migrate_positions_table(conn):
    """Old schemas had stop_loss_price/target_price (fixed exits), then
    peak_price + pead_score (PEAD-gated trailing stop). The current strategy buys
    on an earnings_pulse rating alone, so pead_score no longer applies. Migrate any
    existing open positions rather than losing them on a schema change."""
    cols = [row[1] for row in conn.execute("PRAGMA table_info(positions)").fetchall()]
    if not cols:
        return  # fresh DB, table doesn't exist yet
    if "peak_price" in cols and "pead_score" not in cols:
        return  # already on the current schema

    old_rows = conn.execute("SELECT * FROM positions").fetchall()
    conn.execute("DROP TABLE positions")
    conn.execute(
        """CREATE TABLE positions (
            ticker TEXT PRIMARY KEY,
            quantity INTEGER NOT NULL,
            entry_price REAL NOT NULL,
            entry_time TEXT NOT NULL,
            peak_price REAL NOT NULL
        )"""
    )
    for row in old_rows:
        peak = row["peak_price"] if "peak_price" in row.keys() else row["entry_price"]
        conn.execute(
            """INSERT INTO positions (ticker, quantity, entry_price, entry_time, peak_price)
               VALUES (?, ?, ?, ?, ?)""",
            (row["ticker"], row["quantity"], row["entry_price"], row["entry_time"], peak),
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
        _migrate_positions_table(conn)
        _drop_pending_signal_tables(conn)
        conn.executescript(SCHEMA)
        row = conn.execute("SELECT cash FROM portfolio WHERE id = 1").fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO portfolio (id, cash) VALUES (1, ?)",
                (config.STARTING_CAPITAL_INR,),
            )


def get_cash():
    with _conn() as conn:
        return conn.execute("SELECT cash FROM portfolio WHERE id = 1").fetchone()["cash"]


def set_cash(new_cash):
    with _conn() as conn:
        conn.execute("UPDATE portfolio SET cash = ? WHERE id = 1", (new_cash,))


def get_open_positions():
    with _conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM positions").fetchall()]


def get_position(ticker):
    with _conn() as conn:
        row = conn.execute("SELECT * FROM positions WHERE ticker = ?", (ticker,)).fetchone()
        return dict(row) if row else None


def open_position(ticker, quantity, entry_price):
    with _conn() as conn:
        conn.execute(
            """INSERT INTO positions
               (ticker, quantity, entry_price, entry_time, peak_price)
               VALUES (?, ?, ?, ?, ?)""",
            (
                ticker,
                quantity,
                entry_price,
                datetime.now(timezone.utc).isoformat(),
                entry_price,
            ),
        )


def update_peak_price(ticker, peak_price):
    with _conn() as conn:
        conn.execute("UPDATE positions SET peak_price = ? WHERE ticker = ?", (peak_price, ticker))


def close_position(ticker):
    with _conn() as conn:
        conn.execute("DELETE FROM positions WHERE ticker = ?", (ticker,))


def record_trade(ticker, side, quantity, price, reason):
    with _conn() as conn:
        conn.execute(
            """INSERT INTO trades (ticker, side, quantity, price, time, reason)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (ticker, side, quantity, price, datetime.now(timezone.utc).isoformat(), reason),
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


