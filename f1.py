import requests
import os
import json
import time
import logging
import datetime
import threading
import concurrent.futures
from dataclasses import dataclass
from typing import Any, Callable
from zoneinfo import ZoneInfo

import sports

logger = logging.getLogger(__name__)

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

# Shared thread pool for fan-out fetches (e.g. one request per driver for a
# session's results). Reused across calls instead of spinning up a fresh
# pool per invocation.
_executor = concurrent.futures.ThreadPoolExecutor(max_workers=32)


# Discord renders only a small ANSI subset inside a ```ansi block: eight
# foreground colours plus a bold flag. The 2026 grid has eleven teams, so
# bold is used as a second dimension -- each hue carries at most two
# teams, paired so the two are easy to tell apart (Ferrari red vs Haas
# bold red, Red Bull blue vs Williams bold blue).
ANSI_RESET = "\u001b[0m"
_RED = "\u001b[0;31m"
_BOLD_RED = "\u001b[1;31m"
_GREEN = "\u001b[0;32m"
_BOLD_GREEN = "\u001b[1;32m"
_YELLOW = "\u001b[0;33m"
_BLUE = "\u001b[0;34m"
_BOLD_BLUE = "\u001b[1;34m"
_PINK = "\u001b[0;35m"
_CYAN = "\u001b[0;36m"
_BOLD_CYAN = "\u001b[1;36m"
_WHITE = "\u001b[0;37m"

# Keyed by the team names ESPN actually returns. Each is mapped to the
# closest thing the palette has to the real livery.
TEAM_COLORS = {
    'Ferrari': _RED,
    'Haas': _BOLD_RED,
    'Aston Martin': _GREEN,
    'Audi': _BOLD_GREEN,
    'McLaren': _YELLOW,          # papaya; yellow is the nearest available
    'Red Bull': _BLUE,
    'Williams': _BOLD_BLUE,
    'Mercedes': _CYAN,           # petronas teal
    'Racing Bulls': _BOLD_CYAN,
    'Alpine': _PINK,
    'Cadillac': _WHITE,
}

# ESPN has renamed teams mid-era before (Toro Rosso -> AlphaTauri -> RB ->
# Racing Bulls, Sauber -> Kick Sauber -> Audi). Rather than lose a team's
# colour the day that happens, these older and alternate spellings map
# onto whichever current entry they became.
TEAM_ALIASES = {
    'kick sauber': 'Audi',
    'sauber': 'Audi',
    'stake': 'Audi',
    'alphatauri': 'Racing Bulls',
    'toro rosso': 'Racing Bulls',
    'rb': 'Racing Bulls',
    'red bull racing': 'Red Bull',
    'alfa romeo': 'Audi',
    'force india': 'Aston Martin',
    'racing point': 'Aston Martin',
    'renault': 'Alpine',
}

# Longest first, so "Racing Bulls" is tested before "Red Bull" and a name
# containing both words can't match the shorter one by accident.
_TEAM_MATCH_ORDER = sorted(TEAM_COLORS, key=len, reverse=True)

# A driver's team comes from their athlete record, one request each, so
# it's cached well past a race weekend -- a seat changes at most a couple
# of times a season and a restart re-reads it anyway.
_DRIVER_TEAM_TTL_SECONDS = 12 * 3600
_driver_team_cache: dict[str, tuple[float, str | None]] = {}
_driver_team_lock = threading.Lock()


def team_color(team_name: str | None) -> str | None:
    """The ANSI code for a team, matched leniently so a renamed or
    slightly differently spelled team still gets its colour instead of
    silently falling back to plain text."""
    if not team_name:
        return None
    if team_name in TEAM_COLORS:
        return TEAM_COLORS[team_name]

    lowered = team_name.lower()
    for alias, canonical in TEAM_ALIASES.items():
        if alias in lowered:
            return TEAM_COLORS.get(canonical)
    for known in _TEAM_MATCH_ORDER:
        if known.lower() in lowered:
            return TEAM_COLORS[known]
    return None


def _colorize(text: str, code: str | None) -> str:
    return text if code is None else f"{code}{text}{ANSI_RESET}"


def _fetch_driver_team(athlete_id: str) -> str | None:
    """The team a driver currently races for. Their athlete record is the
    only place ESPN exposes this -- neither the standings entries nor a
    session's competitors carry a team at all."""
    try:
        data = _fetch_json(f"{F1_CORE_BASE}/athletes/{athlete_id}")
        vehicles = data.get('vehicles') or []
        return (vehicles[0].get('team') if vehicles else None) or None
    except Exception as e:
        logger.debug(f"_fetch_driver_team failed for athlete {athlete_id}: {e}")
        return None


