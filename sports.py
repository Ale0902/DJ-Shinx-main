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

SOCCER_LEAGUES = [
    ("Premier League", "eng.1"),
    ("La Liga", "esp.1"),
    ("Champions League", "uefa.champions"),
]

EASTERN = ZoneInfo("America/New_York")


def _soccer_scoreboard_url(slug):
    return f"{ESPN_SITE_BASE}/soccer/{slug}/scoreboard"


def _fetch_scoreboard(url, date=None):
    # ESPN's `dates` filter only accepts a single YYYYMMDD day, not a range.
    params = {'dates': date.strftime('%Y%m%d')} if date else {}
    response = requests.get(url, params=params, timeout=10)
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


def _format_game_line(event, is_soccer=False):
    sport_emoji = '⚽' if is_soccer else '🏈'

    competition = event['competitions'][0]
    competitors = competition['competitors']
    home = next(c for c in competitors if c['homeAway'] == 'home')
    away = next(c for c in competitors if c['homeAway'] == 'away')

    home_name = home['team']['displayName']
    away_name = away['team']['displayName']

    game_time = datetime.datetime.fromisoformat(event['date'].replace('Z', '+00:00'))
    game_time = game_time.astimezone(EASTERN)
    day_label = game_time.strftime('%A (%m/%d)')

    status = competition.get('status', {})
    state = status.get('type', {}).get('state', 'pre')

    if state == 'in':
        clock = status.get('displayClock', '')
        if is_soccer:
            return (f"{sport_emoji} 🔴 {day_label}: {away_name} {away['score']} @ "
                    f"{home_name} {home['score']} ({clock})")
        period = status.get('period', '')
        return (f"{sport_emoji} 🔴 {day_label}: {away_name} {away['score']} @ "
                f"{home_name} {home['score']} (Q{period}, {clock})")
    elif state == 'post':
        detail = status.get('type', {}).get('description', 'Final')
        return (f"{sport_emoji} {day_label}: {away_name} {away['score']} @ "
                f"{home_name} {home['score']} ({detail})")
    else:
        return f"{sport_emoji} {day_label}: {away_name} @ {home_name} — {_format_time(game_time)}"


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


def soccer_synopsis():
    """Returns a synopsis of this week's Premier League, La Liga, and
    Champions League matches with live/final scores."""
    # ESPN's soccer scoreboard only accepts a single day at a time, and its
    # default "current" window doesn't reliably include Sunday, so fetch
    # every day of the calendar week (Monday-through-Sunday) and merge.
    week_dates = _week_dates(start_weekday=0)

    def fetch_day(args):
        league_name, slug, day = args
        try:
            url = _soccer_scoreboard_url(slug)
            return league_name, _fetch_scoreboard(url, date=day).get('events', [])
        except Exception:
            return league_name, []

    jobs = [(league_name, slug, day) for league_name, slug in SOCCER_LEAGUES for day in week_dates]
    events_by_league = {league_name: {} for league_name, _ in SOCCER_LEAGUES}

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(jobs)) as executor:
        for league_name, events in executor.map(fetch_day, jobs):
            for event in events:
                events_by_league[league_name][event['id']] = event

    sections = []
    for league_name, _ in SOCCER_LEAGUES:
        events = sorted(events_by_league[league_name].values(), key=lambda e: e['date'])
        if not events:
            continue

        lines = [_format_game_line(event, is_soccer=True) for event in events]
        sections.append(f"**{league_name}**\n" + "\n".join(lines))

    if not sections:
        return "Couldn't find any soccer matches this week."

    return "## This Week's Soccer Matches!\n\n" + "\n\n".join(sections)


STANDINGS_HEADER = f"{'#':>2} {'Team':<22} {'P':>2} {'W':>2} {'D':>2} {'L':>2} {'GF':>3} {'GA':>3} {'GD':>4} {'Pts':>3}"
STANDINGS_SEPARATOR = '-' * len(STANDINGS_HEADER)


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


def _format_standings_table(league_title, entries, limit=1900):
    """Returns a list of Discord-ready message chunks for the standings
    table. A big table (e.g. UCL's 36-team league phase) can exceed
    Discord's 2000-char limit, so rows are split across multiple messages
    -- each with its own header and closed code fence -- rather than
    letting a naive character-count split cut a fenced block in half."""
    data_rows = [_standings_row(entry) for entry in entries]

    overhead = len(f"## {league_title} Standings\n```\n{STANDINGS_HEADER}\n{STANDINGS_SEPARATOR}\n```")
    longest_row = max((len(row) for row in data_rows), default=0) + 1
    rows_per_chunk = max(1, (limit - overhead) // longest_row)

    messages = []
    for i in range(0, len(data_rows), rows_per_chunk):
        chunk = data_rows[i:i + rows_per_chunk]
        suffix = "" if i == 0 else " (cont.)"
        body = "\n".join([STANDINGS_HEADER, STANDINGS_SEPARATOR, *chunk])
        messages.append(f"## {league_title} Standings{suffix}\n```\n{body}\n```")

    return messages or [f"No {league_title} standings available right now."]


def _league_standings(league_title, slug):
    try:
        data = _fetch_standings(slug)
        entries = data['children'][0]['standings']['entries']
    except Exception:
        return [f"Couldn't reach {league_title} standings right now. Try again later!"]

    entries = sorted(entries, key=lambda e: int(_stat_map(e).get('rank') or 0))
    return _format_standings_table(league_title, entries)


def premier_league_table():
    """Returns the current Premier League standings as a list of
    Discord-ready message chunks."""
    return _league_standings("Premier League", "eng.1")


def la_liga_table():
    """Returns the current La Liga standings as a list of Discord-ready
    message chunks."""
    return _league_standings("La Liga", "esp.1")


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
            line += f" [Agg: {away['team']['displayName']} {away_agg:g} - {home_agg:g} {home['team']['displayName']}]"

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

    return _league_standings("Champions League", "uefa.champions")
