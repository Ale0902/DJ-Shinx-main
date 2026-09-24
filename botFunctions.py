import iTunes_Scrape
import csv
import os
import random
import json
import logging
import requests
from mcstatus import JavaServer

logger = logging.getLogger(__name__)

# Folder that contains this script, so file paths work on Windows and Linux
BASE = os.path.dirname(os.path.abspath(__file__))

# Persists the last-seen chapter/issue so we only announce genuinely new releases
RELEASE_STATE_FILE = os.path.join(BASE, 'release_state.json')

BERSERK_MANGADEX_ID = '801513ba-a712-498c-8f57-cae55b38cc92'
ABSOLUTE_BATMAN_VOLUME_ID = '160294'


def topsongs() -> str:
    iTunes_Scrape.updateSongList()
    with open(os.path.join(BASE, 'Top_Songs.csv'), 'r', encoding='utf-8') as csvfile:
        top_five = list(csv.DictReader(csvfile))[:5]

    lines = ["## The Top 5 Songs on iTunes Right Now!"]
    lines.extend(f"**Rank:** {row['Rank']}\n*{row['Song']}*, {row['Artist']}" for row in top_five)
    return "\n".join(lines) + "\n\nSource: https://www.popvortex.com/music/charts/top-100-songs.php"


def recsongs() -> str:
    with open(os.path.join(BASE, 'SOTD.csv'), 'r', encoding='utf-8') as csvfile:
        rows = list(csv.DictReader(csvfile))

    row = random.choice(rows)
    return (
        "### SOTD!:\n"
        f"\n**Song:** {row['Song Title']}\n"
        f"**Artist:** {row['Artist']}\n"
        f"**Submitted by:** {row['Your name (or tag)']}\n"
        f"\nLink: {row['Spotify or YouTube link']}"
    )


EIGHTBALL_RESPONSES = [
    'It is certain',
    'Reply hazy, try again',
    "Don't count on it",
    'It is decidedly so',
    'Ask again later',
    'My reply is no',
    'Without a doubt',
    'Better not tell you now',
    'My sources say no',
    'Yes definitely',
    'Cannot predict now',
    'Outlook not so good',
    'You may rely on it',
    'Concentrate and ask again',
    'Very doubtful',
    'As I see it, yes',
    'Most likely',
    'Outlook good',
    'Yes',
    'Signs point to yes',
]


def eightball() -> str:
    return random.choice(EIGHTBALL_RESPONSES)


def mc_status() -> str:
    server_ip = 'laurel-drink.tun.ply.gg'
    try:
        server = JavaServer.lookup(server_ip, timeout=5)
        status = server.status()

        player_count = status.players.online
        max_players = status.players.max

        # Build player list if any are online and names are available
        if player_count > 0 and status.players.sample:
            player_names = [p.name for p in status.players.sample]
            players_str = '\n'.join(f'  - {name}' for name in player_names)
            player_section = f"**Online Players ({player_count}/{max_players}):**\n{players_str}"
        elif player_count > 0:
            # Server online but hides player names
            player_section = f"**Online Players:** {player_count}/{max_players} (names hidden by server)"
        else:
            player_section = f"**Online Players:** 0/{max_players} — Nobody is on right now!"

        return (
            f"🟢 **{server_ip} is ONLINE!**\n"
            f"{player_section}"
        )

    except Exception as e:
        logger.warning(f"mc_status failed: {e}")
        return (
            f"🔴 **{server_ip} is OFFLINE** (or unreachable).\n"
            "The server may be down or restarting. Try again later!"
        )


def _load_release_state() -> dict:
    if os.path.exists(RELEASE_STATE_FILE):
        with open(RELEASE_STATE_FILE, 'r') as f:
            return json.load(f)
    return {}


def _save_release_state(state: dict) -> None:
    # Write to a temp file and rename over the target so a crash mid-write
    # can't leave a truncated/corrupt state file behind.
    tmp_path = RELEASE_STATE_FILE + '.tmp'
    with open(tmp_path, 'w') as f:
        json.dump(state, f)
    os.replace(tmp_path, RELEASE_STATE_FILE)