def driver_teams(athlete_ids: list[str]) -> dict[str, str | None]:
    """{athlete_id: team_name} for a whole table at once. One request per
    driver not already cached, run in parallel -- a full 23-driver grid
    takes about a second cold and nothing at all afterwards."""
    now = time.time()
    known: dict[str, str | None] = {}
    missing = []

    with _driver_team_lock:
        for athlete_id in athlete_ids:
            cached = _driver_team_cache.get(athlete_id)
            if cached and now - cached[0] < _DRIVER_TEAM_TTL_SECONDS:
                known[athlete_id] = cached[1]
            else:
                missing.append(athlete_id)

    if missing:
        fetched = list(_executor.map(_fetch_driver_team, missing))
        with _driver_team_lock:
            for athlete_id, team in zip(missing, fetched):
                _driver_team_cache[athlete_id] = (now, team)
                known[athlete_id] = team

    return known


def _driver_cell(name: str, width: int, team: str | None) -> str:
    """A fixed-width driver name, coloured by team. Padded *before* the
    escape codes go on: ANSI sequences are characters too, so colouring
    first and padding after makes every coloured cell count ~11 invisible
    characters toward its width and pulls the column out of line."""
    return _colorize(f"{name:<{width}.{width}}", team_color(team))

# Short-lived cache, mirroring sports.py's -- avoids duplicate round-trips
# to ESPN's unofficial API within the same poll/command (e.g. the grid
# recap and the race results both touching the same event during one
# check_f1_updates() tick).
_CACHE_TTL_SECONDS = 15
_response_cache: dict[tuple, tuple[float, Any]] = {}


def _fetch_json(url: str, params: dict | None = None) -> Any:
    key = (url, tuple(sorted((params or {}).items())))
    cached = _response_cache.get(key)
    now = time.monotonic()
    if cached and now - cached[0] < _CACHE_TTL_SECONDS:
        return cached[1]

    response = requests.get(url, params=params, timeout=10)
    response.raise_for_status()
    data = response.json()
    _response_cache[key] = (now, data)
    if len(_response_cache) > sports._CACHE_SWEEP_SIZE:
        # Every driver's results are their own URL, so this grows with
        # each race weekend unless it's swept.
        sports._sweep_cache(_response_cache, now, _CACHE_TTL_SECONDS)
    return data


def _load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r') as f:
            return json.load(f)
    return {}


def _save_state(state: dict) -> None:
    # Write to a temp file and rename over the target so a crash mid-write
    # (or an overlapping poll) can't leave a truncated/corrupt state file.
    tmp_path = STATE_FILE + '.tmp'
    with open(tmp_path, 'w') as f:
        json.dump(state, f)
    os.replace(tmp_path, STATE_FILE)


def _current_event() -> dict | None:
    """Returns the current/next F1 race weekend from ESPN's scoreboard, or
    None if there isn't one."""
    data = _fetch_json(F1_SITE_SCOREBOARD)
    events = data.get('events', [])
    return events[0] if events else None


def _event_location(event_id: str) -> str | None:
    """Returns 'Circuit Name — City, Country' for the event, or None if it
    can't be resolved."""
    try:
        core_event = _fetch_json(f"{F1_CORE_BASE}/events/{event_id}")
        circuit = _fetch_json(core_event['circuit']['$ref'])
        address = circuit.get('address', {})
        place = ", ".join(p for p in [address.get('city'), address.get('country')] if p)
        return f"{circuit['fullName']} — {place}" if place else circuit.get('fullName')
    except Exception as e:
        logger.debug(f"_event_location failed for event {event_id}: {e}")
        return None


# What a results row shows for a driver with no time: one who didn't set a
# lap or didn't finish -- or anyone, when a session has only just ended and
# ESPN hasn't filled the times in yet.
NO_TIME = '-'


def _competitor_result(event_id: str, competition_id: str, competitor: dict) -> tuple[str, str, str, str] | None:
    """Returns (place, driver_name, total_time, athlete_id) for one driver
    in a session, or None if it can't be fetched. The athlete id rides
    along so the row can be coloured by the driver's team -- a session's
    competitors carry no team of their own."""
    try:
        url = f"{F1_CORE_BASE}/events/{event_id}/competitions/{competition_id}/competitors/{competitor['id']}/statistics"
        data = _fetch_json(url)
        stats = {s['name']: s['displayValue'] for s in data['splits']['categories'][0]['stats']}
        name = competitor['athlete']['displayName']
        return stats.get('place', '-'), name, stats.get('totalTime') or NO_TIME, str(competitor['id'])
    except Exception as e:
        logger.debug(f"_competitor_result failed for competitor {competitor.get('id')}: {e}")
        return None


