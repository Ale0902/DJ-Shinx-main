"""Tracks Nintendo Direct, PlayStation State of Play, and Xbox Games
Showcase / Developer_Direct broadcasts through their full lifecycle:
announcement -> day-before/30-min-before reminders -> post-show recap.
Backs the /setchannel "game_announcements" feature.

All three sources are free, official-ish, and need no API key:
- Nintendo Life's RSS feed. Nintendo itself has no official RSS (every
  guessed newsroom/press feed URL 404s), and polling their Twitter/X
  account isn't viable without a paid API tier -- Nintendo Life reliably
  publishes "Nintendo Direct Confirmed For ..." articles as soon as one's
  announced, often before any video exists yet.
- The PlayStation Blog's RSS feed, which reliably titles these posts
  "State of Play announced ...".
- Xbox Wire's (news.xbox.com) official RSS feed, covering both the Xbox
  Games Showcase and Developer_Direct events.

Event state (has it aired yet, which reminders fired, was a recap found)
lives in SQLite rather than a flat "last seen id" -- unlike a simple
release checker, this needs to track each event across multiple checks
over days, not just dedupe the latest item.
"""
import os
import re
import sqlite3
import datetime
import requests
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo
from bs4 import BeautifulSoup

import llmask

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, 'game_news.db')

# Xbox Wire returns 403 to requests' default User-Agent (curl's default UA
# is let through fine, so this is UA-based bot filtering specifically) --
# sent on every request here as a matter of course, not just for Xbox.
USER_AGENT = "DJ-Shinx-Bot/1.0 (+https://github.com/Ale0902/DJ-Shinx-main)"

NINTENDO_LIFE_FEED_URL = 'https://www.nintendolife.com/feeds/latest'
PLAYSTATION_BLOG_FEED_URL = 'https://blog.playstation.com/feed/'
XBOX_WIRE_FEED_URL = 'https://news.xbox.com/en-us/feed/'

# How long to keep checking for a post-show recap article before giving up.
RECAP_SEARCH_DAYS = 5

# Per source: which RSS feed to watch, the regex that identifies an
# announcement article, the regex that identifies a later recap article
# on that same feed, and the header text used in the announcement message.
EVENT_SOURCES = {
    'nintendo_direct': {
        'feed_url': NINTENDO_LIFE_FEED_URL,
        'announce_re': re.compile(r'nintendo direct', re.IGNORECASE),
        'recap_re': re.compile(r'every (game|announcement)|all the (news|announcements)', re.IGNORECASE),
        'header': "NINTENDO DIRECT ALERT!",
    },
    'state_of_play': {
        'feed_url': PLAYSTATION_BLOG_FEED_URL,
        'announce_re': re.compile(r'state of play', re.IGNORECASE),
        'recap_re': re.compile(r'all announcements|everything announced|all the reveals', re.IGNORECASE),
        'header': "PLAYSTATION STATE OF PLAY ANNOUNCED!",
    },
    'xbox_showcase': {
        'feed_url': XBOX_WIRE_FEED_URL,
        'announce_re': re.compile(r'xbox games showcase|developer_direct', re.IGNORECASE),
        'recap_re': re.compile(r'all the news and announcements|recap', re.IGNORECASE),
        'header': "XBOX SHOWCASE ALERT!",
    },
}

# Common abbreviations these announcements use, mapped to a real IANA zone
# so zoneinfo resolves the correct UTC offset (including DST) for whatever
# date is given -- the LLM is only asked for the stated local time and
# this abbreviation, never to compute the offset itself.
TIMEZONE_ABBREVIATIONS = {
    'ET': 'America/New_York', 'EST': 'America/New_York', 'EDT': 'America/New_York',
    'PT': 'America/Los_Angeles', 'PST': 'America/Los_Angeles', 'PDT': 'America/Los_Angeles',
    'CT': 'America/Chicago', 'CST': 'America/Chicago', 'CDT': 'America/Chicago',
    'MT': 'America/Denver', 'MST': 'America/Denver', 'MDT': 'America/Denver',
    'BST': 'Europe/London', 'GMT': 'Europe/London', 'UTC': 'UTC',
    'CEST': 'Europe/Paris', 'CET': 'Europe/Paris',
    'JST': 'Asia/Tokyo',
    'AEST': 'Australia/Sydney', 'AEDT': 'Australia/Sydney',
}

