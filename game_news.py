"""Checks for newly announced Nintendo Direct and PlayStation State of Play
broadcasts, backing the /setchannel "game_announcements" feature.

Sources are both official and need no API key:
- Nintendo of America's YouTube channel RSS feed (a Direct's video/premiere
  page is what actually signals one's been scheduled).
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

BASE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE, 'game_news_state.json')

NINTENDO_YOUTUBE_CHANNEL_ID = 'UCGIY_O-8vW4rfX98KlMkvRg'  # Nintendo of America
NINTENDO_FEED_URL = f'https://www.youtube.com/feeds/videos.xml?channel_id={NINTENDO_YOUTUBE_CHANNEL_ID}'
PLAYSTATION_BLOG_FEED_URL = 'https://blog.playstation.com/feed/'

ATOM_NS = {'atom': 'http://www.w3.org/2005/Atom', 'yt': 'http://www.youtube.com/xml/schemas/2015'}

NINTENDO_DIRECT_RE = re.compile(r'nintendo direct', re.IGNORECASE)
STATE_OF_PLAY_RE = re.compile(r'state of play', re.IGNORECASE)


def _load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r') as f:
            return json.load(f)
    return {}


def _save_state(state):
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f)


def _fetch_nintendo_videos():
    """Returns [(video_id, title, url), ...] from Nintendo of America's
    YouTube channel feed, newest first."""
    response = requests.get(NINTENDO_FEED_URL, timeout=10)
    response.raise_for_status()
    root = ET.fromstring(response.text)

    videos = []
    for entry in root.findall('atom:entry', ATOM_NS):
        video_id = entry.findtext('yt:videoId', namespaces=ATOM_NS)
        title = entry.findtext('atom:title', namespaces=ATOM_NS) or ''
        link_el = entry.find('atom:link', ATOM_NS)
        url = link_el.get('href') if link_el is not None else f'https://www.youtube.com/watch?v={video_id}'
        videos.append((video_id, title, url))
    return videos


def _fetch_playstation_posts():
    """Returns [(guid, title, url), ...] from the PlayStation Blog RSS
    feed, newest first."""
    response = requests.get(PLAYSTATION_BLOG_FEED_URL, timeout=10)
    response.raise_for_status()
    root = ET.fromstring(response.text)

    posts = []
    channel = root.find('channel')
    for item in (channel.findall('item') if channel is not None else []):
        title = item.findtext('title') or ''
        link = item.findtext('link') or ''
        guid = item.findtext('guid') or link
        posts.append((guid, title, link))
    return posts


def check_nintendo_direct():
    """Checks for a newly posted Nintendo Direct video on Nintendo of
    America's YouTube channel. Returns an announcement string if one's
    appeared since the last check, or None if there's nothing new (or
    this is the very first check, since there's no prior baseline)."""
    try:
        videos = _fetch_nintendo_videos()
    except Exception:
        return None

    direct = next((v for v in videos if NINTENDO_DIRECT_RE.search(v[1])), None)
    if not direct:
        return None
    video_id, title, url = direct

    state = _load_state()
    previous = state.get('nintendo_direct_video_id')
    if previous == video_id:
        return None

    state['nintendo_direct_video_id'] = video_id
    _save_state(state)
    if previous is None:
        return None

    return f"🎮 **Nintendo Direct Alert!**\n**{title}**\nWatch here: {url}"


def check_state_of_play():
    """Checks the PlayStation Blog for a newly posted State of Play
    announcement. Returns an announcement string if one's appeared since
    the last check, or None (including on the very first check)."""
    try:
        posts = _fetch_playstation_posts()
    except Exception:
        return None

    sop = next((p for p in posts if STATE_OF_PLAY_RE.search(p[1])), None)
    if not sop:
        return None
    guid, title, url = sop

    state = _load_state()
    previous = state.get('state_of_play_guid')
    if previous == guid:
        return None

    state['state_of_play_guid'] = guid
    _save_state(state)
    if previous is None:
        return None

    return f"🎮 **PlayStation State of Play Announced!**\n**{title}**\nRead more: {url}"


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
