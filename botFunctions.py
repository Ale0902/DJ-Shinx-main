import iTunes_Scrape
import csv
import os
import random
import json
import requests
from mcstatus import JavaServer

# Folder that contains this script, so file paths work on Windows and Linux
BASE = os.path.dirname(os.path.abspath(__file__))

# Persists the last-seen chapter/issue so we only announce genuinely new releases
RELEASE_STATE_FILE = os.path.join(BASE, 'release_state.json')

BERSERK_MANGADEX_ID = '801513ba-a712-498c-8f57-cae55b38cc92'
ABSOLUTE_BATMAN_VOLUME_ID = '160294'

def topsongs():
        iTunes_Scrape.updateSongList()
        with open(os.path.join(BASE, 'Top_Songs.csv'), 'r') as csvfile:
            csv_reader = csv.reader(csvfile, delimiter=',')
            count = 1
            song_name =[]
            artist =[]
            rank =[]

            for row in csv_reader:
                song_name.append(row[0])
                artist.append(row[1])
                rank.append(row[2])
                count += 1

                if(count > 6):
                    break   
        return ("## The Top 5 Songs on iTunes Right Now!\n"
                f"**Rank:** {rank[1]}\n*{song_name[1]}*, {artist[1]}\n"
                f"**Rank:** {rank[2]}\n*{song_name[2]}*, {artist[2]}\n"
                f"**Rank:** {rank[3]}\n*{song_name[3]}*, {artist[3]}\n"
                f"**Rank:** {rank[4]}\n*{song_name[4]}*, {artist[4]}\n"
                f"**Rank:** {rank[5]}\n*{song_name[5]}*, {artist[5]}\n"
                "\nSource: https://www.popvortex.com/music/charts/top-100-songs.php"
                )

def recsongs():
     with open(os.path.join(BASE, 'SOTD.csv'), 'r', encoding='utf-8') as csvfile:
        csv_reader = csv.reader(csvfile, delimiter=',')
        rows = list(csv_reader)
        rand = random.randrange(1,len(list(rows)))
        count = 0
        songname = []
        artist = []
        link = []
        name = []

        for row in rows:
            count += 1
            if(count == rand):
                songname = row[0]
                artist= row[1]
                link = row[2]
                name =row[3]
                #rows.remove(row)
                break

        return ("### SOTD!:\n"
                f"\n**Song:** {songname}\n"
                f"**Artist:** {artist}\n"
                f"**Submitted by:** {name}\n"
                f"\nLink: {link}"
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

def eightball():
     return random.choice(EIGHTBALL_RESPONSES)

def mc_status():
    server_ip = 'listened-refried.tun.ply.gg'
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

    except Exception:
        return (
            f"🔴 **{server_ip} is OFFLINE** (or unreachable).\n"
            "The server may be down or restarting. Try again later!"
        )

def _load_release_state():
    if os.path.exists(RELEASE_STATE_FILE):
        with open(RELEASE_STATE_FILE, 'r') as f:
            return json.load(f)
    return {}

def _save_release_state(state):
    with open(RELEASE_STATE_FILE, 'w') as f:
        json.dump(state, f)

def check_berserk_release():
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
    except Exception:
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
    return (
        "⚔️ **New Berserk Chapter Released!**\n"
        f"**Chapter {chapter_num}**{title_part}\n"
        f"Read it here: https://mangadex.org/chapter/{chapter_id}"
    )

def check_absolute_batman_release():
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
        'field_list': 'id,name,issue_number,cover_date,site_detail_url',
    }
    headers = {'User-Agent': 'DJ-Shinx-Bot/1.0'}

    try:
        response = requests.get(url, params=params, headers=headers, timeout=10)
        response.raise_for_status()
        results = response.json().get('results', [])
    except Exception:
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

    return (
        "🦇 **New Absolute Batman Issue Released!**\n"
        f"**Issue #{issue_number}: {title}**{link_part}"
    )