import requests
import datetime
import concurrent.futures
from zoneinfo import ZoneInfo

# ESPN's public (unofficial, no key needed) scoreboard/standings API. The
# scoreboard always reflects the current match week/day for a league,
# including live scores, so no local state needs to be tracked between calls
# the way the manga release checks do.
ESPN_SITE_BASE = "https://site.api.espn.com/apis/site/v2/sports"
ESPN_STANDINGS_BASE = "https://site.api.espn.com/apis/v2/sports"
NFL_SCOREBOARD_URL = f"{ESPN_SITE_BASE}/football/nfl/scoreboard"
CFB_SCOREBOARD_URL = f"{ESPN_SITE_BASE}/football/college-football/scoreboard"
MLB_SCOREBOARD_URL = f"{ESPN_SITE_BASE}/baseball/mlb/scoreboard"

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


def _soccer_scoreboard_url(slug):
    return f"{ESPN_SITE_BASE}/soccer/{slug}/scoreboard"


def _fetch_scoreboard(url, date=None, params=None):
    request_params = dict(params or {})
    # ESPN's `dates` filter only accepts a single YYYYMMDD day, not a range.
    if date:
        request_params['dates'] = date.strftime('%Y%m%d')
    response = requests.get(url, params=request_params, timeout=10)
    response.raise_for_status()
    return response.json()


def _fetch_standings(slug):
    url = f"{ESPN_STANDINGS_BASE}/soccer/{slug}/standings"
    response = requests.get(url, timeout=10)
    response.raise_for_status()
    return response.json()


def _week_dates(start_weekday):
    """Returns the 7 dates of the week beginning on start_weekday
    (Monday=0 ... Sunday=6) that contains today, in Eastern time."""
    today = datetime.datetime.now(EASTERN).date()
    days_since_start = (today.weekday() - start_weekday) % 7
    week_start = today - datetime.timedelta(days=days_since_start)
    return [week_start + datetime.timedelta(days=i) for i in range(7)]


def _format_time(dt):
    return dt.strftime('%I:%M %p ET').lstrip('0')


def _format_game_line(event, is_soccer=False, label_fn=None):
    sport_emoji = '⚽' if is_soccer else '🏈'

    competition = event['competitions'][0]
    competitors = competition['competitors']
    home = next(c for c in competitors if c['homeAway'] == 'home')
    away = next(c for c in competitors if c['homeAway'] == 'away')

    label_fn = label_fn or (lambda c: c['team']['displayName'])
    home_name = f"**{label_fn(home)}**"
    away_name = f"**{label_fn(away)}**"

    game_time = datetime.datetime.fromisoformat(event['date'].replace('Z', '+00:00'))
    game_time = game_time.astimezone(EASTERN)
    day_label = game_time.strftime('%A (%m/%d)')

    status = competition.get('status', {})
    state = status.get('type', {}).get('state', 'pre')

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


def nfl_synopsis():
    """Returns a synopsis of this week's NFL games with live/final scores."""
    try:
        # ESPN's default NFL scoreboard already spans the full Thu-Sun-Mon
        # week as one "current week", so no date filtering is needed here.
        data = _fetch_scoreboard(NFL_SCOREBOARD_URL)
    except Exception:
        return "Couldn't reach the NFL scores right now. Try again later!"

    events = data.get('events', [])
    if not events:
        return "No NFL games scheduled this week."

    week_number = data.get('week', {}).get('number')
    header = f"## NFL Week {week_number} Games!" if week_number else "## This Week's NFL Games!"

    lines = [_format_game_line(event) for event in events]
    return header + "\n" + "\n".join(lines)


def _cfb_rank(competitor):
    rank = competitor.get('curatedRank', {}).get('current')
    return rank if rank and rank <= 25 else None


def _cfb_label(competitor):
    name = competitor['team']['displayName']
    rank = _cfb_rank(competitor)
    if rank:
        name = f"#{rank} {name}"
    if competitor['team']['id'] == USF_TEAM_ID:
        name = f"⭐ {name}"
    return name


def _cfb_involves_usf(event):
    return any(c['team']['id'] == USF_TEAM_ID for c in event['competitions'][0]['competitors'])


def _cfb_involves_ranked_team(event):
    return any(_cfb_rank(c) for c in event['competitions'][0]['competitors'])


