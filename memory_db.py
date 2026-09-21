"""Long-term, per-user memory for /chat -- facts the model decides are worth
remembering across conversations (e.g. "likes the Tampa Bay Rays"), stored
in a local SQLite file. Separate from llmask.py's conversation history,
which is short-term and channel-scoped; this is long-term and follows the
user across channels/DMs until they clear it with /forgetme.

Never sent anywhere outside this bot process, and never used to train or
fine-tune anything -- it's just context fed back into the same self-hosted
Ollama instance at answer time, the same way conversation history already is.
"""
import os
import sqlite3
import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, 'memory.db')

MAX_FACTS_PER_USER = 50  # oldest facts drop off past this, so it can't grow forever


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS facts ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "user_id TEXT NOT NULL, "
        "fact TEXT NOT NULL, "
        "created_at TEXT NOT NULL)"
    )
    return conn


def add_fact(user_id, fact: str) -> None:
    """Saves a fact for this user, trimming their oldest facts beyond
    MAX_FACTS_PER_USER so one person can't accumulate an unbounded number."""
    user_id = str(user_id)
    with _connect() as conn:
        conn.execute(
            "INSERT INTO facts (user_id, fact, created_at) VALUES (?, ?, ?)",
            (user_id, fact, datetime.datetime.now(datetime.timezone.utc).isoformat()),
        )
        conn.execute(
            "DELETE FROM facts WHERE user_id = ? AND id NOT IN "
            "(SELECT id FROM facts WHERE user_id = ? ORDER BY id DESC LIMIT ?)",
            (user_id, user_id, MAX_FACTS_PER_USER),
        )


def get_facts(user_id) -> list[str]:
    """Returns everything remembered about this user, oldest first."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT fact FROM facts WHERE user_id = ? ORDER BY id", (str(user_id),)
        ).fetchall()
    return [row[0] for row in rows]


def clear_facts(user_id) -> int:
    """Deletes everything remembered about this user. Returns how many
    facts were deleted -- backs the /forgetme command."""
    with _connect() as conn:
        cursor = conn.execute("DELETE FROM facts WHERE user_id = ?", (str(user_id),))
        return cursor.rowcount
