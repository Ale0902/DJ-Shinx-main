"""Checks for newly announced Nintendo Direct and PlayStation State of Play
broadcasts, backing the /setchannel "game_announcements" feature.

Both sources are free, need no API key, and are actual news write-ups of
the announcement itself (not just a video appearing day-of):
- Nintendo Life's RSS feed. Nintendo itself has no official RSS (every
  guessed newsroom/press feed URL 404s), and polling their Twitter/X
  account isn't viable without a paid API tier (the free tier only
  covers a bot's own tweets, not reading another account's timeline) --
  Nintendo Life reliably publishes "Nintendo Direct Confirmed For ..."
  articles as soon as Nintendo announces one, often before any video
  exists yet.
- The PlayStation Blog's RSS feed, which reliably titles these posts
  "State of Play announced ...".

Same shape as botFunctions.py's Berserk/Absolute Batman checkers: persist
the last-seen item's id, and only announce once something newer shows up
-- including staying quiet on the very first check ever, since there's no
real "new" to compare against yet.
"""
import os
import re
import json
import requests
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from bs4 import BeautifulSoup

import llmask

BASE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE, 'game_news_state.json')

NINTENDO_LIFE_FEED_URL = 'https://www.nintendolife.com/feeds/latest'
PLAYSTATION_BLOG_FEED_URL = 'https://blog.playstation.com/feed/'

NINTENDO_DIRECT_RE = re.compile(r'nintendo direct', re.IGNORECASE)
STATE_OF_PLAY_RE = re.compile(r'state of play', re.IGNORECASE)

# Both sites consistently state the exact broadcast date/time somewhere in
# the announcement text -- confirmed against real articles (Nintendo Life:
# "It will take place on Tuesday, 4th August 2026 at 3pm BST"; PlayStation
# Blog: "Watch ... live on September 3 starting at 6:00am PT / 9:00am
# ET..."). The two sources (and even different articles on the same site)
# phrase this differently enough that a fixed regex would be fragile and
# go stale -- this only runs once per genuinely new announcement (not a
# hot path), so asking the already-running local LLM to pull it out is a
# better fit than hand-rolling date-parsing regex.
BROADCAST_TIME_PROMPT = (
    "This is a gaming news snippet announcing an upcoming Nintendo Direct or "
    "PlayStation State of Play broadcast. Reply with ONLY the exact date and "
    "time it airs, as a short human-readable string with one timezone, e.g. "
    "'Thursday, September 3, 2026 at 9am ET'. If several timezones are "
    "listed, just pick one reasonable one. If no specific date or time is "
    "stated, reply with exactly: unknown\n\nText: {text}"
)


def _load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r') as f:
            return json.load(f)
    return {}


def _save_state(state):
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f)


def _fetch_rss_posts(url: str):
    """Returns [(guid, title, link, published, description), ...] from a
    standard RSS 2.0 feed, newest first -- shared by both sources below,
    which are both plain RSS blogs/news feeds. published is the item's
    pubDate as a datetime.date, or None if missing/unparseable --  this
    is the article's publish date, used only as a fallback if the LLM
    can't pull an exact broadcast date/time out of the description."""
    response = requests.get(url, timeout=10)
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


def _broadcast_time(description_html: str, published) -> str:
    """Returns a human-readable broadcast date/time, extracted from the
    announcement's own text by the LLM, falling back to the article's
    publish date if that fails for any reason (Ollama unreachable, no
    date actually stated, an unparseable reply, etc.)."""
    text = BeautifulSoup(description_html, 'html.parser').get_text(' ', strip=True)
    if text:
        answer = llmask.quick_query(BROADCAST_TIME_PROMPT.format(text=text[:3000]))
        if answer and not answer.strip().lower().startswith('unknown'):
            return answer.strip()
    return _format_date(published)


def check_nintendo_direct():
    """Checks Nintendo Life's RSS feed for a newly published article
    announcing a Nintendo Direct. Returns an announcement string if one's
    appeared since the last check, or None if there's nothing new (or
    this is the very first check, since there's no prior baseline)."""
    try:
        posts = _fetch_rss_posts(NINTENDO_LIFE_FEED_URL)
    except Exception:
        return None

    direct = next((p for p in posts if NINTENDO_DIRECT_RE.search(p[1])), None)
    if not direct:
        return None
    guid, title, url, published, description = direct

    state = _load_state()
    previous = state.get('nintendo_direct_guid')
    if previous == guid:
        return None

    state['nintendo_direct_guid'] = guid
    _save_state(state)
    if previous is None:
        return None

    return f"🎮 **NINTENDO DIRECT ALERT! ({_broadcast_time(description, published)})**\nRead more: {url}"


def check_state_of_play():
    """Checks the PlayStation Blog for a newly posted State of Play
    announcement. Returns an announcement string if one's appeared since
    the last check, or None (including on the very first check)."""
    try:
        posts = _fetch_rss_posts(PLAYSTATION_BLOG_FEED_URL)
    except Exception:
        return None

    sop = next((p for p in posts if STATE_OF_PLAY_RE.search(p[1])), None)
    if not sop:
        return None
    guid, title, url, published, description = sop

    state = _load_state()
    previous = state.get('state_of_play_guid')
    if previous == guid:
        return None

    state['state_of_play_guid'] = guid
    _save_state(state)
    if previous is None:
        return None

    return f"🎮 **PLAYSTATION STATE OF PLAY ANNOUNCED! ({_broadcast_time(description, published)})**\nRead more: {url}"


def check_game_announcements():
    """Returns a list of new gaming-announcement messages (Nintendo
    Direct, PlayStation State of Play) since the last check -- combines
    both so the caller can post whichever are new together."""
    messages = []
    direct = check_nintendo_direct()
    if direct:
        messages.append(direct)
    state_of_play = check_state_of_play()
    if state_of_play:
        messages.append(state_of_play)
    return messages
