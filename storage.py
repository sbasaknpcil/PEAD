import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

import config

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
    stop_loss_price REAL NOT NULL,
    target_price REAL NOT NULL,
    pead_score REAL NOT NULL
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
    message_id INTEGER PRIMARY KEY,
    processed_at TEXT NOT NULL
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


def init_db():
    with _conn() as conn:
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


def open_position(ticker, quantity, entry_price, stop_loss_price, target_price, pead_score):
    with _conn() as conn:
        conn.execute(
            """INSERT INTO positions
               (ticker, quantity, entry_price, entry_time, stop_loss_price, target_price, pead_score)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                ticker,
                quantity,
                entry_price,
                datetime.now(timezone.utc).isoformat(),
                stop_loss_price,
                target_price,
                pead_score,
            ),
        )


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


def is_message_processed(message_id):
    with _conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM processed_messages WHERE message_id = ?", (message_id,)
        ).fetchone()
        return row is not None


def mark_message_processed(message_id):
    with _conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO processed_messages (message_id, processed_at) VALUES (?, ?)",
            (message_id, datetime.now(timezone.utc).isoformat()),
        )