BROADCAST_DATETIME_PROMPT = (
    "This is a gaming news snippet announcing an upcoming Nintendo Direct, "
    "PlayStation State of Play, or Xbox showcase broadcast, published "
    "around {published}. Find the exact date, time, and timezone it airs "
    "and reply in EXACTLY this format and nothing else:\n"
    "DATE: YYYY-MM-DD\n"
    "TIME: HH:MM\n"
    "TIMEZONE: <one of ET, PT, CT, MT, BST, CEST, JST, AEST>\n"
    "Use 24-hour time. If several timezones are listed, prefer ET if "
    "given, otherwise pick any one listed. If no specific date or time is "
    "stated anywhere in the text, reply with exactly: unknown\n\nText: {text}"
)
_STRUCTURED_RE = re.compile(
    r'DATE:\s*(\d{4}-\d{2}-\d{2}).*?TIME:\s*(\d{1,2}):(\d{2}).*?TIMEZONE:\s*(\w+)',
    re.IGNORECASE | re.DOTALL,
)


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS events ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "event_type TEXT NOT NULL, "
        "guid TEXT NOT NULL, "
        "title TEXT NOT NULL, "
        "url TEXT NOT NULL, "
        "broadcast_time TEXT, "
        "display_time TEXT NOT NULL, "
        "day_before_sent INTEGER NOT NULL DEFAULT 0, "
        "thirty_min_sent INTEGER NOT NULL DEFAULT 0, "
        "recap_sent INTEGER NOT NULL DEFAULT 0, "
        "UNIQUE(event_type, guid))"
    )
    return conn


def _fetch_rss_posts(url: str):
    """Returns [(guid, title, link, published, description), ...] from a
    standard RSS 2.0 feed, newest first. published is the item's pubDate
    as a datetime.date, or None if missing/unparseable -- used only as a
    fallback display date if the LLM can't find an exact broadcast time."""
    response = requests.get(url, headers={'User-Agent': USER_AGENT}, timeout=10)
    response.raise_for_status()
    root = ET.fromstring(response.text)

    posts = []
    channel = root.find('channel')
    for item in (channel.findall('item') if channel is not None else []):
        title = item.findtext('title') or ''
        link = item.findtext('link') or ''
        guid = item.findtext('guid') or link
        description = item.findtext('description') or ''
        pub_date_text = item.findtext('pubDate')
        try:
            published = parsedate_to_datetime(pub_date_text).date() if pub_date_text else None
        except (TypeError, ValueError):
            published = None
        posts.append((guid, title, link, published, description))
    return posts


def _format_date(date) -> str:
    if date is None:
        return "date unknown"
    return date.strftime('%B %d, %Y').replace(' 0', ' ', 1)


def _extract_broadcast_datetime(description_html: str, published):
    """Returns (utc_datetime_or_None, display_string). Asks the LLM for
    structured date/time/timezone fields (rather than a free-form string)
    so the actual timezone conversion -- including DST -- happens
    deterministically via zoneinfo, instead of trusting a small local
    model to compute a UTC offset itself. Falls back to the article's
    publish date (and no schedulable time) if extraction fails for any
    reason: Ollama unreachable, no date actually stated, an unrecognized
    timezone abbreviation, or a malformed reply."""
    text = BeautifulSoup(description_html, 'html.parser').get_text(' ', strip=True)
    if text:
        prompt = BROADCAST_DATETIME_PROMPT.format(published=published or 'recently', text=text[:3000])
        answer = llmask.quick_query(prompt)
        if answer and not answer.strip().lower().startswith('unknown'):
            match = _STRUCTURED_RE.search(answer)
            if match:
                date_str, hour, minute, tz_abbrev = match.groups()
                zone_name = TIMEZONE_ABBREVIATIONS.get(tz_abbrev.upper())
                if zone_name:
                    try:
                        naive = datetime.datetime.fromisoformat(f"{date_str}T{int(hour):02d}:{minute}:00")
                        aware = naive.replace(tzinfo=ZoneInfo(zone_name))
                        # %-I isn't portable (GNU/Linux-only strftime extension) --
                        # lstrip is safe here since %I is always 01-12, never "00".
                        hour_12 = aware.strftime('%I').lstrip('0') or '12'
                        display = (
                            f"{aware.strftime('%A')}, {aware.strftime('%B')} {aware.day}, {aware.year} "
                            f"at {hour_12}:{aware.strftime('%M')} {aware.strftime('%p')} {tz_abbrev.upper()}"
                        )
                        return aware.astimezone(datetime.timezone.utc), display
                    except (ValueError, KeyError):
                        pass

    return None, _format_date(published)


