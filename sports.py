import requests
import datetime
import time
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

SOCCER_PAGE_LIMIT = 1900  # leaves headroom under Discord's 2000-char cap

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

    # Once there's a score to show, bold whichever team is ahead (or has
    # won) instead of bolding both team names -- a tie bolds neither.
    if state in ('in', 'post'):
        try:
            home_score, away_score = int(home['score']), int(away['score'])
        except (KeyError, ValueError, TypeError):
            home_score = away_score = 0
        if home_score > away_score:
            home_name = f"**{home_name}**"
        elif away_score > home_score:
            away_name = f"**{away_name}**"

    if is_soccer:
        matchup = f"{home_name} vs {away_name}"
        score = f"**{home['score']}-{away['score']}**"
    else:
        matchup = f"{away_name} @ {home_name}"
        score = f"**{away['score']}-{home['score']}**"

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


def nfl_synopsis() -> str:
    """Returns a synopsis of this week's NFL games with live/final scores."""
    try:
        # ESPN's default NFL scoreboard already spans the full Thu-Sun-Mon
        # week as one "current week", so no date filtering is needed here.
        data = _fetch_scoreboard(NFL_SCOREBOARD_URL)
    except Exception as e:
        logger.warning(f"nfl_synopsis failed: {e}")
        return "Couldn't reach the NFL scores right now. Try again later!"

    events = data.get('events', [])
    if not events:
        return "No NFL games scheduled this week."

    week_number = data.get('week', {}).get('number')
    header = f"## NFL Week {week_number} Games!" if week_number else "## This Week's NFL Games!"

    lines = [_format_game_line(event) for event in events]
    return header + "\n" + "\n".join(lines)


def nfl_live_matches() -> str:
    """Returns a synopsis of only the NFL games currently in progress,
    with their current score and time remaining -- unlike /nfl, this
    excludes finished and upcoming games entirely."""
    try:
        data = _fetch_scoreboard(NFL_SCOREBOARD_URL)
    except Exception as e:
        logger.warning(f"nfl_live_matches failed: {e}")
        return "Couldn't reach the NFL scores right now. Try again later!"

    events = [event for event in data.get('events', []) if _is_live(event)]
    if not events:
        return "No NFL games currently in progress."

    lines = [_format_game_line(event) for event in events]
    return "## Live NFL Right Now!\n" + "\n".join(lines)


def nfl_results_today() -> str:
    """Returns the final score of every NFL game that finished today --
    unlike /nfl, this excludes in-progress and upcoming games entirely."""
    try:
        data = _fetch_scoreboard(NFL_SCOREBOARD_URL)
    except Exception as e:
        logger.warning(f"nfl_results_today failed: {e}")
        return "Couldn't reach the NFL scores right now. Try again later!"

    today = datetime.datetime.now(EASTERN).date()
    events = [event for event in data.get('events', []) if _is_final(event) and _event_date(event) == today]
    if not events:
        return "No NFL games have finished today."

    lines = [_format_game_line(event) for event in events]
    return "## Today's NFL Results!\n" + "\n".join(lines)


def _nfl_stat(entry: dict, name: str) -> str | None:
    for stat in entry['stats']:
        if stat['name'] == name:
            return stat.get('displayValue')
    return None


def _fetch_nfl_standings() -> dict:
    # level=3 asks ESPN for conference -> division -> team, instead of the
    # default conference -> team grouping which loses division info.
    return _get_json(NFL_STANDINGS_URL, params={'level': 3})


def _nfl_team_line(entry: dict) -> str:
    team = entry['team'].get('shortDisplayName') or entry['team']['displayName']
    record = _nfl_stat(entry, 'overall') or '-'
    streak = _nfl_stat(entry, 'streak')
    return f"{team} — {record} ({streak})" if streak else f"{team} — {record}"


def _nfl_division_page(standings_data: dict) -> str:
    lines = ["## NFL Standings by Division"]
    for conference in standings_data.get('children', []):
        for division in conference.get('children', []):
            entries = division.get('standings', {}).get('entries', [])
            entries.sort(key=lambda e: float(_nfl_stat(e, 'winPercent') or 0), reverse=True)
            lines.append(f"\n**{division['name']}**")
            lines.extend(_nfl_team_line(entry) for entry in entries)
    return "\n".join(lines)


