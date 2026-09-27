import requests
import re
import datetime
import time
import unicodedata
import logging
import concurrent.futures
from typing import Any, Callable
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

# ESPN's public (unofficial, no key needed) scoreboard/standings API. The
# scoreboard always reflects the current match week/day for a league,
# including live scores, so no local state needs to be tracked between calls
# the way the manga release checks do.
ESPN_SITE_BASE = "https://site.api.espn.com/apis/site/v2/sports"
ESPN_STANDINGS_BASE = "https://site.api.espn.com/apis/v2/sports"
NFL_SCOREBOARD_URL = f"{ESPN_SITE_BASE}/football/nfl/scoreboard"
CFB_SCOREBOARD_URL = f"{ESPN_SITE_BASE}/football/college-football/scoreboard"
MLB_SCOREBOARD_URL = f"{ESPN_SITE_BASE}/baseball/mlb/scoreboard"
NFL_STANDINGS_URL = f"{ESPN_STANDINGS_BASE}/football/nfl/standings"

# Per conference: seeds 1-4 are the division winners, 5-7 are the wild card
# spots, and the next few are shown as still "in the hunt" even though
# they're currently outside the 7-team playoff picture.
NFL_PLAYOFF_SPOTS = 7
NFL_HUNT_SIZE = 3

USF_TEAM_ID = "58"  # South Florida Bulls, per ESPN

# Each competition's ESPN slug(s). Most map to a single slug; World Cup
# Qualifying is split by confederation on ESPN, so those results get merged
# into one "World Cup Qualifiers" page.
SOCCER_COMPETITIONS = [
    ("Premier League", ["eng.1"]),
    ("La Liga", ["esp.1"]),
    ("Champions League", ["uefa.champions"]),
    ("World Cup", ["fifa.world"]),
    ("European Championship", ["uefa.euro"]),
    ("Copa América", ["conmebol.america"]),
    ("Nations League", ["uefa.nations"]),
    ("World Cup Qualifiers", [
        "fifa.worldq.uefa",
        "fifa.worldq.conmebol",
        "fifa.worldq.concacaf",
        "fifa.worldq.afc",
        "fifa.worldq.caf",
    ]),
]

# Competitions between national teams, as opposed to clubs -- only these get
# a flag next to the team name, since a club's ESPN displayName (e.g. "Real
# Madrid") isn't a country.
INTERNATIONAL_COMPETITIONS = {
    "World Cup", "European Championship", "Copa América", "Nations League", "World Cup Qualifiers",
}

# Flag emoji by ESPN's displayName for that country, covering every UEFA
# member (the confederation these competitions run most often) plus the
# commonly-seen nations from the other confederations in World Cup
# Qualifiers/Copa América/World Cup. Unlisted names just show without a
# flag rather than erroring.
COUNTRY_FLAGS = {
    # UEFA
    "Albania": "🇦🇱", "Andorra": "🇦🇩", "Armenia": "🇦🇲", "Austria": "🇦🇹",
    "Azerbaijan": "🇦🇿", "Belarus": "🇧🇾", "Belgium": "🇧🇪", "Bosnia-Herzegovina": "🇧🇦",
    "Bulgaria": "🇧🇬", "Croatia": "🇭🇷", "Cyprus": "🇨🇾", "Czechia": "🇨🇿",
    "Denmark": "🇩🇰", "England": "🏴󠁧󠁢󠁥󠁮󠁧󠁿", "Estonia": "🇪🇪", "Faroe Islands": "🇫🇴",
    "Finland": "🇫🇮", "France": "🇫🇷", "Georgia": "🇬🇪", "Germany": "🇩🇪",
    "Gibraltar": "🇬🇮", "Greece": "🇬🇷", "Hungary": "🇭🇺", "Iceland": "🇮🇸",
    "Israel": "🇮🇱", "Italy": "🇮🇹", "Kazakhstan": "🇰🇿", "Kosovo": "🇽🇰",
    "Latvia": "🇱🇻", "Liechtenstein": "🇱🇮", "Lithuania": "🇱🇹", "Luxembourg": "🇱🇺",
    "Malta": "🇲🇹", "Moldova": "🇲🇩", "Montenegro": "🇲🇪", "Netherlands": "🇳🇱",
    "North Macedonia": "🇲🇰", "Northern Ireland": "🇬🇧", "Norway": "🇳🇴", "Poland": "🇵🇱",
    "Portugal": "🇵🇹", "Republic of Ireland": "🇮🇪", "Romania": "🇷🇴", "Russia": "🇷🇺",
    "San Marino": "🇸🇲", "Scotland": "🏴󠁧󠁢󠁳󠁣󠁴󠁿", "Serbia": "🇷🇸", "Slovakia": "🇸🇰",
    "Slovenia": "🇸🇮", "Spain": "🇪🇸", "Sweden": "🇸🇪", "Switzerland": "🇨🇭",
    "Türkiye": "🇹🇷", "Ukraine": "🇺🇦", "Wales": "🏴󠁧󠁢󠁷󠁬󠁳󠁿",
    # CONMEBOL
    "Argentina": "🇦🇷", "Bolivia": "🇧🇴", "Brazil": "🇧🇷", "Chile": "🇨🇱",
    "Colombia": "🇨🇴", "Ecuador": "🇪🇨", "Paraguay": "🇵🇾", "Peru": "🇵🇪",
    "Uruguay": "🇺🇾", "Venezuela": "🇻🇪",
    # CONCACAF
    "United States": "🇺🇸", "Mexico": "🇲🇽", "Canada": "🇨🇦", "Costa Rica": "🇨🇷",
    "Jamaica": "🇯🇲", "Panama": "🇵🇦", "Honduras": "🇭🇳", "El Salvador": "🇸🇻",
    "Guatemala": "🇬🇹", "Trinidad and Tobago": "🇹🇹",
    # AFC
    "Japan": "🇯🇵", "South Korea": "🇰🇷", "Australia": "🇦🇺", "Saudi Arabia": "🇸🇦",
    "Iran": "🇮🇷", "Qatar": "🇶🇦", "Iraq": "🇮🇶", "China PR": "🇨🇳",
    # CAF
    "Nigeria": "🇳🇬", "Egypt": "🇪🇬", "Senegal": "🇸🇳", "Morocco": "🇲🇦",
    "Ghana": "🇬🇭", "Cameroon": "🇨🇲", "Tunisia": "🇹🇳", "Algeria": "🇩🇿",
    "South Africa": "🇿🇦", "Ivory Coast": "🇨🇮",
}


def _flag_label(competitor: dict) -> str:
    name = competitor['team']['displayName']
    flag = COUNTRY_FLAGS.get(name)
    return f"{flag} {name}" if flag else name

SOCCER_PAGE_LIMIT = 4000  # leaves headroom under an embed description's 4096-char cap