def _mangadex_cover_url(manga_id: str) -> str | None:
    """The series' cover art, for the announcement embed's thumbnail. A
    separate request from the chapter feed (which doesn't carry one), but
    only ever made on the rare occasion there's actually a new chapter to
    announce. Returns None on any failure -- the announcement is worth
    sending without a picture."""
    try:
        response = requests.get(
            f'https://api.mangadex.org/manga/{manga_id}',
            params={'includes[]': 'cover_art'}, timeout=10,
        )
        response.raise_for_status()
        relationships = response.json()['data']['relationships']
    except Exception as e:
        logger.debug(f"_mangadex_cover_url failed: {e}")
        return None

    for relationship in relationships:
        if relationship.get('type') == 'cover_art':
            filename = (relationship.get('attributes') or {}).get('fileName')
            if filename:
                # .512.jpg is MangaDex's downscaled variant -- the
                # full-size original is several MB for no visible gain at
                # the size Discord renders a thumbnail.
                return f'https://uploads.mangadex.org/covers/{manga_id}/{filename}.512.jpg'
    return None


def check_berserk_release() -> str | None:
    """Checks MangaDex for the latest Berserk chapter.

    Returns an announcement string if a new chapter has appeared since the
    last check, or None if there's nothing new (or this is the very first
    check, since there's no prior chapter to compare against yet).
    """
    url = f'https://api.mangadex.org/manga/{BERSERK_MANGADEX_ID}/feed'
    params = {
        'limit': 1,
        'translatedLanguage[]': 'en',
        'order[readableAt]': 'desc',
        'contentRating[]': ['safe', 'suggestive', 'erotica'],
    }

    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json().get('data', [])
    except Exception as e:
        logger.warning(f"check_berserk_release failed: {e}")
        return None

    if not data:
        return None

    latest = data[0]
    chapter_num = latest['attributes'].get('chapter')
    chapter_title = latest['attributes'].get('title') or ''
    chapter_id = latest['id']

    state = _load_release_state()
    previous = state.get('berserk_chapter')
    if previous == chapter_num:
        return None

    state['berserk_chapter'] = chapter_num
    _save_release_state(state)

    if previous is None:
        return None

    title_part = f": {chapter_title}" if chapter_title else ''
    cover_url = _mangadex_cover_url(BERSERK_MANGADEX_ID)
    thumbnail_part = f"\nTHUMBNAIL: {cover_url}" if cover_url else ''
    return (
        "⚔️ **New Berserk Chapter Released!**\n"
        f"**Chapter {chapter_num}**{title_part}\n"
        f"Read it here: https://mangadex.org/chapter/{chapter_id}"
        f"{thumbnail_part}"
    )


def check_absolute_batman_release() -> str | None:
    """Checks Comic Vine for the latest Absolute Batman issue.

    Returns an announcement string if a new issue has appeared since the
    last check, or None if there's nothing new, the API key isn't
    configured, or this is the very first check.
    """
    api_key = os.getenv('COMICVINE_API_KEY')
    if not api_key:
        return None

    url = 'https://comicvine.gamespot.com/api/issues/'
    params = {
        'api_key': api_key,
        'format': 'json',
        'filter': f'volume:{ABSOLUTE_BATMAN_VOLUME_ID}',
        'sort': 'cover_date:desc',
        'limit': 1,
        'field_list': 'id,name,issue_number,cover_date,site_detail_url,image',
    }
    headers = {'User-Agent': 'DJ-Shinx-Bot/1.0'}

    try:
        response = requests.get(url, params=params, headers=headers, timeout=10)
        response.raise_for_status()
        results = response.json().get('results', [])
    except Exception as e:
        logger.warning(f"check_absolute_batman_release failed: {e}")
        return None

    if not results:
        return None

    latest = results[0]
    issue_number = latest.get('issue_number')

    state = _load_release_state()
    previous = state.get('absolute_batman_issue')
    if previous == issue_number:
        return None

    state['absolute_batman_issue'] = issue_number
    _save_release_state(state)

    if previous is None:
        return None

    title = latest.get('name') or f'Absolute Batman #{issue_number}'
    link = latest.get('site_detail_url', '')
    link_part = f"\nMore info: {link}" if link else ''

    # Comic Vine returns the issue's cover in several sizes; medium is the
    # right order of magnitude for a thumbnail, with the others as
    # fallbacks in case a given issue is missing that one.
    image = latest.get('image') or {}
    cover_url = next(
        (image.get(key) for key in ('medium_url', 'original_url', 'thumb_url') if image.get(key)),
        None,
    )
    thumbnail_part = f"\nTHUMBNAIL: {cover_url}" if cover_url else ''

    return (
        "🦇 **New Absolute Batman Issue Released!**\n"
        f"**Issue #{issue_number}: {title}**{link_part}"
        f"{thumbnail_part}"
    )
