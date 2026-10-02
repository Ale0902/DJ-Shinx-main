"""Japanese horse racing (JRA) updates -- the graded stakes on the
upcoming card, the horses running in each, every horse's chance of
winning, and the results once they're in.

Everything comes from netkeiba, the de facto public source for JRA
racing: race.netkeiba.com for the race calendar and the odds, and its
English sister site en.netkeiba.com for fields and results with
romanized horse/jockey names. None of it is an official API -- it's the
same HTML/JSON the site's own pages load -- so every fetch here is
guarded to degrade to "nothing to announce" instead of raising into a
background loop if their markup ever changes.
"""
import concurrent.futures
import datetime
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

RACE_LIST_URL = "https://race.netkeiba.com/top/race_list_sub.html"
ODDS_URL = "https://race.netkeiba.com/api/api_get_jra_odds.html"
EN_FIELD_URL = "https://en.netkeiba.com/race/shutuba.html"
EN_RESULT_URL = "https://en.netkeiba.com/race/race_result.html"

USER_AGENT = "Mozilla/5.0 (compatible; DJ-Shinx-Bot/1.0; +https://github.com/Ale0902/DJ-Shinx-main)"

BASE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE, 'jra_state.json')

JST = ZoneInfo("Asia/Tokyo")

# netkeiba marks a race's grade with an Icon_GradeType<N> class. Only
# graded stakes are announced -- a JRA weekend runs ~70 races, and these
# are the handful anyone outside Japan follows. (5 is open class, 15
# listed, 16-18 the 3/2/1-win classes; none of those make the cut.)
GRADES = {'1': 'G1', '2': 'G2', '3': 'G3', '10': 'J.G1', '11': 'J.G2', '12': 'J.G3'}

# Digits 5-6 of a race id are the racecourse, e.g. 2026|05|04|02|11 is
# Tokyo (05), meeting 4, day 2, race 11.
VENUES = {
    '01': 'Sapporo', '02': 'Hakodate', '03': 'Fukushima', '04': 'Niigata', '05': 'Tokyo',
    '06': 'Nakayama', '07': 'Chukyo', '08': 'Kyoto', '09': 'Hanshin', '10': 'Kokura',
}

SURFACES = {'芝': 'Turf', 'ダ': 'Dirt', '障': 'Jump'}

# The race card only goes up a few days out (entries close Sunday, the
# draw is Thursday/Friday), so a week ahead covers everything that can
# exist. Yesterday is included so a race that finished just before
# midnight Japan time still gets its result posted.
SCAN_DAYS = range(-1, 7)

# How long before post time the "race is coming up" alert goes out. The
# loop polls every 5 minutes, so this lands 30-35 minutes out.
POST_ALERT_LEAD = datetime.timedelta(minutes=35)
# Results are checked from shortly after the off until they appear. Past
# the give-up point (the bot was down, say) a result is stale news and is
# skipped rather than posted hours late.
RESULT_DELAY = datetime.timedelta(minutes=5)
RESULT_GIVE_UP = datetime.timedelta(hours=12)
# Announced markers are kept long enough to outlive the scan window,
# then pruned so the state file doesn't grow forever.
STATE_RETENTION = datetime.timedelta(days=21)

# Odds move every minute on race day; the card and fields barely move.
_ODDS_TTL_SECONDS = 60
_FIELD_TTL_SECONDS = 10 * 60
_CARD_TTL_SECONDS = 30 * 60

_executor = concurrent.futures.ThreadPoolExecutor(max_workers=8)
_response_cache: dict[tuple, tuple[float, str]] = {}


@dataclass
class Race:
    race_id: str
    grade: str
    name: str                       # Japanese name off the card; see display_name()
    venue: str
    number: int
    course: str                     # e.g. "Turf 1800m"
    runners: int | None
    post_time: datetime.datetime    # aware, JST


@dataclass
class Runner:
    number: int | None              # horse number; None until the draw is made
    name: str
    jockey: str
    scratched: bool = False