def _position(place) -> str:
    """"P3" for a classified finish, or whatever ESPN says otherwise."""
    try:
        return f"P{int(float(place))}"
    except (TypeError, ValueError):
        return str(place)


def _session_results(event_id: str, competition: dict) -> list[tuple[str, str, str, str]]:
    """(place, driver, time, athlete id) for every driver in a session,
    position-sorted -- [] unless every driver's result came back, so a
    transient API hiccup is retried on a later poll rather than posting a
    classification with drivers missing from it."""
    competitors = competition.get('competitors', [])
    if not competitors:
        return []

    def fetch(competitor: dict) -> tuple[str, str, str, str] | None:
        return _competitor_result(event_id, competition['id'], competitor)

    results = [r for r in _executor.map(fetch, competitors) if r]
    if len(results) < len(competitors):
        return []

    def sort_key(result: tuple[str, str, str, str]) -> int:
        try:
            return int(float(result[0]))
        except (TypeError, ValueError):
            return 999

    return sorted(results, key=sort_key)


def _session_results_table(event_id: str, competition: dict) -> str | None:
    """Returns a formatted, position-sorted results table for a session,
    or None if no results could be fetched (e.g. transient API hiccup --
    the caller should retry on a later poll rather than giving up)."""
    results = _session_results(event_id, competition)
    return _results_table(results) if results else None


def _results_table(results: list[tuple[str, str, str, str]]) -> str:
    teams = driver_teams([athlete_id for _, _, _, athlete_id in results])

    header = f"{'Pos':>3}  {'Driver':<22} Time"
    rows = [header, '-' * len(header)]
    for place, name, total_time, athlete_id in results:
        driver = _driver_cell(name, 22, teams.get(athlete_id))
        rows.append(f"{place:>3}  {driver} {total_time}")

    return "```ansi\n" + "\n".join(rows) + "\n```"


def _grid_recap(event_id: str, competitions: list[dict], race_abbrev: str) -> str | None:
    grid_abbrev = GRID_SESSION_FOR_RACE.get(race_abbrev)
    grid_session = next((c for c in competitions if c['type']['abbreviation'] == grid_abbrev), None)
    if not grid_session:
        return None

    table = _session_results_table(event_id, grid_session)
    if not table:
        return None

    label = SESSION_LABELS.get(grid_abbrev, grid_abbrev)
    return f"**{label} Recap:**\n{table}"


def _race_day_message(event: dict, competitions: list[dict], race_abbrev: str) -> str:
    recap = _grid_recap(event['id'], competitions, race_abbrev)
    recap_text = f"\n\n{recap}" if recap else ""
    return f"# IT'S {RACE_DAY_BANNERS[race_abbrev]} DAY!! 🏎️🏁\n**{event['name']}**{recap_text}"


def _session_label(competition: dict) -> str:
    abbrev = competition['type']['abbreviation']
    return SESSION_LABELS.get(abbrev, abbrev)


@dataclass
class Post:
    """One F1 announcement. A session's results board names that session
    in `board`, so a later Post for the same board with edit=True replaces
    it in place -- as the times fill in, or the order changes -- rather
    than posting the whole table again."""
    text: str
    board: str | None = None
    edit: bool = False


def _board_key(competition: dict) -> str:
    return f"f1:{competition['id']}"


def _results_message(event: dict, competition: dict, results: list[tuple[str, str, str, str]]) -> str:
    text = f"## {_session_label(competition)} Results — {event['name']}\n{_results_table(results)}"
    completed = competition.get('status', {}).get('type', {}).get('completed', False)
    if not completed and any(time == NO_TIME for _, _, time, _ in results):
        text += "\n*⏱️ Times are still coming in — this board updates itself when they land.*"
    return text


# ESPN marks a session "End of Session" as soon as it's over and the
# classification is in, but only flips it to final (completed) once it's
# official -- for a practice session that was well over half an hour
# later, and the lap times only arrived with it. The board goes out at
# whichever comes first and is edited in place every poll after that
# until the session's final: as times land, and if stewards' penalties
# reorder a qualifying session or a race after the flag.
_SESSION_ENDED_STATUSES = {'STATUS_SESSION_COMPLETE', 'STATUS_FINAL'}