def cfb_synopsis():
    """Returns a compressed synopsis of this week's Division I FBS college
    football games. The full FBS slate runs 60-75+ games a week -- far too
    much for one Discord message -- so this focuses on South Florida's game
    (always shown) plus every game involving a ranked (Top 25) team."""
    try:
        # ESPN's default scoreboard only returns a small curated subset.
        # groups=80 selects FBS and a high limit pulls the full week's slate.
        data = _fetch_scoreboard(CFB_SCOREBOARD_URL, params={'groups': 80, 'limit': 400})
    except Exception:
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


def _mlb_contending_team_ids(threshold=MLB_CONTENTION_THRESHOLD):
    """Returns the set of MLB team ids ESPN currently gives at least
    `threshold`% odds of making the playoffs. Every MLB series (30 teams,
    ~15 concurrent matchups) would be too much text, so this replaces a
    static "big market" list with teams that are actually still in the
    hunt right now."""
    try:
        response = requests.get(MLB_STANDINGS_URL, timeout=10)
        response.raise_for_status()
        data = response.json()
    except Exception:
        return set()

    contenders = set()
    for league in data.get('children', []):
        for entry in league.get('standings', {}).get('entries', []):
            stats = {stat['name']: stat for stat in entry['stats']}
            pct = stats.get('playoffPercent', {}).get('value')
            if pct is not None and pct >= threshold:
                contenders.add(entry['team']['id'])

    return contenders


def _mlb_series_record(games):
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


def _mlb_series_line(games):
    games = sorted(games, key=lambda e: e['date'])
    competition = games[0]['competitions'][0]
    competitors = competition['competitors']
    home_name = f"**{next(c for c in competitors if c['homeAway'] == 'home')['team']['shortDisplayName']}**"
    away_name = f"**{next(c for c in competitors if c['homeAway'] == 'away')['team']['shortDisplayName']}**"

    first_date = datetime.datetime.fromisoformat(games[0]['date'].replace('Z', '+00:00')).astimezone(EASTERN)
    last_date = datetime.datetime.fromisoformat(games[-1]['date'].replace('Z', '+00:00')).astimezone(EASTERN)
    if first_date.date() == last_date.date():
        date_range = first_date.strftime('%a')
    else:
        date_range = f"{first_date.strftime('%a')}-{last_date.strftime('%a')}"

    home_wins, away_wins, all_completed = _mlb_series_record(games)

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


def mlb_series_synopsis():
    """Returns this week's MLB matchups grouped into series (rather than
    every individual game, which would run 90+ games/week) with each
    series' overall record so far."""
    week_dates = _week_dates(start_weekday=0)

    events_by_id = {}
    for day in week_dates:
        try:
            data = _fetch_scoreboard(MLB_SCOREBOARD_URL, date=day)
        except Exception:
            continue
        for event in data.get('events', []):
            events_by_id[event['id']] = event

    if not events_by_id:
        return "No MLB games scheduled this week."

    contenders = _mlb_contending_team_ids()

    def is_featured(team_id):
        return team_id in FEATURED_MLB_TEAM_IDS or team_id in contenders

    series_map = {}
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


def _truncate_page(body, limit=SOCCER_PAGE_LIMIT):
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


def soccer_pages():
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

    def fetch(job):
        name, slug, day = job
        try:
            events = _fetch_scoreboard(_soccer_scoreboard_url(slug), date=day).get('events', [])
        except Exception:
            events = []
        return name, events

    events_by_competition = {name: {} for name, _ in SOCCER_COMPETITIONS}
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(32, len(jobs))) as executor:
        for name, events in executor.map(fetch, jobs):
            for event in events:
                events_by_competition[name][event['id']] = event

    pages = []
    for name, _ in SOCCER_COMPETITIONS:
        events = sorted(events_by_competition[name].values(), key=lambda e: e['date'])
        if not events:
            continue
        lines = [_format_game_line(event, is_soccer=True) for event in events]
        body = f"## {name}\n" + "\n".join(lines)
        pages.append((name, _truncate_page(body)))

    return pages


STANDINGS_HEADER = f"{'#':>2} {'Team':<22} {'P':>2} {'W':>2} {'D':>2} {'L':>2} {'GF':>3} {'GA':>3} {'GD':>4} {'Pts':>3}"
STANDINGS_SEPARATOR = '-' * len(STANDINGS_HEADER)

# Discord renders a subset of ANSI codes inside a ```ansi block (desktop/web
# only -- mobile just shows the plain, uncolored text).
ANSI_RESET = "\u001b[0m"
ANSI_RED = "\u001b[0;31m"
ANSI_GREEN = "\u001b[0;32m"
ANSI_BLUE = "\u001b[0;34m"
ANSI_WHITE = "\u001b[0;37m"