EASTERN = ZoneInfo("America/New_York")

# Shared thread pool for fan-out fetches (e.g. one soccer competition's
# scoreboard per day of the week). Reused across calls instead of spinning
# up a fresh pool per command invocation.
_executor = concurrent.futures.ThreadPoolExecutor(max_workers=32)

# Short-lived cache for ESPN's JSON responses. ESPN's scoreboard/standings
# API is unofficial and undocumented, so this both avoids duplicate
# round-trips within a single command (e.g. /ucl checking the phase and
# then fetching the bracket from the same URL) or across near-simultaneous
# commands from different users, and gives a little cushion against getting
# rate-limited. Live scores are only ever this many seconds stale.
_CACHE_TTL_SECONDS = 15
_response_cache: dict[tuple, tuple[float, Any]] = {}


def _get_json(url: str, params: dict | None = None) -> Any:
    """GETs a JSON endpoint, serving a cached response if the same
    url+params were fetched within _CACHE_TTL_SECONDS."""
    key = (url, tuple(sorted((params or {}).items())))
    cached = _response_cache.get(key)
    now = time.monotonic()
    if cached and now - cached[0] < _CACHE_TTL_SECONDS:
        return cached[1]

    response = requests.get(url, params=params, timeout=10)
    response.raise_for_status()
    data = response.json()
    _response_cache[key] = (now, data)
    return data


def _soccer_scoreboard_url(slug: str) -> str:
    return f"{ESPN_SITE_BASE}/soccer/{slug}/scoreboard"


def _fetch_scoreboard(url: str, date: datetime.date | None = None, params: dict | None = None) -> dict:
    request_params = dict(params or {})
    # ESPN's `dates` filter only accepts a single YYYYMMDD day, not a range.
    if date:
        request_params['dates'] = date.strftime('%Y%m%d')
    return _get_json(url, params=request_params)


def _fetch_standings(slug: str) -> dict:
    url = f"{ESPN_STANDINGS_BASE}/soccer/{slug}/standings"
    return _get_json(url)


def _week_dates(start_weekday: int) -> list[datetime.date]:
    """Returns the 7 dates of the week beginning on start_weekday
    (Monday=0 ... Sunday=6) that contains today, in Eastern time."""
    today = datetime.datetime.now(EASTERN).date()
    days_since_start = (today.weekday() - start_weekday) % 7
    week_start = today - datetime.timedelta(days=days_since_start)
    return [week_start + datetime.timedelta(days=i) for i in range(7)]


def _format_time(dt: datetime.datetime) -> str:
    return dt.strftime('%I:%M %p ET').lstrip('0')


def _format_game_line(event: dict, is_soccer: bool = False, label_fn: Callable[[dict], str] | None = None) -> str:
    sport_emoji = '⚽' if is_soccer else '🏈'

    competition = event['competitions'][0]
    competitors = competition['competitors']
    home = next(c for c in competitors if c['homeAway'] == 'home')
    away = next(c for c in competitors if c['homeAway'] == 'away')

    label_fn = label_fn or (lambda c: c['team']['displayName'])
    home_name, away_name = label_fn(home), label_fn(away)

    game_time = datetime.datetime.fromisoformat(event['date'].replace('Z', '+00:00'))
    game_time = game_time.astimezone(EASTERN)
    day_label = game_time.strftime('%A (%m/%d)')

    status = competition.get('status', {})
    state = status.get('type', {}).get('state', 'pre')

    # Once there's a score to show, color whichever team is ahead (or has
    # won) green and the other red -- a tie colors neither. This relies on
    # the line being wrapped in a ```ansi code block by the caller.
    home_color = away_color = None
    if state in ('in', 'post'):
        try:
            home_score, away_score = int(home['score']), int(away['score'])
        except (KeyError, ValueError, TypeError):
            home_score = away_score = 0
        if home_score > away_score:
            home_color, away_color = ANSI_GREEN, ANSI_RED
        elif away_score > home_score:
            home_color, away_color = ANSI_RED, ANSI_GREEN

    home_name = _colorize(home_name, home_color)
    away_name = _colorize(away_name, away_color)
    home_score_text = _colorize(home['score'], home_color)
    away_score_text = _colorize(away['score'], away_color)

    if is_soccer:
        matchup = f"{home_name} vs {away_name}"
        score = f"{home_score_text}-{away_score_text}"
    else:
        matchup = f"{away_name} @ {home_name}"
        score = f"{away_score_text}-{home_score_text}"

    if state == 'in':
        clock = status.get('displayClock', '')
        if is_soccer:
            return f"{sport_emoji} 🔴 {day_label}: {matchup} {score} ({clock})"
        period = status.get('period', '')
        return f"{sport_emoji} 🔴 {day_label}: {matchup} {score} (Q{period}, {clock})"
    elif state == 'post':
        detail = status.get('type', {}).get('description', 'Final')
        return f"{sport_emoji} {day_label}: {matchup} {score} ({detail})"
    else:
        return f"{sport_emoji} {day_label}: {matchup} — {_format_time(game_time)}"


# A Discord embed is a fixed width, so the only way to stop a slate
# wrapping is to spend fewer characters per row. The old one-line-per-game
# format spent most of its width on things that repeat identically down
# the whole list -- a 🏈 on every row, "Sunday (09/27): " on every row,
# "(Final)" on every row of a results list -- and pushed each matchup onto
# a second line, so sixteen games read as a wall of thirty-two lines.
#
# Instead: the date becomes one heading per day, the emoji and the
# redundant status go, and teams use ESPN's shortDisplayName. NFL
# nicknames are unique league-wide, so "Panthers" loses nothing over
# "Carolina Panthers" but is half the width -- 10 characters at most
# across the league, against 21.
NFL_NAME_WIDTH = 10

# Wide enough for "35-14" and for the "at" of a game that hasn't kicked
# off, so scores and fixtures line up in the same column.
NFL_SCORE_WIDTH = 6


def _nfl_day_heading(event: dict) -> str:
    kickoff = datetime.datetime.fromisoformat(event['date'].replace('Z', '+00:00')).astimezone(EASTERN)
    return kickoff.strftime('%A, %b %d').replace(' 0', ' ')


def _short_team_name(competitor: dict) -> str:
    team = competitor['team']
    return team.get('shortDisplayName') or team.get('name') or team['displayName']