def _session_ended(competition: dict) -> bool:
    status = competition.get('status', {}).get('type', {})
    return bool(status.get('completed')) or status.get('name') in _SESSION_ENDED_STATUSES


def _classification_changes(shown: list, latest: list) -> list[str]:
    """One line per driver whose position in the latest classification
    isn't the one the board showed. Positions only: times filling in or
    being restated are just edited into the board, but someone moving --
    a penalty, say -- is worth a message of its own, since an edit to an
    earlier post is easy to miss."""
    before = {athlete_id: (place, name) for place, name, _, athlete_id in shown}
    lines = []
    for place, name, _, athlete_id in latest:
        old = before.pop(athlete_id, None)
        if old is None:
            lines.append(f"• **{name}**: now {_position(place)}")
        elif _position(old[0]) != _position(place):
            lines.append(f"• **{name}**: {_position(old[0])} → {_position(place)}")
    for old_place, name in before.values():
        lines.append(f"• **{name}**: {_position(old_place)} → no longer classified")
    return lines


def check_f1_updates() -> list[Post]:
    """Checks the current F1 race weekend for new milestones -- race week
    start, each session's results once it finishes, and qualifying/race
    day -- and returns the posts to make, including edits to results
    boards already posted. Persists state to disk (keyed by event +
    session id) so nothing gets announced twice across restarts or
    repeated polls."""
    try:
        event = _current_event()
    except Exception as e:
        logger.warning(f"check_f1_updates failed: {e}")
        return []

    if not event:
        return []

    event_id = event['id']
    state = _load_state()
    if state.get('event_id') != event_id:
        state = {'event_id': event_id, 'done': []}

    done = state['done']
    posts = []
    today = datetime.datetime.now(EASTERN).date()

    weekend_start = datetime.datetime.fromisoformat(event['date'].replace('Z', '+00:00')).astimezone(EASTERN).date()
    if 'race_week' not in done and today >= weekend_start:
        is_sprint_weekend = any(
            c['type']['abbreviation'] in ('SS', 'SR') for c in event.get('competitions', [])
        )
        headline = "IT'S RACE WEEK + SPRINT!!!" if is_sprint_weekend else "IT'S RACE WEEK!!"
        location = _event_location(event_id) or "location TBD"
        posts.append(Post(f"# {headline} 🏎️🏁\n**{event['name']}**\nWhere: {location}"))
        done.append('race_week')

    competitions = sorted(event.get('competitions', []), key=lambda c: c['date'])

    # What each results board shows, for sessions not final yet: {session
    # id: [[place, driver, time, athlete id], ...]}. ESPN's classification
    # is checked against it every poll, and the board edited when they
    # differ. (Earlier versions kept the same rows as 'first_results'.)
    boards = state.setdefault('boards', state.pop('first_results', {}))

    for competition in competitions:
        comp_id = competition['id']
        abbrev = competition['type']['abbreviation']
        label = SESSION_LABELS.get(abbrev, abbrev)
        comp_date = datetime.datetime.fromisoformat(competition['date'].replace('Z', '+00:00')).astimezone(EASTERN)
        completed = competition.get('status', {}).get('type', {}).get('completed', False)

        day_marker = f"day:{comp_id}"
        if abbrev in QUALI_DAY_BANNERS and day_marker not in done and today == comp_date.date():
            banner = QUALI_DAY_BANNERS[abbrev]
            posts.append(Post(f"# IT'S {banner} DAY!! ⏱️🏎️\n**{label}** for the {event['name']}"))
            done.append(day_marker)

        if abbrev in RACE_DAY_BANNERS and day_marker not in done and today == comp_date.date():
            posts.append(Post(_race_day_message(event, competitions, abbrev)))
            done.append(day_marker)

        results_marker = f"results:{comp_id}"
        final_marker = f"final:{comp_id}"
        if _session_ended(competition) and results_marker not in done:
            results = _session_results(event_id, competition)
            if results:
                posts.append(Post(_results_message(event, competition, results), board=_board_key(competition)))
                done.append(results_marker)
                if completed:
                    done.append(final_marker)  # what was posted already is the final classification
                else:
                    boards[comp_id] = [list(result) for result in results]
        elif results_marker in done and final_marker not in done:
            shown = boards.get(comp_id)
            if shown is None:
                # Posted before boards were kept, so there's nothing to
                # check ESPN's classification against.
                done.append(final_marker)
                continue
            results = _session_results(event_id, competition)
            if not results:
                continue  # a transient hiccup; checked again next poll
            latest = [list(result) for result in results]
            if latest != shown:
                posts.append(Post(
                    _results_message(event, competition, results), board=_board_key(competition), edit=True,
                ))
                changes = _classification_changes(shown, latest)
                if changes:
                    posts.append(Post(
                        f"📝 **{label} results updated** — {event['name']}\n"
                        f"The order changed since they were first posted (the board above now has it):\n"
                        + "\n".join(changes)
                    ))
                boards[comp_id] = latest
            if completed:
                done.append(final_marker)
                del boards[comp_id]

    _save_state(state)
    return posts


