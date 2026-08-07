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
    peak_price REAL NOT NULL,
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
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    processed_at TEXT NOT NULL,
    PRIMARY KEY (chat_id, message_id)
);

CREATE TABLE IF NOT EXISTS pending_pead_signals (
    ticker TEXT PRIMARY KEY,
    pead_score REAL NOT NULL,
    received_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS excellent_signals (
    ticker TEXT PRIMARY KEY,
    received_at TEXT NOT NULL
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
    """Old schema had stop_loss_price/target_price (fixed exits); the new
    same-day trailing-stop strategy uses peak_price instead. Migrate any
    existing open positions rather than losing them on a schema change."""
    cols = [row[1] for row in conn.execute("PRAGMA table_info(positions)").fetchall()]
    if not cols or "peak_price" in cols:
        return  # fresh DB (table doesn't exist yet) or already migrated

    old_rows = conn.execute("SELECT * FROM positions").fetchall()
    conn.execute("DROP TABLE positions")
    conn.execute(
        """CREATE TABLE positions (
            ticker TEXT PRIMARY KEY,
            quantity INTEGER NOT NULL,
            entry_price REAL NOT NULL,
            entry_time TEXT NOT NULL,
            peak_price REAL NOT NULL,
            pead_score REAL NOT NULL
        )"""
    )
    for row in old_rows:
        conn.execute(
            """INSERT INTO positions (ticker, quantity, entry_price, entry_time, peak_price, pead_score)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (row["ticker"], row["quantity"], row["entry_price"], row["entry_time"],
             row["entry_price"], row["pead_score"]),  # peak_price starts at entry_price, self-corrects on next check
        )


def init_db():
    with _conn() as conn:
        _migrate_positions_table(conn)
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


def open_position(ticker, quantity, entry_price, pead_score):
    with _conn() as conn:
        conn.execute(
            """INSERT INTO positions
               (ticker, quantity, entry_price, entry_time, peak_price, pead_score)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                ticker,
                quantity,
                entry_price,
                datetime.now(timezone.utc).isoformat(),
                entry_price,
                pead_score,
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


def add_pending_pead(ticker, pead_score):
    with _conn() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO pending_pead_signals (ticker, pead_score, received_at)
               VALUES (?, ?, ?)""",
            (_bare_ticker(ticker), pead_score, datetime.now(timezone.utc).isoformat()),
        )


def get_pending_pead_today(ticker):
    """The pending PEAD signal for this ticker, if it arrived today (IST) — else None."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM pending_pead_signals WHERE ticker = ?", (_bare_ticker(ticker),)
        ).fetchone()
    if row is None or not _same_ist_day(row["received_at"]):
        return None
    return dict(row)


def remove_pending_pead(ticker):
    with _conn() as conn:
        conn.execute("DELETE FROM pending_pead_signals WHERE ticker = ?", (_bare_ticker(ticker),))


def record_excellent_signal(ticker):
    with _conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO excellent_signals (ticker, received_at) VALUES (?, ?)",
            (_bare_ticker(ticker), datetime.now(timezone.utc).isoformat()),
        )


def has_excellent_today(ticker):
    """True if this ticker got an 'excellent' signal today (IST)."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT received_at FROM excellent_signals WHERE ticker = ?", (_bare_ticker(ticker),)
        ).fetchone()
    return row is not None and _same_ist_day(row["received_at"])
