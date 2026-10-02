"""Per-guild configuration of which channel each automatic announcement
feature posts into -- set via /setchannel instead of hardcoding channel
IDs in source, which previously meant reconfiguring where something
posted needed a code change, a commit, and a deploy. Stored in a local
SQLite file, same pattern as memory_db.py's per-user facts.
"""
import os
import sqlite3

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, 'channel_config.db')

# feature key -> human-readable label, shown in /setchannel's dropdown.
# Add an entry here whenever a new auto-announcement feature is wired up
# to read its destination channel from here instead of a hardcoded ID.
FEATURES = {
    'sotd': "Song of the Day",
    'manga_comics': "Manga/comic release announcements",
    'f1_updates': "F1 session/race updates",
    'jra_updates': "Japanese horse racing (JRA) race previews, odds and results",
    'game_announcements': "Nintendo Direct / State of Play / Xbox Showcase announcements",
    'steam_sales': "Steam sales on popular titles",
}


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS channels ("
        "guild_id TEXT NOT NULL, "
        "feature TEXT NOT NULL, "
        "channel_id TEXT NOT NULL, "
        "PRIMARY KEY (guild_id, feature))"
    )
    # Which message an announcement went out as in each channel, for one
    # that's edited in place later -- like an F1 results board filling in
    # its times. Kept here rather than in memory so a restart between the
    # post and its edit doesn't post the whole thing again.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS posts ("
        "post_key TEXT NOT NULL, "
        "channel_id TEXT NOT NULL, "
        "message_id TEXT NOT NULL, "
        "PRIMARY KEY (post_key, channel_id))"
    )
    return conn


def remember_post(post_key: str, channel_id, message_id) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO posts (post_key, channel_id, message_id) VALUES (?, ?, ?) "
            "ON CONFLICT (post_key, channel_id) DO UPDATE SET message_id = excluded.message_id",
            (post_key, str(channel_id), str(message_id)),
        )


def get_post(post_key: str, channel_id) -> int | None:
    """The message an announcement went out as in this channel, or None."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT message_id FROM posts WHERE post_key = ? AND channel_id = ?",
            (post_key, str(channel_id)),
        ).fetchone()
    return int(row[0]) if row else None


def set_channel(guild_id, feature: str, channel_id) -> None:
    """Sets (or replaces) the channel a feature posts into for one guild."""
    with _connect() as conn:
        conn.execute(
            "INSERT INTO channels (guild_id, feature, channel_id) VALUES (?, ?, ?) "
            "ON CONFLICT (guild_id, feature) DO UPDATE SET channel_id = excluded.channel_id",
            (str(guild_id), feature, str(channel_id)),
        )


def get_channel(guild_id, feature: str) -> int | None:
    """Returns the configured channel id for this guild+feature, or None
    if it hasn't been set."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT channel_id FROM channels WHERE guild_id = ? AND feature = ?",
            (str(guild_id), feature),
        ).fetchone()
    return int(row[0]) if row else None


def get_for_guild(guild_id) -> dict[str, int]:
    """Returns {feature: channel_id} for every feature this guild has set
    up -- what /setchannel shows, so a feature that was never set up in a
    server can't be mistaken for one that's broken there."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT feature, channel_id FROM channels WHERE guild_id = ?", (str(guild_id),)
        ).fetchall()
    return {feature: int(channel_id) for feature, channel_id in rows}


def get_all_for_feature(feature: str) -> dict[int, int]:
    """Returns {guild_id: channel_id} for every guild that has configured
    this feature -- lets a background announcement loop fan a single
    result out to every guild that wants it, instead of one hardcoded
    destination."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT guild_id, channel_id FROM channels WHERE feature = ?", (feature,)
        ).fetchall()
    return {int(guild_id): int(channel_id) for guild_id, channel_id in rows}