def _nfl_playoff_page(standings_data: dict) -> str:
    lines = ["## NFL Playoff Picture"]
    for conference in standings_data.get('children', []):
        entries = [
            entry
            for division in conference.get('children', [])
            for entry in division.get('standings', {}).get('entries', [])
        ]
        entries.sort(key=lambda e: int(_nfl_stat(e, 'playoffSeed') or 99))

        division_leaders = entries[:4]
        wild_card = entries[4:NFL_PLAYOFF_SPOTS]
        in_the_hunt = entries[NFL_PLAYOFF_SPOTS:NFL_PLAYOFF_SPOTS + NFL_HUNT_SIZE]

        lines.append(f"\n**{conference['name']}**")
        lines.append("Division Leaders:")
        lines.extend(f"  {i}. {_nfl_team_line(e)}" for i, e in enumerate(division_leaders, start=1))
        lines.append("Wild Card:")
        lines.extend(f"  {i}. {_nfl_team_line(e)}" for i, e in enumerate(wild_card, start=5))
        if in_the_hunt:
            lines.append("In the Hunt:")
            lines.extend(f"  {_nfl_team_line(e)}" for e in in_the_hunt)

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


def cfb_synopsis() -> str:
    """Returns a compressed synopsis of this week's Division I FBS college
    football games. The full FBS slate runs 60-75+ games a week -- far too
    much for one Discord message -- so this focuses on South Florida's game
    (always shown) plus every game involving a ranked (Top 25) team."""
    try:
        # ESPN's default scoreboard only returns a small curated subset.
        # groups=80 selects FBS and a high limit pulls the full week's slate.
        data = _fetch_scoreboard(CFB_SCOREBOARD_URL, params={'groups': 80, 'limit': 400})
    except Exception as e:
        logger.warning(f"cfb_synopsis failed: {e}")
        return "Couldn't reach the college football scores right now. Try again later!"

    events = data.get('events', [])
    if not events:
        return "No college football games scheduled this week."

    week_number = data.get('week', {}).get('number')
    header = f"## College Football Week {week_number}!" if week_number else "## This Week's College Football Games!"

    usf_games = sorted((e for e in events if _cfb_involves_usf(e)), key=lambda e: e['date'])
    usf_event_ids = {e['id'] for e in usf_games}
    ranked_games = sorted(
        (e for e in events if e['id'] not in usf_event_ids and _cfb_involves_ranked_team(e)),
        key=lambda e: e['date'],
    )

    sections = []
    if usf_games:
        lines = [_format_game_line(event, label_fn=_cfb_label) for event in usf_games]
        sections.append("**South Florida Bulls**\n" + "\n".join(lines))

    if ranked_games:
        lines = [_format_game_line(event, label_fn=_cfb_label) for event in ranked_games]
        sections.append("**Top 25 Games**\n" + "\n".join(lines))

    if not sections:
        return header + "\nNo ranked matchups or USF game found this week."

    return header + "\n\n" + "\n\n".join(sections)


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

    # Bold whichever team is ahead (or has won) the series instead of
    # bolding both team names -- a tied series bolds neither.
    if home_wins > away_wins:
        home_name = f"**{home_name}**"
    elif away_wins > home_wins:
        away_name = f"**{away_name}**"

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


def mlb_series_synopsis() -> str:
    """Returns this week's MLB matchups grouped into series (rather than
    every individual game, which would run 90+ games/week) with each
    series' overall record so far."""
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
        return "No MLB games scheduled this week."

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
        return "No notable MLB series found this week."

    lines = sorted((_mlb_series_line(games) for games in series_map.values()), key=lambda item: item[0])

    header = "## This Week's MLB Series!"
    return header + "\n" + "\n".join(line for _, line in lines)


def _truncate_page(body: str, limit: int = SOCCER_PAGE_LIMIT) -> str:
    """Trims a page's match list to fit Discord's message limit, noting how
    many matches were cut rather than letting a busy competition (e.g. World
    Cup Qualifiers across five confederations) overflow the message."""
    if len(body) <= limit:
        return body

    lines = body.split("\n")
    kept = []
    total = 0
    for line in lines:
        total += len(line) + 1
        if total > limit:
            break
        kept.append(line)

    remaining = len(lines) - len(kept)
    if remaining > 0:
        kept.append(f"...and {remaining} more match{'es' if remaining != 1 else ''}.")
    return "\n".join(kept)


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
        lines = [_format_game_line(event, is_soccer=True) for event in events]
        body = f"## {name}\n" + "\n".join(lines)
        pages.append((name, _truncate_page(body)))

    return pages


