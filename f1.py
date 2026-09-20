import requests
import os
import json
import datetime
import concurrent.futures
from zoneinfo import ZoneInfo

# ESPN's public (unofficial, no key needed) racing API. The "site" API gives
# the race weekend's session list (with embedded driver names), while the
# "core" API is needed for per-driver results (place/time) and circuit info,
# since the site API's embedded competitors don't carry that data.
F1_SITE_SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/racing/f1/scoreboard"
F1_SITE_STANDINGS = "https://site.api.espn.com/apis/v2/sports/racing/f1/standings"
F1_CORE_BASE = "https://sports.core.api.espn.com/v2/sports/racing/leagues/f1"

BASE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE, 'f1_state.json')

EASTERN = ZoneInfo("America/New_York")

SESSION_LABELS = {
    'FP1': 'Free Practice 1',
    'FP2': 'Free Practice 2',
    'FP3': 'Free Practice 3',
    'SS': 'Sprint Qualifying',
    'SR': 'Sprint Race',
    'Qual': 'Qualifying',
    'Race': 'Race',
}

# Which qualifying-type session sets the grid for which race-type session,
# so the race-day announcement can recap the right one.
GRID_SESSION_FOR_RACE = {'Race': 'Qual', 'SR': 'SS'}

# Banner wording for each qualifying/race-type session, so sprint weekends
# get the same day-of announcement treatment as the main sessions.
QUALI_DAY_BANNERS = {'Qual': "QUALIFYING", 'SS': "SPRINT QUALIFYING"}
RACE_DAY_BANNERS = {'Race': "RACE", 'SR': "SPRINT RACE"}


def _fetch_json(url, params=None):
    response = requests.get(url, params=params, timeout=10)
    response.raise_for_status()
    return response.json()


def _load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r') as f:
            return json.load(f)
    return {}


def _save_state(state):
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f)


def _current_event():
    """Returns the current/next F1 race weekend from ESPN's scoreboard, or
    None if there isn't one."""
    data = _fetch_json(F1_SITE_SCOREBOARD)
    events = data.get('events', [])
    return events[0] if events else None


def _event_location(event_id):
    """Returns 'Circuit Name — City, Country' for the event, or None if it
    can't be resolved."""
    try:
        core_event = _fetch_json(f"{F1_CORE_BASE}/events/{event_id}")
        circuit = _fetch_json(core_event['circuit']['$ref'])
        address = circuit.get('address', {})
        place = ", ".join(p for p in [address.get('city'), address.get('country')] if p)
        return f"{circuit['fullName']} — {place}" if place else circuit.get('fullName')
    except Exception:
        return None


def _competitor_result(event_id, competition_id, competitor):
    """Returns (place, driver_name, total_time) for one driver in a
    session, or None if it can't be fetched."""
    try:
        url = f"{F1_CORE_BASE}/events/{event_id}/competitions/{competition_id}/competitors/{competitor['id']}/statistics"
        data = _fetch_json(url)
        stats = {s['name']: s['displayValue'] for s in data['splits']['categories'][0]['stats']}
        name = competitor['athlete']['displayName']
        return stats.get('place', '-'), name, stats.get('totalTime', '-')
    except Exception:
        return None


def _session_results_table(event_id, competition):
    """Returns a formatted, position-sorted results table for a session,
    or None if no results could be fetched (e.g. transient API hiccup --
    the caller should retry on a later poll rather than giving up)."""
    competitors = competition.get('competitors', [])
    if not competitors:
        return None

    def fetch(competitor):
        return _competitor_result(event_id, competition['id'], competitor)

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(competitors)) as executor:
        results = [r for r in executor.map(fetch, competitors) if r]

    if not results:
        return None

    def sort_key(result):
        try:
            return int(float(result[0]))
        except (TypeError, ValueError):
            return 999

    results.sort(key=sort_key)

    header = f"{'Pos':>3}  {'Driver':<22} Time"
    rows = [header, '-' * len(header)]
    for place, name, total_time in results:
        rows.append(f"{place:>3}  {name:<22} {total_time}")

    return "```\n" + "\n".join(rows) + "\n```"


def _grid_recap(event_id, competitions, race_abbrev):
    grid_abbrev = GRID_SESSION_FOR_RACE.get(race_abbrev)
    grid_session = next((c for c in competitions if c['type']['abbreviation'] == grid_abbrev), None)
    if not grid_session:
        return None

    table = _session_results_table(event_id, grid_session)
    if not table:
        return None

    label = SESSION_LABELS.get(grid_abbrev, grid_abbrev)
    return f"**{label} Recap:**\n{table}"