def _check_source(event_type: str) -> str | None:
    """Checks one source for a newly published announcement article,
    records it, and returns an announcement string -- or None if there's
    nothing new, or this is the very first time this event_type has ever
    been checked (silently seeds a baseline instead of replaying
    whatever's already in the feed as if it just happened)."""
    config = EVENT_SOURCES[event_type]
    try:
        posts = _fetch_rss_posts(config['feed_url'])
    except Exception:
        return None

    match = next((p for p in posts if config['announce_re'].search(p[1])), None)
    if not match:
        return None
    guid, title, url, published, description = match

    with _connect() as conn:
        is_first_ever = conn.execute(
            "SELECT COUNT(*) FROM events WHERE event_type = ?", (event_type,)
        ).fetchone()[0] == 0
        already_seen = conn.execute(
            "SELECT 1 FROM events WHERE event_type = ? AND guid = ?", (event_type, guid)
        ).fetchone()
        if already_seen:
            return None

        if is_first_ever:
            broadcast_time, display_time = None, _format_date(published)
        else:
            broadcast_time, display_time = _extract_broadcast_datetime(description, published)

        conn.execute(
            "INSERT INTO events (event_type, guid, title, url, broadcast_time, display_time, "
            "day_before_sent, thirty_min_sent, recap_sent) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event_type, guid, title, url,
                broadcast_time.isoformat() if broadcast_time else None,
                display_time,
                1 if is_first_ever else 0,
                1 if is_first_ever else 0,
                1 if is_first_ever else 0,
            ),
        )

    if is_first_ever:
        return None

    return f"🎮 **{config['header']} ({display_time})**\nRead more: {url}"


def check_game_announcements() -> list[str]:
    """Returns new announcement strings across all three tracked sources."""
    messages = []
    for event_type in EVENT_SOURCES:
        result = _check_source(event_type)
        if result:
            messages.append(result)
    return messages


def due_reminders() -> list[dict]:
    """Returns reminders that are due but not yet sent -- a day-before
    reminder once within 24h of air time, a 30-minutes-before reminder
    once within 30 minutes of it -- marking each as sent so a later check
    doesn't repeat it. If an event is *already* within 30 minutes the
    first time it's checked (e.g. broadcast_time only resolved shortly
    before air), both flags are set together so a "day before" reminder
    never fires after the fact. Each item: {'kind': 'day_before' or
    'thirty_min', 'title', 'url', 'display_time'}."""
    now = datetime.datetime.now(datetime.timezone.utc)
    due = []

    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, title, url, broadcast_time, display_time, day_before_sent, thirty_min_sent "
            "FROM events WHERE broadcast_time IS NOT NULL AND (day_before_sent = 0 OR thirty_min_sent = 0)"
        ).fetchall()

        for event_id, title, url, broadcast_time_text, display_time, day_before_sent, thirty_min_sent in rows:
            broadcast_time = datetime.datetime.fromisoformat(broadcast_time_text)

            if not thirty_min_sent and now >= broadcast_time - datetime.timedelta(minutes=30):
                due.append({'kind': 'thirty_min', 'title': title, 'url': url, 'display_time': display_time})
                conn.execute(
                    "UPDATE events SET thirty_min_sent = 1, day_before_sent = 1 WHERE id = ?", (event_id,)
                )
            elif not day_before_sent and now >= broadcast_time - datetime.timedelta(days=1):
                due.append({'kind': 'day_before', 'title': title, 'url': url, 'display_time': display_time})
                conn.execute("UPDATE events SET day_before_sent = 1 WHERE id = ?", (event_id,))

    return due


def check_recaps() -> list[str]:
    """For events that have already aired, checks that source's feed for
    a follow-up recap article. Matching is by keyword within the same
    feed (e.g. "Every Game, Announcement And Trailer"), not by verifying
    the recap is actually about this specific event -- a real but minor
    limitation if two events from the same source overlap in the recap
    search window. Gives up silently after RECAP_SEARCH_DAYS."""
    messages = []
    now = datetime.datetime.now(datetime.timezone.utc)

    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, event_type, title, url, broadcast_time FROM events "
            "WHERE recap_sent = 0 AND broadcast_time IS NOT NULL"
        ).fetchall()

    feed_cache = {}
    for event_id, event_type, title, _url, broadcast_time_text in rows:
        broadcast_time = datetime.datetime.fromisoformat(broadcast_time_text)
        if now < broadcast_time:
            continue  # hasn't aired yet

        if now > broadcast_time + datetime.timedelta(days=RECAP_SEARCH_DAYS):
            with _connect() as conn:
                conn.execute("UPDATE events SET recap_sent = 1 WHERE id = ?", (event_id,))
            continue

        config = EVENT_SOURCES[event_type]
        if event_type not in feed_cache:
            try:
                feed_cache[event_type] = _fetch_rss_posts(config['feed_url'])
            except Exception:
                feed_cache[event_type] = []

        recap = next((p for p in feed_cache[event_type] if config['recap_re'].search(p[1])), None)
        if not recap:
            continue

        _, recap_title, recap_url, _, _ = recap
        messages.append(f"📋 **Recap: {title}**\n**{recap_title}**\n{recap_url}")
        with _connect() as conn:
            conn.execute("UPDATE events SET recap_sent = 1 WHERE id = ?", (event_id,))

    return messages