def _fetch_text(url: str, params: dict, ttl: float) -> str:
    key = (url, tuple(sorted(params.items())))
    cached = _response_cache.get(key)
    now = time.monotonic()
    if cached and now - cached[0] < ttl:
        return cached[1]

    response = requests.get(url, params=params, headers={'User-Agent': USER_AGENT}, timeout=10)
    response.raise_for_status()
    # en.netkeiba sends "charset=" with nothing after it, which requests
    # reads as Latin-1 and turns every non-ASCII name into mojibake. Both
    # sites are UTF-8 throughout.
    response.encoding = 'utf-8'
    text = response.text
    _response_cache[key] = (now, text)
    return text


def _load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r') as f:
            return json.load(f)
    return {}


def _save_state(state: dict) -> None:
    # Temp file + rename, same as f1.py, so a crash mid-write can't leave
    # a truncated state file that re-announces the whole weekend.
    tmp_path = STATE_FILE + '.tmp'
    with open(tmp_path, 'w') as f:
        json.dump(state, f)
    os.replace(tmp_path, STATE_FILE)


def _race_card(date: datetime.date) -> list[Race]:
    """The graded races on one day's JRA card (a Japan-time date), or []
    if there's no racing that day or the card couldn't be fetched."""
    try:
        html = _fetch_text(RACE_LIST_URL, {'kaisai_date': date.strftime('%Y%m%d')}, _CARD_TTL_SECONDS)
    except Exception as e:
        logger.warning(f"jra: couldn't fetch the race card for {date}: {e}")
        return []

    races = []
    for item in BeautifulSoup(html, 'html.parser').select('li.RaceList_DataItem'):
        try:
            grade = None
            for icon in item.select('.Icon_GradeType'):
                for cls in icon.get('class', []):
                    match = re.fullmatch(r'Icon_GradeType(\d+)', cls)
                    if match and match.group(1) in GRADES:
                        grade = GRADES[match.group(1)]
            if not grade:
                continue

            race_id = re.search(r'race_id=(\d{12})', item.select_one('a[href*="race_id="]')['href']).group(1)
            hour, minute = item.select_one('.RaceList_Itemtime').get_text(strip=True).split(':')
            post_time = datetime.datetime(date.year, date.month, date.day, int(hour), int(minute), tzinfo=JST)

            course_text = item.select_one('.RaceList_ItemLong').get_text(strip=True)
            course_match = re.match(r'(\D)(\d+)m', course_text)
            course = (
                f"{SURFACES.get(course_match.group(1), course_match.group(1))} {course_match.group(2)}m"
                if course_match else course_text
            )

            runners_el = item.select_one('.RaceList_Itemnumber')
            runners_match = re.search(r'\d+', runners_el.get_text()) if runners_el else None

            races.append(Race(
                race_id=race_id,
                grade=grade,
                name=item.select_one('.ItemTitle').get_text(strip=True),
                venue=VENUES.get(race_id[4:6], 'JRA'),
                number=int(race_id[-2:]),
                course=course,
                runners=int(runners_match.group(0)) if runners_match else None,
                post_time=post_time,
            ))
        except Exception as e:
            # One odd entry (a cancelled race, a markup tweak) shouldn't
            # cost the rest of the day's card.
            logger.debug(f"jra: skipped an unparseable race card entry on {date}: {e}")
    return races


def _graded_races(today: datetime.date) -> list[Race]:
    """Every graded race in the scan window, soonest first."""
    dates = [today + datetime.timedelta(days=offset) for offset in SCAN_DAYS]
    cards = _executor.map(_race_card, dates)
    return sorted((race for card in cards for race in card), key=lambda r: r.post_time)


def _race_field(race_id: str) -> tuple[str | None, list[Runner]]:
    """(English race name, runners) from en.netkeiba's field page. Either
    can come back empty if the page couldn't be fetched or parsed."""
    try:
        html = _fetch_text(EN_FIELD_URL, {'race_id': race_id}, _FIELD_TTL_SECONDS)
    except Exception as e:
        logger.warning(f"jra: couldn't fetch the field for race {race_id}: {e}")
        return None, []

    soup = BeautifulSoup(html, 'html.parser')
    name_el = soup.select_one('.RaceList_Item02 .Race_Name')
    name = name_el.get_text(' ', strip=True) if name_el else None

    runners = []
    for row in soup.select('tr.HorseList'):
        try:
            cells = row.find_all('td', recursive=False)
            # Cell 0 is the bracket (waku), cell 1 the horse number.
            number_text = cells[1].get_text(strip=True)
            horse = row.select_one('dt.Horse a') or row.select_one('dt.Horse')
            jockey_cell = next((c for c in cells if c.get('class') == ['Txt_L']), None)
            runners.append(Runner(
                number=int(number_text) if number_text.isdigit() else None,
                name=horse.get_text(' ', strip=True),
                jockey=jockey_cell.get_text(' ', strip=True) if jockey_cell else '',
                scratched='Cancel' in row.get('class', []),
            ))
        except Exception as e:
            logger.debug(f"jra: skipped an unparseable runner in race {race_id}: {e}")
    return name, runners