def _format_nfl_row(event: dict) -> str:
    """One game as a fixed-width row: away team, score (or "at"), home
    team, then whatever still needs saying -- the quarter and clock for a
    live game, the kickoff time for an upcoming one, "OT" for a game that
    needed it. A plain "Final" is left off: in a results list every row is
    final, and the heading already says so."""
    competition = event['competitions'][0]
    competitors = competition['competitors']
    home = next(c for c in competitors if c['homeAway'] == 'home')
    away = next(c for c in competitors if c['homeAway'] == 'away')

    status = competition.get('status', {})
    state = status.get('type', {}).get('state', 'pre')

    if state == 'pre':
        kickoff = datetime.datetime.fromisoformat(event['date'].replace('Z', '+00:00')).astimezone(EASTERN)
        middle = 'at'.center(NFL_SCORE_WIDTH)
        note = _format_time(kickoff)
        away_color = home_color = None
    else:
        try:
            away_score, home_score = int(away['score']), int(home['score'])
        except (KeyError, ValueError, TypeError):
            away_score = home_score = 0
        middle = f"{away_score}-{home_score}".center(NFL_SCORE_WIDTH)

        # Only the winner is coloured. Colouring the loser too meant every
        # row carried two colours, which on a full sixteen-game slate read
        # as noise rather than as information.
        away_color = ANSI_GREEN if away_score > home_score else None
        home_color = ANSI_GREEN if home_score > away_score else None

        if state == 'in':
            note = f"Q{status.get('period', '')} {status.get('displayClock', '')}".strip()
        else:
            detail = status.get('type', {}).get('description', 'Final')
            # "Final/OT" is worth a mention; a plain "Final" isn't.
            note = detail.split('/', 1)[1] if '/' in detail else ''

    away_cell = _colorize(f"{_short_team_name(away):<{NFL_NAME_WIDTH}.{NFL_NAME_WIDTH}}", away_color)

    # The home team is the last column, so it's padded only when a note
    # follows that needs to line up. Padding it regardless would leave
    # trailing spaces that rstrip can't reach, since a coloured cell ends
    # with its reset code *after* the padding.
    home_name = f"{_short_team_name(home):.{NFL_NAME_WIDTH}}"
    if note:
        home_name = f"{home_name:<{NFL_NAME_WIDTH}}"
    home_cell = _colorize(home_name, home_color)

    row = f"{away_cell} {middle} {home_cell}"
    return f"{row} {note}" if note else row


def _nfl_lines_by_day(events: list[dict], show_days: bool = True) -> list[str]:
    """Rows grouped under one heading per day, in kickoff order. Each
    group is a single entry so _chunk_ansi_block can't split a heading
    away from the games beneath it."""
    events = sorted(events, key=lambda e: e['date'])
    if not show_days:
        return [_format_nfl_row(event) for event in events]

    groups: dict[str, list[str]] = {}
    for event in events:
        groups.setdefault(_nfl_day_heading(event), []).append(_format_nfl_row(event))

    blocks = []
    for heading, rows in groups.items():
        # Bold rather than markdown ** -- inside an ansi block the
        # asterisks would show up literally.
        blocks.append(_colorize(heading, ANSI_BOLD_WHITE) + chr(10) + chr(10).join(rows))
    return blocks

def nfl_synopsis() -> list[str]:
    """Returns this week's NFL games with live/final scores as a list of
    Discord-ready message chunks."""
    try:
        # ESPN's default NFL scoreboard already spans the full Thu-Sun-Mon
        # week as one "current week", so no date filtering is needed here.
        data = _fetch_scoreboard(NFL_SCOREBOARD_URL)
    except Exception as e:
        logger.warning(f"nfl_synopsis failed: {e}")
        return ["Couldn't reach the NFL scores right now. Try again later!"]

    events = data.get('events', [])
    if not events:
        return ["No NFL games scheduled this week."]

    week_number = data.get('week', {}).get('number')
    header = f"## NFL Week {week_number} Games!" if week_number else "## This Week's NFL Games!"

    return _chunk_ansi_block(header, _nfl_lines_by_day(events))


def nfl_live_matches() -> list[str]:
    """Returns a synopsis of only the NFL games currently in progress,
    with their current score and time remaining, as a list of Discord-ready
    message chunks -- unlike /nfl, this excludes finished and upcoming
    games entirely."""
    try:
        data = _fetch_scoreboard(NFL_SCOREBOARD_URL)
    except Exception as e:
        logger.warning(f"nfl_live_matches failed: {e}")
        return ["Couldn't reach the NFL scores right now. Try again later!"]

    events = [event for event in data.get('events', []) if _is_live(event)]
    if not events:
        return ["No NFL games currently in progress."]

    return _chunk_ansi_block("## Live NFL Right Now!", _nfl_lines_by_day(events, show_days=False))


def nfl_results_this_week() -> list[str]:
    """Returns the final score of every NFL game that has finished so far
    this week, as a list of Discord-ready message chunks -- unlike /nfl,
    this excludes in-progress and upcoming games entirely. ESPN's default
    scoreboard already spans the full Thu-Sun-Mon week as one "current
    week", so this naturally includes Thursday and Friday night results
    alongside the rest, not just games from today."""
    try:
        data = _fetch_scoreboard(NFL_SCOREBOARD_URL)
    except Exception as e:
        logger.warning(f"nfl_results_this_week failed: {e}")
        return ["Couldn't reach the NFL scores right now. Try again later!"]

    events = [event for event in data.get('events', []) if _is_final(event)]
    if not events:
        return ["No NFL games have finished yet this week."]

    return _chunk_ansi_block("## This Week's NFL Results!", _nfl_lines_by_day(events))


def _nfl_stat(entry: dict, name: str) -> str | None:
    for stat in entry['stats']:
        if stat['name'] == name:
            return stat.get('displayValue')
    return None


def _fetch_nfl_standings() -> dict:
    # level=3 asks ESPN for conference -> division -> team, instead of the
    # default conference -> team grouping which loses division info.
    return _get_json(NFL_STANDINGS_URL, params={'level': 3})


def _nfl_team_row(entry: dict, tag: str = '') -> str:
    team = entry['team'].get('shortDisplayName') or entry['team']['displayName']
    record = _nfl_stat(entry, 'overall') or '-'
    streak = _nfl_stat(entry, 'streak') or '-'
    return f"{team:<18.18} {record:<8} {streak:<4}{tag}"


def _nfl_division_page(standings_data: dict) -> str:
    """One compact aligned table per conference (all its divisions
    together) instead of a separate blank-line-separated block per
    division -- fits the same info in noticeably less vertical space."""
    lines = ["## NFL Standings by Division"]
    for conference in standings_data.get('children', []):
        rows = [f"{'Team':<18} {'Record':<8} Streak"]
        for division in conference.get('children', []):
            entries = division.get('standings', {}).get('entries', [])
            entries.sort(key=lambda e: float(_nfl_stat(e, 'winPercent') or 0), reverse=True)
            rows.append(f"-- {division['name']} --")
            rows.extend(_nfl_team_row(entry) for entry in entries)
        body = "\n".join(rows)
        lines.append(f"**{conference['name']}**\n```\n{body}\n```")
    return "\n".join(lines)