def check_f1_updates():
    """Checks the current F1 race weekend for new milestones -- race week
    start, each session's results once it finishes, and qualifying/race
    day -- and returns a list of message strings to post. Persists state
    to disk (keyed by event + session id) so nothing gets announced twice
    across restarts or repeated polls."""
    try:
        event = _current_event()
    except Exception:
        return []

    if not event:
        return []

    event_id = event['id']
    state = _load_state()
    if state.get('event_id') != event_id:
        state = {'event_id': event_id, 'done': []}

    done = state['done']
    messages = []
    today = datetime.datetime.now(EASTERN).date()

    weekend_start = datetime.datetime.fromisoformat(event['date'].replace('Z', '+00:00')).astimezone(EASTERN).date()
    if 'race_week' not in done and today >= weekend_start:
        is_sprint_weekend = any(
            c['type']['abbreviation'] in ('SS', 'SR') for c in event.get('competitions', [])
        )
        headline = "IT'S RACE WEEK + SPRINT!!!" if is_sprint_weekend else "IT'S RACE WEEK!!"
        location = _event_location(event_id) or "location TBD"
        messages.append(
            f"# {headline} 🏎️🏁\n**{event['name']}**\nWhere: {location}"
        )
        done.append('race_week')

    competitions = sorted(event.get('competitions', []), key=lambda c: c['date'])

    for competition in competitions:
        comp_id = competition['id']
        abbrev = competition['type']['abbreviation']
        label = SESSION_LABELS.get(abbrev, abbrev)
        comp_date = datetime.datetime.fromisoformat(competition['date'].replace('Z', '+00:00')).astimezone(EASTERN)
        completed = competition.get('status', {}).get('type', {}).get('completed', False)

        day_marker = f"day:{comp_id}"
        if abbrev in QUALI_DAY_BANNERS and day_marker not in done and today == comp_date.date():
            banner = QUALI_DAY_BANNERS[abbrev]
            messages.append(f"# IT'S {banner} DAY!! ⏱️🏎️\n**{label}** for the {event['name']}")
            done.append(day_marker)

        if abbrev in RACE_DAY_BANNERS and day_marker not in done and today == comp_date.date():
            banner = RACE_DAY_BANNERS[abbrev]
            recap = _grid_recap(event_id, competitions, abbrev)
            recap_text = f"\n\n{recap}" if recap else ""
            messages.append(f"# IT'S {banner} DAY!! 🏎️🏁\n**{event['name']}**{recap_text}")
            done.append(day_marker)

        results_marker = f"results:{comp_id}"
        if completed and results_marker not in done:
            table = _session_results_table(event_id, competition)
            if table:
                messages.append(f"## {label} Results — {event['name']}\n{table}")
                done.append(results_marker)

    _save_state(state)
    return messages


def f1_status():
    """Returns whether it's currently F1 race week -- the calendar week
    (Monday-Sunday) containing the race -- and if so, which session(s)
    are happening today."""
    try:
        event = _current_event()
    except Exception:
        return "Couldn't reach the F1 schedule right now. Try again later!"

    if not event:
        return "No upcoming F1 event found."

    first_session = datetime.datetime.fromisoformat(event['date'].replace('Z', '+00:00')).astimezone(EASTERN).date()
    race_week_start = first_session - datetime.timedelta(days=first_session.weekday())
    race_week_end = race_week_start + datetime.timedelta(days=6)
    today = datetime.datetime.now(EASTERN).date()

    if race_week_start <= today <= race_week_end:
        competitions = sorted(event.get('competitions', []), key=lambda c: c['date'])
        todays = [
            c for c in competitions
            if datetime.datetime.fromisoformat(c['date'].replace('Z', '+00:00')).astimezone(EASTERN).date() == today
        ]
        if todays:
            labels = ", ".join(SESSION_LABELS.get(c['type']['abbreviation'], c['type']['abbreviation']) for c in todays)
            today_text = f"\nToday: {labels}"
        else:
            today_text = ""
        return f"🏎️🏁 Yes, it's race week!\n**{event['name']}**{today_text}"

    days_until = (race_week_start - today).days
    day_word = "day" if days_until == 1 else "days"
    return (
        f"🚦 No, it's not race week.\n"
        f"Next up: **{event['name']}** in {days_until} {day_word} (week of {race_week_start.strftime('%B %d')})."
    )


def _f1_standings_table(title, entries, name_fn):
    def stat_map(entry):
        return {stat['name']: stat.get('displayValue') for stat in entry['stats']}

    def sort_key(entry):
        try:
            return int(float(stat_map(entry).get('rank') or 0))
        except (TypeError, ValueError):
            return 999

    entries = sorted(entries, key=sort_key)

    header = f"{'#':>2} {'Name':<24} {'Pts':>4}"
    rows = [header, '-' * len(header)]
    for entry in entries:
        stats = stat_map(entry)
        rank = stats.get('rank', '-')
        points = stats.get('championshipPts') or stats.get('points') or '-'
        rows.append(f"{rank:>2} {name_fn(entry):<24.24} {points:>4}")

    table = "\n".join(rows)
    return f"## {title}\n```\n{table}\n```"


def f1_standings():
    """Returns the current F1 drivers' and constructors' championship
    standings as a list of Discord-ready message chunks."""
    try:
        data = _fetch_json(F1_SITE_STANDINGS)
        children = {child['name']: child for child in data.get('children', [])}
    except Exception:
        return ["Couldn't reach F1 standings right now. Try again later!"]

    messages = []

    drivers = children.get('Driver Standings')
    if drivers:
        entries = drivers['standings']['entries']
        messages.append(_f1_standings_table("Driver Standings", entries, lambda e: e['athlete']['displayName']))

    constructors = children.get('Constructor Standings')
    if constructors:
        entries = constructors['standings']['entries']
        messages.append(_f1_standings_table("Constructor Standings", entries, lambda e: e['team']['displayName']))

    return messages or ["No F1 standings available right now."]