def _colorize(text, code):
    return text if code is None else f"{code}{text}{ANSI_RESET}"


def _relegation_color(rank, total):
    """Bottom 3 spots (the relegation zone) are colored red."""
    return ANSI_RED if rank > total - 3 else None


def _ucl_zone_color(rank, total):
    """Top 8 (direct Round of 16 qualification) green, 9-24 (knockout
    round playoffs) blue, the rest (eliminated) white."""
    if rank <= 8:
        return ANSI_GREEN
    if rank <= 24:
        return ANSI_BLUE
    return ANSI_WHITE


def _stat_map(entry):
    return {stat['name']: stat['displayValue'] for stat in entry['stats']}


def _standings_row(entry):
    stats = _stat_map(entry)
    team = entry['team'].get('shortDisplayName') or entry['team']['displayName']
    return (
        f"{stats.get('rank', '-'):>2} {team:<22.22} "
        f"{stats.get('gamesPlayed', '-'):>2} {stats.get('wins', '-'):>2} "
        f"{stats.get('ties', '-'):>2} {stats.get('losses', '-'):>2} "
        f"{stats.get('pointsFor', '-'):>3} {stats.get('pointsAgainst', '-'):>3} "
        f"{stats.get('pointDifferential', '-'):>4} {stats.get('points', '-'):>3}"
    )


def _format_standings_table(league_title, entries, color_fn=None, limit=1900):
    """Returns a list of Discord-ready message chunks for the standings
    table. A big table (e.g. UCL's 36-team league phase) can exceed
    Discord's 2000-char limit, so rows are split across multiple messages
    -- each with its own header and closed code fence -- rather than
    letting a naive character-count split cut a fenced block in half.

    color_fn(rank, total) -> an ANSI color code (or None) lets callers
    highlight zones like relegation spots or UCL qualification cutoffs."""
    total = len(entries)
    data_rows = []
    for entry in entries:
        row = _standings_row(entry)
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


def _league_standings(league_title, slug, color_fn=None):
    try:
        data = _fetch_standings(slug)
        entries = data['children'][0]['standings']['entries']
    except Exception:
        return [f"Couldn't reach {league_title} standings right now. Try again later!"]

    entries = sorted(entries, key=lambda e: int(_stat_map(e).get('rank') or 0))
    return _format_standings_table(league_title, entries, color_fn=color_fn)


def premier_league_table():
    """Returns the current Premier League standings (relegation zone in
    red) as a list of Discord-ready message chunks."""
    return _league_standings("Premier League", "eng.1", color_fn=_relegation_color)


def la_liga_table():
    """Returns the current La Liga standings (relegation zone in red) as
    a list of Discord-ready message chunks."""
    return _league_standings("La Liga", "esp.1", color_fn=_relegation_color)


def _ucl_current_phase():
    """Returns the current UCL calendar phase label (e.g. 'League Phase',
    'Rd of 16', 'Final') by matching today's date against ESPN's own UCL
    calendar, or None if it can't be determined."""
    try:
        data = _fetch_scoreboard(_soccer_scoreboard_url('uefa.champions'))
        calendar_entries = data['leagues'][0]['calendar'][0]['entries']
    except Exception:
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


def _format_bracket_line(event):
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


def _ucl_bracket(phase_label):
    """Returns the current knockout-round matchups (with aggregate score,
    when ESPN provides one for a two-legged tie) once UCL has moved past
    the league phase. This lists the active round's fixtures rather than a
    full multi-round tree, since later rounds aren't drawn/known yet."""
    try:
        data = _fetch_scoreboard(_soccer_scoreboard_url('uefa.champions'))
    except Exception:
        return ["Couldn't reach the Champions League bracket right now. Try again later!"]

    events = data.get('events', [])
    if not events:
        return [f"## UEFA Champions League — {phase_label}\nNo matches scheduled yet for this round."]

    lines = [_format_bracket_line(event) for event in sorted(events, key=lambda e: e['date'])]
    return [f"## UEFA Champions League — {phase_label}\n" + "\n".join(lines)]


def ucl_table():
    """Returns the UCL league-phase standings table, or the current
    knockout round's bracket matchups once the tournament moves past the
    league phase, as a list of Discord-ready message chunks."""
    phase = _ucl_current_phase()

    if phase and phase != 'League Phase':
        return _ucl_bracket(phase)

    return _league_standings("Champions League", "uefa.champions", color_fn=_ucl_zone_color)