def _nfl_playoff_page(standings_data: dict) -> str:
    """One compact table per conference: seed, team, record, streak, with
    a short tag marking wild card and in-the-hunt spots inline instead of
    separate section headers for each group."""
    lines = ["## NFL Playoff Picture"]
    for conference in standings_data.get('children', []):
        entries = [
            entry
            for division in conference.get('children', [])
            for entry in division.get('standings', {}).get('entries', [])
        ]
        entries.sort(key=lambda e: int(_nfl_stat(e, 'playoffSeed') or 99))

        shown = entries[:NFL_PLAYOFF_SPOTS + NFL_HUNT_SIZE]
        rows = [f"{'#':<3}{'Team':<18} {'Record':<8} Streak"]
        for i, entry in enumerate(shown, start=1):
            tag = '' if i <= 4 else '  (WC)' if i <= NFL_PLAYOFF_SPOTS else '  (hunt)'
            rows.append(f"{i:<3}{_nfl_team_row(entry, tag)}")

        body = "\n".join(rows)
        lines.append(f"**{conference['name']}**\n```\n{body}\n```")
    return "\n".join(lines)


def nfl_standings_pages() -> list[tuple[str, str]]:
    """Returns [(title, page_text)] for the /nflstandings paginator: one
    page grouping every team by division, and one page showing the current
    playoff picture -- division leaders, wild card spots, and the next few
    teams still in the hunt for a wild card berth."""
    try:
        data = _fetch_nfl_standings()
    except Exception as e:
        logger.warning(f"nfl_standings_pages failed: {e}")
        return [("Standings", "Couldn't reach NFL standings right now. Try again later!")]

    return [
        ("Division Standings", _nfl_division_page(data)),
        ("Playoff Picture", _nfl_playoff_page(data)),
    ]


def _cfb_rank(competitor: dict) -> int | None:
    rank = competitor.get('curatedRank', {}).get('current')
    return rank if rank and rank <= 25 else None


def _cfb_label(competitor: dict) -> str:
    name = competitor['team']['displayName']
    rank = _cfb_rank(competitor)
    if rank:
        name = f"#{rank} {name}"
    if competitor['team']['id'] == USF_TEAM_ID:
        name = f"⭐ {name}"
    return name


def _cfb_involves_usf(event: dict) -> bool:
    return any(c['team']['id'] == USF_TEAM_ID for c in event['competitions'][0]['competitors'])


def _cfb_involves_ranked_team(event: dict) -> bool:
    return any(_cfb_rank(c) for c in event['competitions'][0]['competitors'])


def cfb_synopsis() -> list[str]:
    """Returns a compressed synopsis of this week's Division I FBS college
    football games as a list of Discord-ready message chunks. The full FBS
    slate runs 60-75+ games a week -- far too much for one Discord message
    -- so this focuses on South Florida's game (always shown) plus every
    game involving a ranked (Top 25) team."""
    try:
        # ESPN's default scoreboard only returns a small curated subset.
        # groups=80 selects FBS and a high limit pulls the full week's slate.
        data = _fetch_scoreboard(CFB_SCOREBOARD_URL, params={'groups': 80, 'limit': 400})
    except Exception as e:
        logger.warning(f"cfb_synopsis failed: {e}")
        return ["Couldn't reach the college football scores right now. Try again later!"]

    events = data.get('events', [])
    if not events:
        return ["No college football games scheduled this week."]

    week_number = data.get('week', {}).get('number')
    header = f"## College Football Week {week_number}!" if week_number else "## This Week's College Football Games!"

    usf_games = sorted((e for e in events if _cfb_involves_usf(e)), key=lambda e: e['date'])
    usf_event_ids = {e['id'] for e in usf_games}
    ranked_games = sorted(
        (e for e in events if e['id'] not in usf_event_ids and _cfb_involves_ranked_team(e)),
        key=lambda e: e['date'],
    )

    if not usf_games and not ranked_games:
        return [header + "\nNo ranked matchups or USF game found this week."]

    messages = []
    if usf_games:
        lines = [_format_game_line(event, label_fn=_cfb_label) for event in usf_games]
        messages.extend(_chunk_ansi_block(f"{header}\n\n**South Florida Bulls**", lines))

    if ranked_games:
        lines = [_format_game_line(event, label_fn=_cfb_label) for event in ranked_games]
        section_header = "**Top 25 Games**" if messages else f"{header}\n\n**Top 25 Games**"
        messages.extend(_chunk_ansi_block(section_header, lines))

    return messages


# Always shown regardless of opponent or playoff standing.
FEATURED_MLB_TEAM_IDS = {
    "28",  # Miami Marlins
    "21",  # New York Mets
    "30",  # Tampa Bay Rays
}

MLB_STANDINGS_URL = f"{ESPN_STANDINGS_BASE}/baseball/mlb/standings"
MLB_CONTENTION_THRESHOLD = 5.0  # min ESPN playoff-odds % to count as "in contention"


def _mlb_contending_team_ids(threshold: float = MLB_CONTENTION_THRESHOLD) -> set[str]:
    """Returns the set of MLB team ids ESPN currently gives at least
    `threshold`% odds of making the playoffs. Every MLB series (30 teams,
    ~15 concurrent matchups) would be too much text, so this replaces a
    static "big market" list with teams that are actually still in the
    hunt right now."""
    try:
        data = _get_json(MLB_STANDINGS_URL)
    except Exception as e:
        logger.warning(f"_mlb_contending_team_ids failed: {e}")
        return set()

    contenders = set()
    for league in data.get('children', []):
        for entry in league.get('standings', {}).get('entries', []):
            stats = {stat['name']: stat for stat in entry['stats']}
            pct = stats.get('playoffPercent', {}).get('value')
            if pct is not None and pct >= threshold:
                contenders.add(entry['team']['id'])

    return contenders


def _mlb_series_record(games: list[dict]) -> tuple[int, int, bool]:
    """Returns (home_wins, away_wins, all_completed) across a group of
    games between the same two teams."""
    home_wins = away_wins = 0
    completed_count = 0
    for game in games:
        competition = game['competitions'][0]
        if not competition.get('status', {}).get('type', {}).get('completed'):
            continue
        completed_count += 1
        competitors = competition['competitors']
        home = next(c for c in competitors if c['homeAway'] == 'home')
        away = next(c for c in competitors if c['homeAway'] == 'away')
        if int(home['score']) > int(away['score']):
            home_wins += 1
        elif int(away['score']) > int(home['score']):
            away_wins += 1

    return home_wins, away_wins, completed_count == len(games)