def _win_odds(race_id: str) -> tuple[dict[int, float], str | None, datetime.datetime | None]:
    """({horse number: win odds}, status, as-of time) from netkeiba's odds
    feed. Status is 'yoso' while betting is closed -- the odds are then
    netkeiba's own forecast -- 'result' once they're final, and anything
    else means a live JRA pool. Scratched horses come back with negative
    odds and are left out."""
    try:
        text = _fetch_text(ODDS_URL, {'race_id': race_id, 'type': '1', 'action': 'update'}, _ODDS_TTL_SECONDS)
        payload = json.loads(text)
    except Exception as e:
        logger.warning(f"jra: couldn't fetch odds for race {race_id}: {e}")
        return {}, None, None

    data = payload.get('data') or {}
    win_pool = (data.get('odds') or {}).get('1') or {}
    odds = {}
    for number, values in win_pool.items():
        try:
            value = float(values[0])
        except (TypeError, ValueError, IndexError):
            continue
        if value > 0:
            odds[int(number)] = value

    as_of = None
    if data.get('official_datetime'):
        try:
            as_of = datetime.datetime.strptime(data['official_datetime'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=JST)
        except ValueError:
            pass
    return odds, payload.get('status'), as_of


def win_probabilities(odds: dict[int, float]) -> dict[int, float]:
    """Each horse's chance of winning, read off its win odds.

    1/odds alone overstates everyone: the pool keeps its takeout (20% in
    the JRA win pool), so those figures sum to ~1.25 rather than 1.
    Instead of scaling every horse down by the same factor, this solves
    for the exponent k where sum((1/odds)^k) == 1 -- the "power method"
    -- which trims longshots harder than favourites. That corrects the
    well-documented favourite-longshot bias, where bettors overplay
    outsiders and the raw odds make them look likelier than they are."""
    implied = {number: 1 / value for number, value in odds.items() if value > 0}
    if not implied:
        return {}

    # A 1.0 favourite (p == 1) has no exponent that balances the book, so
    # that rare case just falls through to plain normalisation.
    if len(implied) > 1 and max(implied.values()) < 1:
        low, high = 0.01, 50.0
        for _ in range(60):
            k = (low + high) / 2
            if sum(p ** k for p in implied.values()) > 1:
                low = k
            else:
                high = k
        implied = {number: p ** ((low + high) / 2) for number, p in implied.items()}

    total = sum(implied.values())
    return {number: p / total for number, p in implied.items()}


def _percent(probability: float | None) -> str:
    if probability is None:
        return '-'
    if probability < 0.005:
        return '<1%'
    return f"{probability * 100:.0f}%"


def _timestamp(moment: datetime.datetime, style: str) -> str:
    """A Discord timestamp, which every viewer sees in their own timezone --
    the races run overnight for the Americas, so a fixed zone would be
    wrong for most of the server one way or the other."""
    return f"<t:{int(moment.timestamp())}:{style}>"


def display_name(race: Race, english_name: str | None) -> str:
    return f"{race.grade} · {english_name or race.name}"


def _race_heading(race: Race, english_name: str | None) -> str:
    runners = f" · {race.runners} runners" if race.runners else ""
    return (
        f"## {display_name(race, english_name)}\n"
        f"{race.venue} R{race.number} · {race.course}{runners}\n"
        f"Post time: {_timestamp(race.post_time, 'F')} ({_timestamp(race.post_time, 'R')})"
    )


def _odds_note(status: str | None, as_of: datetime.datetime | None) -> str:
    if status == 'yoso':
        source = "Projected odds (netkeiba's forecast) — JRA betting hasn't opened yet."
    elif status == 'result':
        source = "Final JRA win odds."
    elif as_of:
        source = f"Live JRA win odds as of {_timestamp(as_of, 't')}."
    else:
        source = "Live JRA win odds."
    return f"*{source} Win % is the market's chance for each horse, with the JRA's cut stripped out.*"


def _field_table(runners: list[Runner], odds: dict[int, float], probabilities: dict[int, float]) -> str:
    """The field as a fixed-width table, likeliest winner first. Falls
    back to draw order with no odds columns when there are no odds to
    show (feed down, or the draw hasn't been made yet)."""
    if not probabilities:
        header = f"{'#':>2} {'Horse':<20} Jockey"
        rows = [header, '-' * len(header)]
        for runner in sorted(runners, key=lambda r: r.number or 99):
            number = str(runner.number) if runner.number else '-'
            rows.append(f"{number:>2} {runner.name:<20.20} {runner.jockey}")
        return "```\n" + "\n".join(rows) + "\n```"

    def sort_key(runner: Runner) -> tuple:
        probability = probabilities.get(runner.number) if not runner.scratched else None
        return (probability is None, -(probability or 0), runner.number or 99)

    header = f"{'#':>2} {'Horse':<18} {'Jockey':<11} {'Odds':>5} {'Win':>4}"
    rows = [header, '-' * len(header)]
    for runner in sorted(runners, key=sort_key):
        number = str(runner.number) if runner.number else '-'
        probability = None if runner.scratched else probabilities.get(runner.number)
        if probability is None:
            odds_text, win_text = 'SCR', '-'
        else:
            odds_text, win_text = f"{odds[runner.number]:.1f}", _percent(probability)
        rows.append(f"{number:>2} {runner.name:<18.18} {runner.jockey:<11.11} {odds_text:>5} {win_text:>4}")
    return "```\n" + "\n".join(rows) + "\n```"


def _race_block(race: Race) -> str | None:
    """A race's heading, field and win chances as one message body, or
    None if the field couldn't be fetched (so a background caller knows
    to try again next poll rather than announce an empty race)."""
    english_name, runners = _race_field(race.race_id)
    if not runners:
        return None

    odds, status, as_of = _win_odds(race.race_id)
    probabilities = win_probabilities(odds)
    table = _field_table(runners, odds, probabilities)
    note = f"\n{_odds_note(status, as_of)}" if probabilities else ""
    return f"{_race_heading(race, english_name)}\n{table}{note}"


def _favourite(race: Race) -> tuple[str | None, str | None, float | None]:
    """(English race name, favourite's name, favourite's win chance) --
    the one-line summary /jra shows per race."""
    english_name, runners = _race_field(race.race_id)
    probabilities = win_probabilities(_win_odds(race.race_id)[0])
    if not probabilities:
        return english_name, None, None

    names = {runner.number: runner.name for runner in runners if runner.number and not runner.scratched}
    contenders = {number: p for number, p in probabilities.items() if number in names}
    if not contenders:
        return english_name, None, None
    number = max(contenders, key=contenders.get)
    return english_name, names[number], contenders[number]


def _results(race_id: str) -> list[dict]:
    """Every horse that ran -- place, horse, jockey, final odds, time --
    finishers in order, then non-finishers (DNF, disqualified) with a
    place of None. [] if the race hasn't been run (en.netkeiba has no
    results table until it has) or the page couldn't be read. Scratched
    horses carry no odds and are left out; they never ran."""
    try:
        html = _fetch_text(EN_RESULT_URL, {'race_id': race_id}, _ODDS_TTL_SECONDS)
    except Exception as e:
        logger.warning(f"jra: couldn't fetch the result for race {race_id}: {e}")
        return []

    table = BeautifulSoup(html, 'html.parser').select_one('#All_Result_Table')
    if not table:
        return []

    finishers = []
    for row in table.select('tr'):
        place_el = row.select_one('.Result_Num')
        horse_el = row.select_one('.Horse_Name')
        if not place_el or not horse_el or row.find('th'):
            continue
        odds_el = row.select_one('td.Odds')
        try:
            odds = float(odds_el.get_text(strip=True)) if odds_el else None
        except ValueError:
            odds = None
        if odds is None:
            continue

        place_text = place_el.get_text(strip=True)
        jockey_el = row.select_one('td.Jockey')
        time_el = row.select_one('td.Time')
        finishers.append({
            'place': int(place_text) if place_text.isdigit() else None,
            'horse': horse_el.get_text(' ', strip=True),
            'jockey': jockey_el.get_text(' ', strip=True) if jockey_el else '',
            'odds': odds,
            'time': time_el.get_text(strip=True) if time_el else '',
        })
    return sorted(finishers, key=lambda f: (f['place'] is None, f['place'] or 0))


def _result_message(race: Race) -> str | None:
    finishers = _results(race.race_id)
    if not finishers or finishers[0]['place'] != 1:
        return None

    # The winner's pre-race chance, from the final odds of everyone who
    # ran (a horse that didn't finish still took bets).
    probabilities = win_probabilities({i: f['odds'] for i, f in enumerate(finishers)})
    english_name, _ = _race_field(race.race_id)

    medals = {1: '🥇', 2: '🥈', 3: '🥉'}
    lines = [
        f"# 🏆 {display_name(race, english_name)} — RESULT",
        f"{race.venue} R{race.number} · {race.course}",
        "",
    ]
    for finisher in finishers[:3]:
        if finisher['place'] is None:
            break
        jockey = f" ({finisher['jockey']})" if finisher['jockey'] else ""
        medal = medals.get(finisher['place'], f"{finisher['place']}.")
        lines.append(f"{medal} **{finisher['horse']}**{jockey} — {finisher['odds']:.1f}")

    winner_chance = probabilities.get(0)
    if winner_chance is not None:
        favourite_won = max(probabilities, key=probabilities.get) == 0
        if favourite_won:
            lines.append(f"\nThe favourite delivered — {finishers[0]['horse']} went off with a {_percent(winner_chance)} chance.")
        elif winner_chance < 0.10:
            lines.append(f"\n🚨 **UPSET!!** {finishers[0]['horse']} only had a {_percent(winner_chance)} chance of winning!")
        else:
            lines.append(f"\n{finishers[0]['horse']} went off with a {_percent(winner_chance)} chance of winning.")
    if finishers[0]['time']:
        lines.append(f"Winning time: {finishers[0]['time']}")
    return "\n".join(lines)


def check_jra_updates() -> list[str]:
    """Checks the JRA card for anything new to announce and returns the
    messages to post:
      - the day before (Japan time) a day with graded stakes, a preview
        of each race with its field and every horse's win chance;
      - ~30 minutes before each graded race, the field again on the
        live odds;
      - each graded race's result once it's in.
    State is persisted to disk so nothing is announced twice across
    restarts or overlapping polls."""
    now = datetime.datetime.now(JST)
    try:
        races = _graded_races(now.date())
    except Exception as e:
        logger.warning(f"check_jra_updates failed: {e}")
        return []

    state = _load_state()
    done: dict[str, str] = state.setdefault('done', {})
    stamp = now.isoformat()
    messages = []

    by_date: dict[datetime.date, list[Race]] = {}
    for race in races:
        by_date.setdefault(race.post_time.date(), []).append(race)

    for date, day_races in by_date.items():
        marker = f"preview:{date.isoformat()}"
        if marker in done or not 0 <= (date - now.date()).days <= 1:
            continue
        # A race whose 30-minute alert is already due gets that instead
        # of a preview too (only happens if the bot came up on race day).
        upcoming = [r for r in day_races if r.post_time - now > POST_ALERT_LEAD]
        if not upcoming:
            done[marker] = stamp
            continue
        blocks = [_race_block(r) for r in upcoming]
        if any(block is None for block in blocks):
            continue  # a field didn't load; retry the whole preview next poll

        count = len(upcoming)
        stakes = "graded stakes race" if count == 1 else "graded stakes races"
        intro = (
            f"# JRA RACE DAY INCOMING!! 🏇🇯🇵\n"
            f"**{date.strftime('%A, %B')} {date.day}** in Japan — {count} {stakes} on the card\n\n"
        )
        messages.append(intro + blocks[0])
        messages.extend(blocks[1:])
        done[marker] = stamp

    for race in races:
        until_post = race.post_time - now

        post_marker = f"post:{race.race_id}"
        if post_marker not in done:
            if until_post <= datetime.timedelta(0):
                # Already off (the bot was down) -- too late to be useful.
                done[post_marker] = stamp
            elif until_post <= POST_ALERT_LEAD:
                block = _race_block(race)
                if block:
                    minutes = max(1, int(until_post.total_seconds() // 60))
                    messages.append(f"# 🏇 {minutes} MINUTES TO POST!!\n{block}")
                    done[post_marker] = stamp

        result_marker = f"result:{race.race_id}"
        if result_marker not in done and -until_post >= RESULT_DELAY:
            if -until_post > RESULT_GIVE_UP:
                done[result_marker] = stamp
            else:
                message = _result_message(race)
                if message:
                    messages.append(message)
                    done[result_marker] = stamp

    cutoff = now - STATE_RETENTION
    state['done'] = {
        marker: when for marker, when in done.items()
        if datetime.datetime.fromisoformat(when) >= cutoff
    }
    _save_state(state)
    return messages


def _upcoming(now: datetime.datetime) -> list[Race]:
    return [race for race in _graded_races(now.date()) if race.post_time > now]


_NO_RACES = (
    "No JRA graded stakes on the card right now. 🏇\n"
    "The weekend's fields go up by Thursday (Japan time) — check back then!"
)


def jra_schedule() -> str:
    """The upcoming graded stakes, one summary per race with its
    favourite and that horse's chance of winning."""
    try:
        races = _upcoming(datetime.datetime.now(JST))
    except Exception as e:
        logger.warning(f"jra_schedule failed: {e}")
        return "Couldn't reach the JRA race card right now. Try again later!"

    if not races:
        return _NO_RACES

    lines = ["# 🏇 Upcoming JRA Graded Stakes 🇯🇵"]
    for race, (english_name, favourite, chance) in zip(races, _executor.map(_favourite, races)):
        runners = f" · {race.runners} runners" if race.runners else ""
        lines.append(
            f"\n**{display_name(race, english_name)}** — {race.venue} R{race.number} · {race.course}{runners}\n"
            f"{_timestamp(race.post_time, 'F')} ({_timestamp(race.post_time, 'R')})"
        )
        if favourite:
            lines.append(f"Favourite: **{favourite}** ({_percent(chance)} to win)")
    lines.append("\n*Use /jraodds to see a race's full field and each horse's chance of winning.*")
    return "\n".join(lines)


def race_choices(query: str = '') -> list[tuple[str, str]]:
    """(label, race_id) for each upcoming graded race whose name matches
    the query -- feeds /jraodds' autocomplete."""
    try:
        races = _upcoming(datetime.datetime.now(JST))
    except Exception as e:
        logger.debug(f"race_choices failed: {e}")
        return []

    choices = []
    for race, (english_name, _) in zip(races, _executor.map(lambda r: _race_field(r.race_id), races)):
        day = race.post_time.strftime('%a')
        label = f"{display_name(race, english_name)} — {day} {race.venue} R{race.number}"
        if query.lower() in label.lower():
            choices.append((label[:100], race.race_id))
    return choices[:25]


def jra_race(query: str | None = None) -> str:
    """One upcoming graded race's field with every horse's chance of
    winning -- the soonest one, or the one whose name or race id matches
    the query."""
    try:
        races = _upcoming(datetime.datetime.now(JST))
    except Exception as e:
        logger.warning(f"jra_race failed: {e}")
        return "Couldn't reach the JRA race card right now. Try again later!"

    if not races:
        return _NO_RACES

    race = races[0]
    if query:
        query = query.strip().lower()
        race = next((r for r in races if r.race_id == query), None)
        if race is None:
            names = dict(zip(
                (r.race_id for r in races),
                _executor.map(lambda r: (_race_field(r.race_id)[0] or '').lower(), races),
            ))
            race = next((r for r in races if query in names[r.race_id] or query in r.name.lower()), None)
        if race is None:
            return f"No upcoming JRA graded race matches **{query}**. Try /jra for the full list."

    block = _race_block(race)
    return block or "Couldn't load that race's field right now. Try again later!"
