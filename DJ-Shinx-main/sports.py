import requests
import datetime
from zoneinfo import ZoneInfo

# ESPN's public (unofficial, no key needed) scoreboard API. It always reflects
# the current match week/day for a league, including live scores, so no local
# state needs to be tracked between calls the way the manga release checks do.
ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports"
NFL_SCOREBOARD_URL = f"{ESPN_BASE}/football/nfl/scoreboard"

SOCCER_LEAGUES = [
    ("Premier League", f"{ESPN_BASE}/soccer/eng.1/scoreboard"),
    ("La Liga", f"{ESPN_BASE}/soccer/esp.1/scoreboard"),
    ("Champions League", f"{ESPN_BASE}/soccer/uefa.champions/scoreboard"),
]

EASTERN = ZoneInfo("America/New_York")


def _fetch_scoreboard(url):
    response = requests.get(url, timeout=10)
    response.raise_for_status()
    return response.json()


def _format_time(dt):
    return dt.strftime('%I:%M %p ET').lstrip('0')


def _format_game_line(event, is_soccer=False):
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
            return (f"🔴 {day_label}: {away_name} {away['score']} @ "
                    f"{home_name} {home['score']} ({clock})")
        period = status.get('period', '')
        return (f"🔴 {day_label}: {away_name} {away['score']} @ "
                f"{home_name} {home['score']} (Q{period}, {clock})")
    elif state == 'post':
        detail = status.get('type', {}).get('description', 'Final')
        return (f"{day_label}: {away_name} {away['score']} @ "
                f"{home_name} {home['score']} ({detail})")
    else:
        return f"{day_label}: {away_name} @ {home_name} — {_format_time(game_time)}"


def nfl_synopsis():
    """Returns a synopsis of this week's NFL games with live/final scores."""
    try:
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
    sections = []
    for league_name, url in SOCCER_LEAGUES:
        try:
            data = _fetch_scoreboard(url)
        except Exception:
            continue

        events = data.get('events', [])
        if not events:
            continue

        lines = [_format_game_line(event, is_soccer=True) for event in events]
        sections.append(f"**{league_name}**\n" + "\n".join(lines))

    if not sections:
        return "Couldn't find any soccer matches this week."

    return "## This Week's Soccer Matches!\n\n" + "\n\n".join(sections)