def _mlb_series_line(games: list[dict]) -> tuple[str, str]:
    games = sorted(games, key=lambda e: e['date'])
    competition = games[0]['competitions'][0]
    competitors = competition['competitors']
    home_name = next(c for c in competitors if c['homeAway'] == 'home')['team']['shortDisplayName']
    away_name = next(c for c in competitors if c['homeAway'] == 'away')['team']['shortDisplayName']

    first_date = datetime.datetime.fromisoformat(games[0]['date'].replace('Z', '+00:00')).astimezone(EASTERN)
    last_date = datetime.datetime.fromisoformat(games[-1]['date'].replace('Z', '+00:00')).astimezone(EASTERN)
    if first_date.date() == last_date.date():
        date_range = first_date.strftime('%a')
    else:
        date_range = f"{first_date.strftime('%a')}-{last_date.strftime('%a')}"

    home_wins, away_wins, all_completed = _mlb_series_record(games)

    # Color whichever team is ahead (or has won) the series green and the
    # other red -- a tied series colors neither.
    if home_wins > away_wins:
        home_name, away_name = _colorize(home_name, ANSI_GREEN), _colorize(away_name, ANSI_RED)
    elif away_wins > home_wins:
        home_name, away_name = _colorize(home_name, ANSI_RED), _colorize(away_name, ANSI_GREEN)

    record = ""
    if home_wins or away_wins:
        if home_wins == away_wins:
            record = f" — Tied {home_wins}-{away_wins}"
        else:
            leader, leader_wins, other_wins = (
                (home_name, home_wins, away_wins) if home_wins > away_wins
                else (away_name, away_wins, home_wins)
            )
            verb = "won" if all_completed else "leads"
            record = f" — {leader} {verb} {leader_wins}-{other_wins}"

    return games[0]['date'], f"⚾ {away_name} @ {home_name} ({date_range}, {len(games)}G){record}"


def mlb_series_synopsis() -> list[str]:
    """Returns this week's MLB matchups grouped into series (rather than
    every individual game, which would run 90+ games/week) with each
    series' overall record so far, as a list of Discord-ready message
    chunks."""
    week_dates = _week_dates(start_weekday=0)

    def fetch(day: datetime.date) -> list[dict]:
        try:
            return _fetch_scoreboard(MLB_SCOREBOARD_URL, date=day).get('events', [])
        except Exception as e:
            logger.debug(f"mlb_series_synopsis: failed to fetch {day}: {e}")
            return []

    events_by_id = {}
    for events in _executor.map(fetch, week_dates):
        for event in events:
            events_by_id[event['id']] = event

    if not events_by_id:
        return ["No MLB games scheduled this week."]

    contenders = _mlb_contending_team_ids()

    def is_featured(team_id: str) -> bool:
        return team_id in FEATURED_MLB_TEAM_IDS or team_id in contenders

    series_map: dict[tuple[str, str], list[dict]] = {}
    for event in events_by_id.values():
        competitors = event['competitions'][0]['competitors']
        home_id = next(c for c in competitors if c['homeAway'] == 'home')['team']['id']
        away_id = next(c for c in competitors if c['homeAway'] == 'away')['team']['id']
        if not (is_featured(home_id) or is_featured(away_id)):
            continue
        series_map.setdefault((home_id, away_id), []).append(event)

    if not series_map:
        return ["No notable MLB series found this week."]

    lines = sorted((_mlb_series_line(games) for games in series_map.values()), key=lambda item: item[0])

    return _chunk_ansi_block("## This Week's MLB Series!", [line for _, line in lines])


def _chunk_ansi_block(header: str, lines: list[str], limit: int = SOCCER_PAGE_LIMIT) -> list[str]:
    """Packs `lines` (each possibly containing its own embedded newlines,
    e.g. a live match's goal list) into one or more Discord messages, each
    wrapping its slice in its own fenced ```ansi block with its own header
    -- so a busy day's slate can't have a color-coded line's fence split
    across messages the way a naive character-count splitter would."""
    if not lines:
        return []

    fence_overhead = len(f"{header} (cont.)\n```ansi\n\n```")
    messages = []
    current: list[str] = []
    current_len = 0

    def flush():
        # Callers may pad `lines` with '' separators between blocks; if a
        # chunk boundary lands on one it would open or close a page with a
        # stray blank line inside the fence.
        trimmed = current[:]
        while trimmed and not trimmed[0].strip():
            trimmed.pop(0)
        while trimmed and not trimmed[-1].strip():
            trimmed.pop()
        if not trimmed:
            return
        suffix = "" if not messages else " (cont.)"
        messages.append(f"{header}{suffix}\n```ansi\n" + "\n".join(trimmed) + "\n```")

    for line in lines:
        line_len = len(line) + 1
        if current and current_len + line_len > limit - fence_overhead:
            flush()
            current = []
            current_len = 0
        current.append(line)
        current_len += line_len

    flush()
    return messages


def _ansi_page(header: str, lines: list[str], limit: int = SOCCER_PAGE_LIMIT) -> str:
    """Wraps `lines` in a single fenced ```ansi block sized to fit within
    `limit`, trimming from the end and noting how many were cut rather than
    overflowing -- used where the caller needs exactly one page back (e.g.
    one per competition in the /soccer paginator), unlike _chunk_ansi_block
    which can spill into extra messages."""
    overhead = len(f"{header}\n```ansi\n\n```")
    kept = []
    total = 0
    for line in lines:
        total += len(line) + 1
        if total > limit - overhead:
            break
        kept.append(line)

    remaining = len(lines) - len(kept)
    if remaining > 0:
        kept.append(f"...and {remaining} more match{'es' if remaining != 1 else ''}.")

    return f"{header}\n```ansi\n" + "\n".join(kept) + "\n```"


def _fetch_soccer_jobs(jobs: list[tuple], fetch_fn: Callable[[tuple], tuple[str, list[dict]]]) -> dict[str, dict[str, dict]]:
    """Runs fetch_fn(job) -> (competition_name, events) for each job in the
    shared thread pool, merging every job's events into
    {competition_name: {event_id: event}} -- de-duplicating repeats, since
    e.g. soccer_pages() queries the same competition once per day of the
    week and a match can show up in more than one day's response."""
    grouped: dict[str, dict[str, dict]] = {name: {} for name, _ in SOCCER_COMPETITIONS}
    for name, events in _executor.map(fetch_fn, jobs):
        for event in events:
            grouped[name][event['id']] = event
    return grouped