def race_day_catch_up() -> list[Post]:
    """What a server that's only just started getting F1 updates -- the
    bot newly added there, or the feature newly set up -- has missed today,
    if today is a race day: the race-day banner with the grid, or once the
    race has been run and its results posted, those results instead (the
    banner is stale by then).

    Only what has already gone out to everyone else, which is why it reads
    check_f1_updates' own record of what's been announced: anything still
    to come reaches every server through the regular loop anyway, so
    catching it up here too would post it twice. Empty on any other day."""
    try:
        event = _current_event()
        if not event:
            return []
        state = _load_state()
        done = state.get('done', []) if state.get('event_id') == event['id'] else []
        today = datetime.datetime.now(EASTERN).date()
        competitions = sorted(event.get('competitions', []), key=lambda c: c['date'])

        posts = []
        for competition in competitions:
            abbrev = competition['type']['abbreviation']
            race_date = datetime.datetime.fromisoformat(competition['date'].replace('Z', '+00:00')).astimezone(EASTERN).date()
            if abbrev not in RACE_DAY_BANNERS or race_date != today:
                continue
            if f"results:{competition['id']}" in done:
                results = _session_results(event['id'], competition)
                if results:
                    # A board like everyone else's, so it's edited along
                    # with theirs if the classification still changes.
                    posts.append(Post(
                        _results_message(event, competition, results), board=_board_key(competition),
                    ))
            elif f"day:{competition['id']}" in done:
                posts.append(Post(_race_day_message(event, competitions, abbrev)))
        return posts
    except Exception as e:
        logger.warning(f"race_day_catch_up failed: {e}")
        return []


def f1_status() -> str:
    """Returns whether it's currently F1 race week -- the calendar week
    (Monday-Sunday) containing the race -- and if so, which session(s)
    are happening today."""
    try:
        event = _current_event()
    except Exception as e:
        logger.warning(f"f1_status failed: {e}")
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


def _standings_team(entry: dict, teams: dict[str, str | None]) -> str | None:
    """Which team's colour a standings row takes. A constructor row is its
    own team; a driver row borrows the team they race for, so teammates
    sit in the table as a matching pair."""
    if 'team' in entry:
        return entry['team'].get('displayName')
    athlete_id = str((entry.get('athlete') or {}).get('id', ''))
    return teams.get(athlete_id)


def _f1_standings_table(title: str, entries: list[dict], name_fn: Callable[[dict], str]) -> str:
    def stat_map(entry: dict) -> dict:
        return {stat['name']: stat.get('displayValue') for stat in entry['stats']}

    def sort_key(entry: dict) -> int:
        try:
            return int(float(stat_map(entry).get('rank') or 0))
        except (TypeError, ValueError):
            return 999

    entries = sorted(entries, key=sort_key)

    # Only the driver table needs the lookup; a constructor entry already
    # names its own team, so this comes back empty and costs nothing.
    teams = driver_teams([
        str(entry['athlete']['id']) for entry in entries
        if 'team' not in entry and (entry.get('athlete') or {}).get('id')
    ])

    header = f"{'#':>2} {'Name':<24} {'Pts':>4}"
    rows = [header, '-' * len(header)]
    for entry in entries:
        stats = stat_map(entry)
        rank = stats.get('rank', '-')
        points = stats.get('championshipPts') or stats.get('points') or '-'
        name = _driver_cell(name_fn(entry), 24, _standings_team(entry, teams))
        rows.append(f"{rank:>2} {name} {points:>4}")

    table = "\n".join(rows)
    return f"## {title}\n```ansi\n{table}\n```"


def f1_standings() -> list[str]:
    """Returns the current F1 drivers' and constructors' championship
    standings as a list of Discord-ready message chunks."""
    try:
        data = _fetch_json(F1_SITE_STANDINGS)
        children = {child['name']: child for child in data.get('children', [])}
    except Exception as e:
        logger.warning(f"f1_standings failed: {e}")
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