def _is_live(event: dict) -> bool:
    status = event['competitions'][0].get('status', {})
    return status.get('type', {}).get('state') == 'in'


def _is_final(event: dict) -> bool:
    status = event['competitions'][0].get('status', {})
    return status.get('type', {}).get('state') == 'post'


def _event_date(event: dict) -> datetime.date:
    return datetime.datetime.fromisoformat(event['date'].replace('Z', '+00:00')).astimezone(EASTERN).date()


def _match_event_lines(competition: dict) -> list[str]:
    """Returns "12' ⚽ Player Name (Team)" / "34' 🟥 Player Name (Team)"
    lines, in chronological order, for every goal and red card in a
    competition's play-by-play event log ('details')."""
    team_names = {
        c['team']['id']: c['team'].get('shortDisplayName') or c['team']['displayName']
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
        team_name = team_names.get(detail.get('team', {}).get('id'), '')

        if is_goal:
            tag = ' (OG)' if detail.get('ownGoal') else ' (pen)' if detail.get('penaltyKick') else ''
            text = f"⚽ {name}{tag} ({team_name})"
        else:
            text = f"🟥 {name} ({team_name})"

        events.append((clock.get('value', 0), f"     {clock.get('displayValue', '')} {text}"))

    events.sort(key=lambda e: e[0])
    return [line for _, line in events]


def _format_live_soccer_line(event: dict) -> str:
    """A live match's score line plus, indented beneath it, each goal and
    red card so far with who was involved and the minute it happened."""
    lines = [_format_game_line(event, is_soccer=True)]
    lines.extend(_match_event_lines(event['competitions'][0]))
    return "\n".join(lines)


def live_soccer_matches() -> str:
    """Returns a single synopsis of only the soccer matches currently in
    progress, across every tracked competition -- unlike /soccer, this
    excludes finished and upcoming matches entirely."""
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

    sections = []
    for name, _ in SOCCER_COMPETITIONS:
        events = sorted(live_by_competition[name].values(), key=lambda e: e['date'])
        if not events:
            continue
        lines = [_format_live_soccer_line(event) for event in events]
        sections.append(f"**{name}**\n" + "\n".join(lines))

    if not sections:
        return "No soccer matches currently in progress."

    return "## Live Soccer Right Now!\n\n" + "\n\n".join(sections)


def soccer_results_today() -> str:
    """Returns the final score of every soccer match that finished today,
    across every tracked competition -- unlike /soccer, this excludes
    in-progress and upcoming matches entirely."""
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

    sections = []
    for name, _ in SOCCER_COMPETITIONS:
        events = sorted(results_by_competition[name].values(), key=lambda e: e['date'])
        if not events:
            continue
        lines = [_format_game_line(event, is_soccer=True) for event in events]
        sections.append(f"**{name}**\n" + "\n".join(lines))

    if not sections:
        return "No soccer results yet today."

    return "## Today's Soccer Results!\n\n" + "\n\n".join(sections)


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
    limit: int = 1900,
) -> list[str]:
    """Returns a list of Discord-ready message chunks for the standings
    table. A big table (e.g. UCL's 36-team league phase) can exceed
    Discord's 2000-char limit, so rows are split across multiple messages
    -- each with its own header and closed code fence -- rather than
    letting a naive character-count split cut a fenced block in half.

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
    return [f"## UEFA Champions League — {phase_label}\n" + "\n".join(lines)]


def ucl_table() -> list[str]:
    """Returns the UCL league-phase standings table, or the current
    knockout round's bracket matchups once the tournament moves past the
    league phase, as a list of Discord-ready message chunks."""
    phase = _ucl_current_phase()

    if phase and phase != 'League Phase':
        return _ucl_bracket(phase)

    return _league_standings("Champions League", "uefa.champions", color_fn=_ucl_zone_color)