def soccer_pages() -> list[tuple[str, str]]:
    """Returns a list of (title, page_text) tuples, one per competition in
    SOCCER_COMPETITIONS, each showing that competition's matches for the
    current calendar week. Backs the /soccer command's button paginator so
    every competition gets its own page instead of a separate command."""
    # ESPN's soccer scoreboard only accepts a single day at a time, and its
    # default "current" window doesn't reliably include Sunday, so fetch
    # every day of the calendar week (Monday-through-Sunday) and merge.
    week_dates = _week_dates(start_weekday=0)

    jobs = [
        (name, slug, day)
        for name, slugs in SOCCER_COMPETITIONS
        for slug in slugs
        for day in week_dates
    ]

    def fetch(job: tuple) -> tuple[str, list[dict]]:
        name, slug, day = job
        try:
            events = _fetch_scoreboard(_soccer_scoreboard_url(slug), date=day).get('events', [])
        except Exception as e:
            logger.debug(f"soccer_pages: failed to fetch {slug} on {day}: {e}")
            events = []
        return name, events

    events_by_competition = _fetch_soccer_jobs(jobs, fetch)

    pages = []
    for name, _ in SOCCER_COMPETITIONS:
        events = sorted(events_by_competition[name].values(), key=lambda e: e['date'])
        if not events:
            continue
        label_fn = _flag_label if name in INTERNATIONAL_COMPETITIONS else None
        lines = [_format_game_line(event, is_soccer=True, label_fn=label_fn) for event in events]
        pages.append((name, _ansi_page(f"## {name}", lines)))

    return pages


def _is_live(event: dict) -> bool:
    status = event['competitions'][0].get('status', {})
    return status.get('type', {}).get('state') == 'in'


def _is_final(event: dict) -> bool:
    status = event['competitions'][0].get('status', {})
    return status.get('type', {}).get('state') == 'post'


def _match_events(competition: dict) -> list[tuple[str, str, str]]:
    """(minute, team_code, text) for every goal and red card in a match's
    play-by-play log ('details'), in chronological order -- e.g. ("43'",
    "AUT", "⚽ Romano Schmid (pen)").

    Returned unformatted so the caller can size the columns across a whole
    competition rather than per match; sized per match, a single
    stoppage-time goal ("90'+4'") shifted that one block's columns out of
    line with every block around it.

    The side is its three-letter code rather than the full name in
    brackets after the player: the full name was the widest part of every
    line and repeated what the score row directly above already says."""
    team_codes = {
        c['team']['id']: (
            c['team'].get('abbreviation')
            or (c['team'].get('shortDisplayName') or c['team']['displayName'])[:3].upper()
        )
        for c in competition.get('competitors', [])
    }

    events = []
    for detail in competition.get('details', []):
        is_goal = detail.get('scoringPlay')
        is_red_card = detail.get('redCard')
        if not (is_goal or is_red_card):
            continue

        clock = detail.get('clock', {})
        scorers = detail.get('athletesInvolved') or []
        name = scorers[0]['displayName'] if scorers else 'Unknown'
        code = team_codes.get(detail.get('team', {}).get('id'), '')

        if is_goal:
            tag = ' (OG)' if detail.get('ownGoal') else ' (pen)' if detail.get('penaltyKick') else ''
            text = f"⚽ {name}{tag}"
        else:
            text = f"🟥 {name}"

        events.append((clock.get('value', 0), clock.get('displayValue', ''), code, text))

    events.sort(key=lambda e: e[0])
    return [(minute, code, text) for _, minute, code, text in events]


_ANSI_ESCAPE_RE = re.compile('\x1b' + r'\[[0-9;]*m')


def _display_width(text: str) -> int:
    """How many monospace cells `text` occupies once Discord renders it,
    which len() gets wrong in both directions: ANSI escapes are characters
    that take no space, and a flag is several code points drawn two cells
    wide. Wales, England and Scotland are the worst of it -- a black flag
    plus six invisible tag characters, seven code points for two cells --
    so padding by len() under-pads them by five and drags the whole column
    out of line."""
    text = _ANSI_ESCAPE_RE.sub('', text)
    width = 0
    i = 0
    while i < len(text):
        cp = ord(text[i])
        if 0x1F1E6 <= cp <= 0x1F1FF:
            # A regional-indicator pair is one flag.
            width += 2
            i += 2
            continue
        if cp == 0x1F3F4:
            # Black flag followed by tag characters (subdivision flags).
            i += 1
            while i < len(text) and 0xE0000 <= ord(text[i]) <= 0xE007F:
                i += 1
            width += 2
            continue
        if cp in (0x200D, 0xFE0F) or unicodedata.combining(text[i]):
            i += 1  # joiners, variation selectors, accents: no width of their own
            continue
        if cp >= 0x1F300 or unicodedata.east_asian_width(text[i]) in ('W', 'F'):
            width += 2
        else:
            width += 1
        i += 1
    return width


def _pad_display(text: str, width: int) -> str:
    return text + ' ' * max(0, width - _display_width(text))


def _soccer_label(competitor: dict, with_flag: bool) -> str:
    """ESPN's shortDisplayName ("Rep Ireland" rather than "Republic of
    Ireland"), with the country's flag for international fixtures. The
    flag is looked up by the full displayName, which is what
    COUNTRY_FLAGS is keyed on -- the short form wouldn't find it."""
    team = competitor['team']
    short = team.get('shortDisplayName') or team['displayName']
    if with_flag:
        flag = COUNTRY_FLAGS.get(team['displayName'])
        if flag:
            return f"{flag} {short}"
    return short


def _live_soccer_blocks(events: list[dict], with_flags: bool) -> list[str]:
    """One block per match -- an aligned score row with its goals and red
    cards beneath -- laid out like the NFL slates: fixed columns, only the
    side that's ahead coloured, minute at the end.

    Every column width comes from the widest entry in *this* competition
    rather than a fixed constant, so a slate of short club names isn't
    padded out to fit "North Macedonia" and each row stays as narrow as it
    can -- and the goal lines line up from one match to the next, not just
    within one. Home stays on the left: soccer lists the home side first,
    unlike the NFL's away-at-home convention."""
    rows = []
    for event in events:
        competition = event['competitions'][0]
        competitors = competition['competitors']
        home = next(c for c in competitors if c['homeAway'] == 'home')
        away = next(c for c in competitors if c['homeAway'] == 'away')
        try:
            home_score, away_score = int(home['score']), int(away['score'])
        except (KeyError, ValueError, TypeError):
            home_score = away_score = 0
        rows.append({
            'home': _soccer_label(home, with_flags),
            'away': _soccer_label(away, with_flags),
            'home_score': home_score,
            'away_score': away_score,
            'score': f"{home_score}-{away_score}",
            'clock': competition.get('status', {}).get('displayClock', ''),
            'events': _match_events(competition),
        })

    if not rows:
        return []

    home_width = max(_display_width(r['home']) for r in rows)
    away_width = max(_display_width(r['away']) for r in rows)
    score_width = max(len(r['score']) for r in rows)
    all_events = [e for r in rows for e in r['events']]
    minute_width = max((len(minute) for minute, _, _ in all_events), default=0)
    code_width = max((len(code) for _, code, _ in all_events), default=0)

    blocks = []
    for r in rows:
        # A draw colours neither side, same as the NFL rows.
        home_color = ANSI_GREEN if r['home_score'] > r['away_score'] else None
        away_color = ANSI_GREEN if r['away_score'] > r['home_score'] else None

        home_cell = _colorize(_pad_display(r['home'], home_width), home_color)
        away_cell = _colorize(_pad_display(r['away'], away_width), away_color)
        score = r['score'].center(score_width)

        # Single spaces around the score: the padded name columns already
        # separate it visually, and each cell spent here is one a long
        # pairing like Liechtenstein v Lithuania needs to stay on one line.
        lines = [f"{home_cell} {score} {away_cell}  {r['clock']}".rstrip()]
        # Minutes left-aligned and padded, so the usual two- and three-
        # character minutes sit flush with the indent and only the rare
        # stoppage-time goal takes the extra room, rather than pushing
        # every other minute rightward to make space for it.
        lines.extend(
            f"  {minute:<{minute_width}}  {code:<{code_width}}  {text}"
            for minute, code, text in r['events']
        )
        blocks.append(chr(10).join(lines))
    return blocks


def live_soccer_matches() -> list[str]:
    """Returns a list of Discord-ready message chunks covering only the
    soccer matches currently in progress, across every tracked competition
    -- unlike /soccer, this excludes finished and upcoming matches
    entirely."""
    today = datetime.datetime.now(EASTERN).date()
    jobs = [(name, slug) for name, slugs in SOCCER_COMPETITIONS for slug in slugs]

    def fetch(job: tuple) -> tuple[str, list[dict]]:
        name, slug = job
        try:
            events = _fetch_scoreboard(_soccer_scoreboard_url(slug), date=today).get('events', [])
        except Exception as e:
            logger.debug(f"live_soccer_matches: failed to fetch {slug}: {e}")
            events = []
        return name, [e for e in events if _is_live(e)]

    live_by_competition = _fetch_soccer_jobs(jobs, fetch)

    messages = []
    for name, _ in SOCCER_COMPETITIONS:
        events = sorted(live_by_competition[name].values(), key=lambda e: e['date'])
        if not events:
            continue
        blocks = _live_soccer_blocks(events, with_flags=name in INTERNATIONAL_COMPETITIONS)
        # One blank line between matches. Each block is a score line with
        # its own goals indented underneath, so without a separator the
        # goals of one match butt straight up against the next match's
        # score line and the whole slate reads as a single wall.
        lines = [part for block in blocks for part in (block, '')][:-1]
        header = "## Live Soccer Right Now!\n\n**{}**".format(name) if not messages else f"**{name}**"
        messages.extend(_chunk_ansi_block(header, lines))

    return messages or ["No soccer matches currently in progress."]


def soccer_results_today() -> list[str]:
    """Returns the final score of every soccer match that finished today,
    across every tracked competition, as a list of Discord-ready message
    chunks -- unlike /soccer, this excludes in-progress and upcoming
    matches entirely."""
    today = datetime.datetime.now(EASTERN).date()
    jobs = [(name, slug) for name, slugs in SOCCER_COMPETITIONS for slug in slugs]

    def fetch(job: tuple) -> tuple[str, list[dict]]:
        name, slug = job
        try:
            events = _fetch_scoreboard(_soccer_scoreboard_url(slug), date=today).get('events', [])
        except Exception as e:
            logger.debug(f"soccer_results_today: failed to fetch {slug}: {e}")
            events = []
        return name, [e for e in events if _is_final(e)]

    results_by_competition = _fetch_soccer_jobs(jobs, fetch)

    messages = []
    for name, _ in SOCCER_COMPETITIONS:
        events = sorted(results_by_competition[name].values(), key=lambda e: e['date'])
        if not events:
            continue
        label_fn = _flag_label if name in INTERNATIONAL_COMPETITIONS else None
        lines = [_format_game_line(event, is_soccer=True, label_fn=label_fn) for event in events]
        header = "## Today's Soccer Results!\n\n**{}**".format(name) if not messages else f"**{name}**"
        messages.extend(_chunk_ansi_block(header, lines))

    return messages or ["No soccer results yet today."]


STANDINGS_HEADER = f"{'':2}{'#':>2} {'Team':<22} {'P':>2} {'W':>2} {'D':>2} {'L':>2} {'GF':>3} {'GA':>3} {'GD':>4} {'Pts':>3}"
STANDINGS_SEPARATOR = '-' * len(STANDINGS_HEADER)

# Marks a team's row in a standings table while they're in a live match:
# green if currently winning, red if losing, white/grey if tied.
LIVE_STATUS_EMOJI = {'win': '🟢', 'loss': '🔴', 'tie': '⚪'}

# Discord renders a subset of ANSI codes inside a ```ansi block (desktop/web
# only -- mobile just shows the plain, uncolored text).
ANSI_RESET = "\u001b[0m"
ANSI_RED = "\u001b[0;31m"
ANSI_GREEN = "\u001b[0;32m"
ANSI_BLUE = "\u001b[0;34m"
ANSI_WHITE = "\u001b[0;37m"
ANSI_BOLD_WHITE = "\u001b[1;37m"


def _colorize(text: str, code: str | None) -> str:
    return text if code is None else f"{code}{text}{ANSI_RESET}"


def _relegation_color(rank: int, total: int) -> str | None:
    """Bottom 3 spots (the relegation zone) are colored red."""
    return ANSI_RED if rank > total - 3 else None


def _ucl_zone_color(rank: int, total: int) -> str | None:
    """Top 8 (direct Round of 16 qualification) green, 9-24 (knockout
    round playoffs) blue, the rest (eliminated) white."""
    if rank <= 8:
        return ANSI_GREEN
    if rank <= 24:
        return ANSI_BLUE
    return ANSI_WHITE


def _stat_map(entry: dict) -> dict[str, str]:
    return {stat['name']: stat['displayValue'] for stat in entry['stats']}


def _live_status_by_team(slug: str) -> dict[str, str]:
    """Returns {team_id: 'win'|'loss'|'tie'} for teams currently in a live
    match in this competition, based on the match's current score."""
    try:
        today = datetime.datetime.now(EASTERN).date()
        data = _fetch_scoreboard(_soccer_scoreboard_url(slug), date=today)
    except Exception as e:
        logger.debug(f"_live_status_by_team failed for {slug}: {e}")
        return {}

    statuses = {}
    for event in data.get('events', []):
        if not _is_live(event):
            continue
        competitors = event['competitions'][0]['competitors']
        home = next(c for c in competitors if c['homeAway'] == 'home')
        away = next(c for c in competitors if c['homeAway'] == 'away')
        home_score, away_score = int(home['score']), int(away['score'])
        if home_score > away_score:
            statuses[home['team']['id']] = 'win'
            statuses[away['team']['id']] = 'loss'
        elif away_score > home_score:
            statuses[away['team']['id']] = 'win'
            statuses[home['team']['id']] = 'loss'
        else:
            statuses[home['team']['id']] = 'tie'
            statuses[away['team']['id']] = 'tie'

    return statuses


def _standings_row(entry: dict, indicator: str = '') -> str:
    stats = _stat_map(entry)
    team = entry['team'].get('shortDisplayName') or entry['team']['displayName']
    return (
        f"{indicator:<2}{stats.get('rank', '-'):>2} {team:<22.22} "
        f"{stats.get('gamesPlayed', '-'):>2} {stats.get('wins', '-'):>2} "
        f"{stats.get('ties', '-'):>2} {stats.get('losses', '-'):>2} "
        f"{stats.get('pointsFor', '-'):>3} {stats.get('pointsAgainst', '-'):>3} "
        f"{stats.get('pointDifferential', '-'):>4} {stats.get('points', '-'):>3}"
    )


def _format_standings_table(
    league_title: str,
    entries: list[dict],
    color_fn: Callable[[int, int], str | None] | None = None,
    live_status: dict[str, str] | None = None,
    limit: int = SOCCER_PAGE_LIMIT,
) -> list[str]:
    """Returns a list of Discord-ready message chunks for the standings
    table. A big table (e.g. UCL's 36-team league phase) can exceed an
    embed description's 4096-char limit, so rows are split across multiple
    messages -- each with its own header and closed code fence -- rather
    than letting a naive character-count split cut a fenced block in half.

    color_fn(rank, total) -> an ANSI color code (or None) lets callers
    highlight zones like relegation spots or UCL qualification cutoffs.
    live_status is the {team_id: 'win'|'loss'|'tie'} map from
    _live_status_by_team, marking teams currently mid-match."""
    live_status = live_status or {}
    total = len(entries)
    data_rows = []
    for entry in entries:
        indicator = LIVE_STATUS_EMOJI.get(live_status.get(entry['team']['id']), '')
        row = _standings_row(entry, indicator=indicator)
        if color_fn:
            rank = int(_stat_map(entry).get('rank') or 0)
            row = _colorize(row, color_fn(rank, total))
        data_rows.append(row)

    fence = "ansi" if color_fn else ""
    overhead = len(f"## {league_title} Standings\n```{fence}\n{STANDINGS_HEADER}\n{STANDINGS_SEPARATOR}\n```")
    longest_row = max((len(row) for row in data_rows), default=0) + 1
    rows_per_chunk = max(1, (limit - overhead) // longest_row)

    messages = []
    for i in range(0, len(data_rows), rows_per_chunk):
        chunk = data_rows[i:i + rows_per_chunk]
        suffix = "" if i == 0 else " (cont.)"
        body = "\n".join([STANDINGS_HEADER, STANDINGS_SEPARATOR, *chunk])
        messages.append(f"## {league_title} Standings{suffix}\n```{fence}\n{body}\n```")

    return messages or [f"No {league_title} standings available right now."]


def _league_standings(league_title: str, slug: str, color_fn: Callable[[int, int], str | None] | None = None) -> list[str]:
    try:
        data = _fetch_standings(slug)
        entries = data['children'][0]['standings']['entries']
    except Exception as e:
        logger.warning(f"_league_standings({league_title}) failed: {e}")
        return [f"Couldn't reach {league_title} standings right now. Try again later!"]

    entries = sorted(entries, key=lambda e: int(_stat_map(e).get('rank') or 0))
    live_status = _live_status_by_team(slug)
    return _format_standings_table(league_title, entries, color_fn=color_fn, live_status=live_status)


def premier_league_table() -> list[str]:
    """Returns the current Premier League standings (relegation zone in
    red) as a list of Discord-ready message chunks."""
    return _league_standings("Premier League", "eng.1", color_fn=_relegation_color)


def la_liga_table() -> list[str]:
    """Returns the current La Liga standings (relegation zone in red) as
    a list of Discord-ready message chunks."""
    return _league_standings("La Liga", "esp.1", color_fn=_relegation_color)


def _ucl_current_phase() -> str | None:
    """Returns the current UCL calendar phase label (e.g. 'League Phase',
    'Rd of 16', 'Final') by matching today's date against ESPN's own UCL
    calendar, or None if it can't be determined."""
    try:
        data = _fetch_scoreboard(_soccer_scoreboard_url('uefa.champions'))
        calendar_entries = data['leagues'][0]['calendar'][0]['entries']
    except Exception as e:
        logger.warning(f"_ucl_current_phase failed: {e}")
        return None

    now = datetime.datetime.now(datetime.timezone.utc)
    for entry in calendar_entries:
        try:
            start = datetime.datetime.fromisoformat(entry['startDate'].replace('Z', '+00:00'))
            end = datetime.datetime.fromisoformat(entry['endDate'].replace('Z', '+00:00'))
        except (KeyError, ValueError):
            continue
        if start <= now <= end:
            return entry.get('label')

    return None


def _format_bracket_line(event: dict) -> str:
    line = _format_game_line(event, is_soccer=True)

    competition = event['competitions'][0]
    series = competition.get('series')
    if series and series.get('completed'):
        agg_by_id = {c['id']: c.get('aggregateScore') for c in series.get('competitors', [])}
        home = next(c for c in competition['competitors'] if c['homeAway'] == 'home')
        away = next(c for c in competition['competitors'] if c['homeAway'] == 'away')
        home_agg = agg_by_id.get(home['id'])
        away_agg = agg_by_id.get(away['id'])
        if home_agg is not None and away_agg is not None:
            line += f" [Agg: {home['team']['displayName']} {home_agg:g} - {away_agg:g} {away['team']['displayName']}]"

    return line


def _ucl_bracket(phase_label: str) -> list[str]:
    """Returns the current knockout-round matchups (with aggregate score,
    when ESPN provides one for a two-legged tie) once UCL has moved past
    the league phase. This lists the active round's fixtures rather than a
    full multi-round tree, since later rounds aren't drawn/known yet."""
    try:
        data = _fetch_scoreboard(_soccer_scoreboard_url('uefa.champions'))
    except Exception as e:
        logger.warning(f"_ucl_bracket failed: {e}")
        return ["Couldn't reach the Champions League bracket right now. Try again later!"]

    events = data.get('events', [])
    if not events:
        return [f"## UEFA Champions League — {phase_label}\nNo matches scheduled yet for this round."]

    lines = [_format_bracket_line(event) for event in sorted(events, key=lambda e: e['date'])]
    return _chunk_ansi_block(f"## UEFA Champions League — {phase_label}", lines)


def ucl_table() -> list[str]:
    """Returns the UCL league-phase standings table, or the current
    knockout round's bracket matchups once the tournament moves past the
    league phase, as a list of Discord-ready message chunks."""
    phase = _ucl_current_phase()

    if phase and phase != 'League Phase':
        return _ucl_bracket(phase)

    return _league_standings("Champions League", "uefa.champions", color_fn=_ucl_zone_color)
