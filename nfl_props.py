"""NFL prop bets for the play-money economy: /propbet puts coins on the
over or under of a fixed menu for each game this week -- no picking any
player or stat you like. Each team offers exactly four props:

  QB   the starting quarterback's passing yards
  RB1  the lead running back's rushing yards
  WR1  the top wide receiver's receiving yards
  DEF  the defense's takeaways (interceptions + fumbles recovered)

The player lines are DraftKings' own, from ESPN's odds feed, and they
also decide who's on the menu: a team's QB is whoever has a passing
yards line, and its RB1/WR1 is the RB/WR with the biggest yardage line.
Books pull a player's props once he's ruled out, so an injured starter
drops off the menu by himself and his backup takes the spot.

ESPN only carries the lines, not the prices on each side, so a player
prop is treated as the coin flip a book's line is set to be and pays
fair odds plus sportsbook.py's PLAYER_EDGE_PCT -- 2.04x either way.
No book posts a team takeaways line at all, so that one is priced here:
a Poisson model of the defense's takeaway rate and the opponent's
giveaway rate (see _takeaway_market), which pays each side by its own
chance, so the over on 1.5 pays more than the under.

Bets live in economy.db and settle in the same loop as sportsbook.py's.
"""
import concurrent.futures
import datetime
import logging
import math
import re
import sqlite3
import time
from dataclasses import dataclass

import requests

import economy
import sports
import sportsbook
from sportsbook import PLAYER_EDGE_PCT, BetError, Settlement

logger = logging.getLogger(__name__)

CORE_BASE = "https://sports.core.api.espn.com/v2/sports/football/leagues/nfl"
SUMMARY_URL = f"{sports.ESPN_SITE_BASE}/football/nfl/summary"
# DraftKings, the book ESPN shows NFL odds from. A game names its own
# provider on the scoreboard; this is only the fallback.
DEFAULT_PROVIDER = '100'

# ESPN prop type id -> (box score category, box score stat key, label).
PASSING_YARDS = '8'
RUSHING_YARDS = '12'
RECEIVING_YARDS = '13'
PLAYER_STATS = {
    PASSING_YARDS: ('passing', 'passingYards', "passing yards"),
    RUSHING_YARDS: ('rushing', 'rushingYards', "rushing yards"),
    RECEIVING_YARDS: ('receiving', 'receivingYards', "receiving yards"),
}
# The menu's player slots: (role, prop type, position that may fill it).
ROLES = [('QB', PASSING_YARDS, 'QB'), ('RB1', RUSHING_YARDS, 'RB'), ('WR1', RECEIVING_YARDS, 'WR')]

# ESPN serves a game's ~1,200 props 25 to a page, sorted by type id. The
# three needed come first (types 8-13, the first two pages), so paging
# stops once it's past them -- capped in case that order ever changes.
PROP_PAGE_SIZE = 25
MAX_PROP_PAGES = 6

# Takeaways model. NFL teams have averaged about 1.3 takeaways a game in
# recent seasons; a team's own rate is blended in as if the league
# average were PRIOR_GAMES games of evidence, so three games into a
# season one fluky 4-takeaway day can't swing the line.
LEAGUE_TAKEAWAYS_PER_GAME = 1.3
PRIOR_GAMES = 4
TAKEAWAY_LINES = (0.5, 1.5, 2.5)

# Same rule as sportsbook.py: a bet on a game that still hasn't finished
# this long after kickoff gets its stake back.
STALE_AFTER = datetime.timedelta(days=7)

# Building a game's menu cold takes ESPN 3-4 seconds -- more than the 3
# Discord gives an autocomplete to answer -- so warm_menus() rebuilds
# every game's in the background every 5 minutes. A bet locks in a menu
# at most _MENU_TTL_SECONDS old (normally the one the bettor was just
# shown); autocomplete will take one up to _MENU_STALE_SECONDS old rather
# than leave the dropdown empty while the warm loop catches up.
_MENU_TTL_SECONDS = 6 * 60
_MENU_STALE_SECONDS = 30 * 60
_SCOREBOARD_TTL_SECONDS = 60
_ATHLETE_TTL_SECONDS = 12 * 3600  # name/position/team barely change
_TEAM_STATS_TTL_SECONDS = 6 * 3600

_cache: dict[tuple, tuple[float, object]] = {}
# Its own pool, since each menu build fans out on sports._executor --
# nesting both levels in one pool could starve it.
_menu_executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)


def _cached(key: tuple, ttl: float, fetch):
    hit = _cache.get(key)
    now = time.monotonic()
    if hit and now - hit[0] < ttl:
        return hit[1]
    value = fetch()
    _cache[key] = (now, value)
    return value


def _get_json(url: str, params: dict | None = None) -> dict:
    # ESPN's $refs come back as http://; the API serves the same over https.
    response = requests.get(url.replace('http://', 'https://', 1), params=params, timeout=10)
    response.raise_for_status()
    return response.json()


#=========================--GAMES--==========================================#

@dataclass
class Team:
    id: str
    abbr: str     # e.g. "IND"
    name: str     # e.g. "Colts"


@dataclass
class Game:
    event_id: str
    season: int
    starts_at: datetime.datetime
    state: str  # ESPN's 'pre', 'in' or 'post'
    home: Team
    away: Team
    provider: str

    @property
    def matchup(self) -> str:
        return f"{self.away.name} @ {self.home.name}"

    @property
    def open_for_bets(self) -> bool:
        return self.state == 'pre' and self.starts_at > datetime.datetime.now(datetime.timezone.utc)

    def describe(self) -> str:
        """e.g. "🏈 Colts @ Commanders · Sun 9:30 AM ET" """
        return f"🏈 {self.matchup} · {sportsbook._kickoff(self.starts_at)}"

    def matches(self, needle: str) -> bool:
        names = [self.matchup, self.home.name, self.away.name, self.home.abbr, self.away.abbr]
        return any(needle in name.casefold() for name in names)


def _parse_game(event: dict) -> Game | None:
    try:
        competition = event['competitions'][0]
        teams = {}
        for competitor in competition['competitors']:
            team = competitor['team']
            teams[competitor['homeAway']] = Team(
                id=str(team['id']),
                abbr=team.get('abbreviation', ''),
                name=team.get('shortDisplayName') or team['displayName'],
            )
        odds = (competition.get('odds') or [None])[0] or {}
        return Game(
            event_id=str(event['id']),
            season=int(event.get('season', {}).get('year') or datetime.date.today().year),
            starts_at=sportsbook._parse_time(event['date']),
            state=competition['status']['type']['state'],
            home=teams['home'],
            away=teams['away'],
            provider=str((odds.get('provider') or {}).get('id') or DEFAULT_PROVIDER),
        )
    except (KeyError, IndexError, TypeError, ValueError) as e:
        logger.debug(f"nfl_props: skipping unreadable event {event.get('id')}: {e}")
        return None


def _week_games() -> list[Game]:
    """This NFL week's games, soonest first -- ESPN's scoreboard with no
    date is the current week."""
    def fetch():
        events = sports._fetch_scoreboard(sports.NFL_SCOREBOARD_URL).get('events', [])
        games = [g for g in (_parse_game(e) for e in events) if g]
        return sorted(games, key=lambda g: g.starts_at)
    return _cached(('week',), _SCOREBOARD_TTL_SECONDS, fetch)


def board() -> list[Game]:
    """This week's games that haven't kicked off yet."""
    try:
        return [game for game in _week_games() if game.open_for_bets]
    except Exception as e:
        logger.warning(f"nfl_props: couldn't fetch this week's games: {e}")
        return []


def game_suggestions(current: str) -> list[tuple[str, str]]:
    needle = current.strip().casefold()
    return [(g.describe()[:100], g.event_id) for g in board() if g.matches(needle)][:25]


def find_game(text: str) -> Game | None:
    """The open game an autocomplete value (event id) or typed team name
    refers to, or None if there isn't exactly one."""
    text = text.strip()
    games = board()
    matches = [g for g in games if g.event_id == text] or [g for g in games if text and g.matches(text.casefold())]
    return matches[0] if len(matches) == 1 else None


def resolve_game(text: str) -> Game:
    game = find_game(text)
    if game is None:
        raise BetError(
            f"Couldn't find an NFL game open for prop bets matching **{text}**. "
            f"Start typing a team in /propbet to pick from this week's games."
        )
    return game


#=========================--THE MENU--=======================================#

@dataclass
class Prop:
    key: str                  # e.g. "wr1:IND" -- what /propbet's autocomplete hands back
    role: str                 # 'QB', 'RB1', 'WR1' or 'DEF'
    team: Team
    subject: str              # "Josh Downs", or "Colts defense"
    athlete_id: str | None    # None for a defense
    stat: str                 # box score key, or 'takeaways'
    stat_label: str
    line: float
    payouts: dict[str, int]   # 'over' / 'under' -> payout_pct

    @property
    def label(self) -> str:
        """e.g. "Josh Downs (IND WR1) — receiving yards o/u 62.5" """
        who = self.subject if self.role == 'DEF' else f"{self.subject} ({self.team.abbr} {self.role})"
        return f"{who} — {self.stat_label} o/u {self.line:g}"


def _player_lines(game: Game) -> dict[tuple[str, str], tuple[float, str]]:
    """{(prop type, athlete id): (line, athlete $ref)} for the three
    yardage props. Empty when the book hasn't posted props for the game."""
    url = f"{CORE_BASE}/events/{game.event_id}/competitions/{game.event_id}/odds/{game.provider}/propBets"
    last_type = max(int(t) for t in PLAYER_STATS)
    lines = {}
    for page in range(1, MAX_PROP_PAGES + 1):
        try:
            data = _get_json(url, {'limit': PROP_PAGE_SIZE, 'page': page})
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                return {}  # no props posted for this game (yet)
            raise
        items = data.get('items', [])
        for item in items:
            type_id = str(item.get('type', {}).get('id'))
            target = ((item.get('current') or {}).get('target') or {}).get('value')
            ref = (item.get('athlete') or {}).get('$ref', '')
            athlete = re.search(r'athletes/(\d+)', ref)
            if type_id in PLAYER_STATS and target is not None and athlete:
                lines[(type_id, athlete.group(1))] = (float(target), ref)
        past_needed = any(int(item.get('type', {}).get('id', 0)) > last_type for item in items)
        if not items or past_needed or page >= data.get('pageCount', 0):
            break
    return lines


def _athlete(athlete_id: str, ref: str) -> tuple[str, str, str] | None:
    """(name, position, team id), or None if it can't be read."""
    def fetch():
        try:
            data = _get_json(ref)
            team = re.search(r'teams/(\d+)', (data.get('team') or {}).get('$ref', ''))
            return data['displayName'], data.get('position', {}).get('abbreviation', ''), team.group(1) if team else ''
        except Exception as e:
            logger.debug(f"nfl_props: couldn't read athlete {athlete_id}: {e}")
            return None
    return _cached(('athlete', athlete_id), _ATHLETE_TTL_SECONDS, fetch)


def _turnover_rates(team_id: str, season: int) -> tuple[float, float, int]:
    """(takeaways per game, giveaways per game, games played) this
    regular season, or zeros before a team has played (or if ESPN can't
    be reached, which the model then treats the same way)."""
    def fetch():
        try:
            data = _get_json(f"{CORE_BASE}/seasons/{season}/types/2/teams/{team_id}/statistics")
            stats = {
                stat['name']: stat.get('value')
                for category in data['splits']['categories'] for stat in category['stats']
            }
            games = int(stats.get('gamesPlayed') or 0)
            if not games:
                return 0.0, 0.0, 0
            return (stats.get('totalTakeaways') or 0) / games, (stats.get('totalGiveaways') or 0) / games, games
        except Exception as e:
            logger.debug(f"nfl_props: no turnover stats for team {team_id}: {e}")
            return 0.0, 0.0, 0
    return _cached(('turnovers', team_id, season), _TEAM_STATS_TTL_SECONDS, fetch)


def _poisson_over(rate: float, line: float) -> float:
    """P(X > line) for X ~ Poisson(rate)."""
    at_most = sum(math.exp(-rate) * rate ** k / math.factorial(k) for k in range(int(line) + 1))
    return 1 - at_most


def _payout_pct(chance: float) -> int:
    return int((100 + PLAYER_EDGE_PCT) / chance)


def _takeaway_market(defense: Team, offense: Team, season: int) -> tuple[float, dict[str, int]]:
    """(line, payouts) for a defense's takeaways against this offense.

    Takeaways are rare, independent-ish events, which is what a Poisson
    distribution models. Its rate averages what this defense takes away
    per game with what this offense gives away per game, shrunk toward the
    league average (see PRIOR_GAMES). The line is whichever of 0.5 / 1.5 /
    2.5 comes closest to an even bet, and each side pays by its own chance."""
    takeaways, _, defense_games = _turnover_rates(defense.id, season)
    _, giveaways, offense_games = _turnover_rates(offense.id, season)
    games = min(defense_games, offense_games)
    observed = (takeaways + giveaways) / 2
    rate = (observed * games + LEAGUE_TAKEAWAYS_PER_GAME * PRIOR_GAMES) / (games + PRIOR_GAMES)

    line = min(TAKEAWAY_LINES, key=lambda l: abs(_poisson_over(rate, l) - 0.5))
    over = _poisson_over(rate, line)
    return line, {'over': _payout_pct(over), 'under': _payout_pct(1 - over)}


def _build_menu(game: Game) -> list[Prop]:
    lines = _player_lines(game)
    athletes = {athlete_id: ref for (_, athlete_id), (_, ref) in lines.items()}
    info = dict(zip(athletes, sports._executor.map(lambda a: _athlete(a, athletes[a]), athletes)))

    even = _payout_pct(0.5)
    menu = []
    for team, opponent in ((game.away, game.home), (game.home, game.away)):
        for role, prop_type, position in ROLES:
            candidates = [
                (line, athlete_id) for (type_id, athlete_id), (line, _) in lines.items()
                if type_id == prop_type and info.get(athlete_id)
                and info[athlete_id][1] == position and info[athlete_id][2] == team.id
            ]
            if not candidates:
                continue
            line, athlete_id = max(candidates)
            _, stat, label = PLAYER_STATS[prop_type]
            menu.append(Prop(
                key=f"{role.lower()}:{team.abbr}", role=role, team=team, subject=info[athlete_id][0],
                athlete_id=athlete_id, stat=stat, stat_label=label, line=line,
                payouts={'over': even, 'under': even},
            ))
        line, payouts = _takeaway_market(team, opponent, game.season)
        menu.append(Prop(
            key=f"def:{team.abbr}", role='DEF', team=team, subject=f"{team.name} defense",
            athlete_id=None, stat='takeaways', stat_label="takeaways", line=line, payouts=payouts,
        ))
    return menu


def menu(game: Game, max_age: float = _MENU_TTL_SECONDS) -> list[Prop]:
    """The props on offer for a game. Without the book's player lines
    there's no menu at all -- not even the defenses -- since a game with
    no props posted is usually days out and its matchup still unsettled."""
    def fetch():
        built = _build_menu(game)
        return built if any(p.role != 'DEF' for p in built) else []
    return _cached(('menu', game.event_id), max_age, fetch)


def warm_menus() -> None:
    """Rebuilds every open game's menu, so autocomplete never has to wait
    on ESPN. Never raises -- a game that fails just keeps its old menu."""
    def rebuild(game: Game):
        try:
            menu(game, max_age=0)
        except Exception as e:
            logger.debug(f"nfl_props: couldn't refresh the menu for {game.event_id}: {e}")
    list(_menu_executor.map(rebuild, board()))


def _menu_or_error(game: Game, max_age: float = _MENU_TTL_SECONDS) -> list[Prop]:
    try:
        props = menu(game, max_age)
    except Exception as e:
        logger.warning(f"nfl_props: couldn't build the menu for {game.event_id}: {e}")
        raise BetError("Couldn't reach ESPN for that game's props. Try again in a minute.") from e
    if not props:
        raise BetError(f"No props are posted for **{game.matchup}** yet. They usually go up a few days before kickoff.")
    return props


def prop_suggestions(game_text: str, current: str) -> list[tuple[str, str]]:
    game = find_game(game_text)
    if game is None:
        return []
    try:
        props = _menu_or_error(game, _MENU_STALE_SECONDS)
    except BetError:
        return []
    needle = current.strip().casefold()
    return [(p.label[:100], p.key) for p in props if needle in p.label.casefold() or needle == p.key.casefold()]


def resolve_prop(game: Game, text: str, max_age: float = _MENU_TTL_SECONDS) -> Prop:
    props = _menu_or_error(game, max_age)
    needle = text.strip().casefold()
    matches = [p for p in props if p.key.casefold() == needle] or [
        p for p in props if needle and needle in p.label.casefold()
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        listed = ", ".join(p.label for p in matches[:4])
        raise BetError(f"**{text}** matches more than one prop ({listed}). Be more specific.")
    raise BetError(
        f"**{text}** isn't on the menu for **{game.matchup}**. Props are each team's QB, RB1, WR1 and "
        f"defense — start typing in /propbet to pick one."
    )


SIDES = ('over', 'under')


def side_suggestions(game_text: str, prop_text: str) -> list[tuple[str, str]]:
    game = find_game(game_text)
    try:
        prop = resolve_prop(game, prop_text, _MENU_STALE_SECONDS) if game else None
    except BetError:
        prop = None
    if prop is None:
        return [("Over", 'over'), ("Under", 'under')]
    return [
        (f"{side.title()} {prop.line:g} — pays {economy.format_multiplier(prop.payouts[side])}", side)
        for side in SIDES
    ]


def resolve_side(text: str) -> str:
    side = {'o': 'over', 'u': 'under'}.get(text.strip().casefold(), text.strip().casefold())
    if side not in SIDES:
        raise BetError("Pick **over** or **under**.")
    return side


def menu_text(game_text: str | None) -> str:
    """/props: one game's whole menu with its lines and payouts -- the
    next game up when none is named."""
    games = board()
    if not games:
        return "No NFL games are open for prop bets right now."
    game = find_game(game_text) if game_text else games[0]
    if game is None:
        return f"No NFL game open for prop bets matches **{game_text}**. Try a team name, like `colts`."
    try:
        props = _menu_or_error(game)
    except BetError as e:
        return str(e)

    even = economy.format_multiplier(_payout_pct(0.5))
    lines = [f"## {game.describe()}"]
    for team in (game.away, game.home):
        lines.append(f"\n**{team.name}**")
        for prop in (p for p in props if p.team.id == team.id):
            if prop.role == 'DEF':
                over = economy.format_multiplier(prop.payouts['over'])
                under = economy.format_multiplier(prop.payouts['under'])
                lines.append(f"`DEF` Defense — takeaways o/u **{prop.line:g}** (over {over} · under {under})")
            else:
                lines.append(f"`{prop.role}` {prop.subject} — {prop.stat_label} o/u **{prop.line:g}**")
    lines.append(
        f"\n*Player props pay {even} either way. Lines are DraftKings', locked in when you bet. "
        f"/propbet to put coins on one.*"
    )
    return "\n".join(lines)


#=========================--BETS--===========================================#

def _connect() -> sqlite3.Connection:
    conn = economy.connect()
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE IF NOT EXISTS nfl_prop_bets ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "guild_id TEXT NOT NULL, "
        "user_id TEXT NOT NULL, "
        "channel_id TEXT NOT NULL, "  # where the result gets announced
        "event_id TEXT NOT NULL, "
        "matchup TEXT NOT NULL, "  # e.g. "Colts @ Commanders"
        "starts_at TEXT NOT NULL, "
        "role TEXT NOT NULL, "  # QB / RB1 / WR1 / DEF
        "team_id TEXT NOT NULL, "  # the player's team, or the defense's
        "subject TEXT NOT NULL, "  # "Josh Downs", or "Colts defense"
        "athlete_id TEXT, "  # NULL for a defense
        "stat TEXT NOT NULL, "  # box score key, or 'takeaways'
        "stat_label TEXT NOT NULL, "
        "line REAL NOT NULL, "  # locked in when placed, like the payout
        "side TEXT NOT NULL, "  # 'over' / 'under'
        "amount INTEGER NOT NULL, "
        "payout_pct INTEGER NOT NULL, "
        "placed_at TEXT NOT NULL, "
        "status TEXT NOT NULL DEFAULT 'open', "  # open / won / lost / push / refunded
        "result REAL, "  # the stat's final value
        "returned INTEGER, "
        "settled_at TEXT)"
    )
    return conn


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def place_bet(guild_id, user_id, channel_id, game: Game, prop: Prop, side: str, amount: int) -> int:
    """Takes the stake and records the bet in one transaction. Returns
    the payout percentage it locked in. Raises BetError if the game has
    kicked off or the bettor can't cover it."""
    if not game.open_for_bets:
        raise BetError(f"**{game.matchup}** has already kicked off — betting closes at kickoff.")
    pct = prop.payouts[side]
    with _connect() as conn:
        if not economy.take_bet(guild_id, user_id, amount, conn=conn):
            raise BetError(
                f"You only have {economy.format_coins(economy.get_balance(guild_id, user_id))} — "
                f"not enough to bet {economy.format_coins(amount)}."
            )
        conn.execute(
            "INSERT INTO nfl_prop_bets (guild_id, user_id, channel_id, event_id, matchup, starts_at, role, "
            "team_id, subject, athlete_id, stat, stat_label, line, side, amount, payout_pct, placed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(guild_id), str(user_id), str(channel_id), game.event_id, game.matchup,
                game.starts_at.isoformat(), prop.role, prop.team.id, prop.subject, prop.athlete_id,
                prop.stat, prop.stat_label, prop.line, side, amount, pct, _now(),
            ),
        )
    return pct


def _bet_name(subject: str, side: str, line: float, stat_label: str) -> str:
    """e.g. "Josh Downs over 62.5 receiving yards" """
    return f"{subject} {side} {line:g} {stat_label}"


def open_bets_text(guild_id, user_id) -> str | None:
    """This user's unsettled prop bets in this server, one per line, or None."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM nfl_prop_bets WHERE guild_id = ? AND user_id = ? AND status = 'open' "
            "ORDER BY starts_at",
            (str(guild_id), str(user_id)),
        ).fetchall()
    if not rows:
        return None
    return "\n".join(
        f"🏈 {row['matchup']} · {sportsbook._kickoff(sportsbook._parse_time(row['starts_at']))} — "
        f"{economy.format_coins(row['amount'])} on "
        f"**{_bet_name(row['subject'], row['side'], row['line'], row['stat_label'])}**, "
        f"pays {economy.format_coins(row['amount'] * row['payout_pct'] // 100)}"
        for row in rows
    )


#=========================--SETTLING--=======================================#

@dataclass
class BoxScore:
    state: str
    completed: bool
    stats: dict[str, dict[str, float]]  # athlete id -> {box score key: value}
    played: set[str]                     # every athlete id anywhere in the box score
    turnovers: dict[str, int]            # team id -> turnovers committed


def _number(text) -> float | None:
    try:
        return float(str(text).replace(',', ''))
    except (TypeError, ValueError):
        return None


def _box_score(event_id: str) -> BoxScore:
    data = _get_json(SUMMARY_URL, {'event': event_id})
    status = data['header']['competitions'][0]['status']['type']
    boxscore = data.get('boxscore') or {}

    stats: dict[str, dict[str, float]] = {}
    played = set()
    for team in boxscore.get('players', []):
        for category in team.get('statistics', []):
            keys = category.get('keys', [])
            for entry in category.get('athletes', []):
                athlete_id = str(entry['athlete']['id'])
                played.add(athlete_id)
                for key, value in zip(keys, entry.get('stats', [])):
                    number = _number(value)
                    if number is not None:
                        stats.setdefault(athlete_id, {})[key] = number

    turnovers = {}
    for team in boxscore.get('teams', []):
        for stat in team.get('statistics', []):
            if stat.get('name') == 'turnovers':
                value = _number(stat.get('displayValue'))
                if value is not None:
                    turnovers[str(team['team']['id'])] = int(value)
    return BoxScore(status['state'], bool(status.get('completed')), stats, played, turnovers)


def _actual(bet: sqlite3.Row, box: BoxScore) -> float | None:
    """The stat's final value for this bet, or None if the bet is void --
    the player never got on the field, or the box score is incomplete."""
    if bet['stat'] == 'takeaways':
        # A defense's takeaways are the other team's turnovers.
        opponents = [count for team_id, count in box.turnovers.items() if team_id != bet['team_id']]
        return float(opponents[0]) if len(opponents) == 1 else None
    athlete_id = bet['athlete_id']
    if athlete_id in box.stats and bet['stat'] in box.stats[athlete_id]:
        return box.stats[athlete_id][bet['stat']]
    # In the box score somewhere but not this category: he played and
    # just didn't record any (a WR held without a catch). Nowhere at all
    # means he didn't play, which voids the bet, the standard book rule.
    return 0.0 if athlete_id in box.played else None


def _grade(bet: sqlite3.Row, actual: float | None) -> tuple[str, int, str]:
    """(status, coins returned, what happened)."""
    title = f"🏈 **{bet['matchup']}**"
    stake = economy.format_coins(bet['amount'])
    name = f"**{_bet_name(bet['subject'], bet['side'], bet['line'], bet['stat_label'])}**"
    if actual is None:
        return 'refunded', bet['amount'], f"{title}\n{bet['subject']} didn't play, so your {stake} on {name} comes back."

    verb = "forced" if bet['stat'] == 'takeaways' else "had"
    label = bet['stat_label'][:-1] if actual == 1 else bet['stat_label']  # "1 takeaway", "1 receiving yard"
    headline = f"{title} — {bet['subject']} {verb} **{actual:g}** {label}"
    if actual == bet['line']:
        return 'push', bet['amount'], f"{headline}\nRight on the line, so your {stake} on {name} comes back."
    hit = actual > bet['line'] if bet['side'] == 'over' else actual < bet['line']
    if hit:
        return 'won', bet['amount'] * bet['payout_pct'] // 100, f"{headline}\nYour {stake} on {name} won! 🎉"
    return 'lost', 0, f"{headline}\nYour {stake} on {name} lost."


def _settle(bet: sqlite3.Row, status: str, returned: int, actual: float | None) -> int | None:
    """Marks the bet settled and pays it out together. Returns the new
    balance, or None if something else already settled it."""
    with _connect() as conn:
        cursor = conn.execute(
            "UPDATE nfl_prop_bets SET status = ?, result = ?, returned = ?, settled_at = ? "
            "WHERE id = ? AND status = 'open'",
            (status, actual, returned, _now(), bet['id']),
        )
        if cursor.rowcount != 1:
            return None
        return economy.pay(bet['guild_id'], bet['user_id'], returned, conn=conn)


def settle_finished_bets() -> list[Settlement]:
    """Checks every game with open prop bets, settling them once it's
    final (or refunding them if it was called off) and returning what to
    tell each bettor. Safe to run as often as you like."""
    with _connect() as conn:
        open_bets = conn.execute("SELECT * FROM nfl_prop_bets WHERE status = 'open'").fetchall()
    by_game: dict[str, list[sqlite3.Row]] = {}
    for bet in open_bets:
        by_game.setdefault(bet['event_id'], []).append(bet)

    now = datetime.datetime.now(datetime.timezone.utc)
    settlements = []
    for event_id, bets in by_game.items():
        try:
            box = _box_score(event_id)
        except Exception as e:
            logger.warning(f"nfl_props: couldn't check game {event_id}: {e}")
            box = None

        if box and box.state == 'post' and box.completed:
            graded = []
            for bet in bets:
                actual = _actual(bet, box)
                graded.append((bet, actual, *_grade(bet, actual)))
        elif (box and box.state == 'post') or now - sportsbook._parse_time(bets[0]['starts_at']) > STALE_AFTER:
            # Over without being completed (postponed, cancelled), or never finished.
            graded = [
                (bet, None, 'refunded', bet['amount'],
                 f"🏈 **{bet['matchup']}** was called off, so your {economy.format_coins(bet['amount'])} on "
                 f"**{_bet_name(bet['subject'], bet['side'], bet['line'], bet['stat_label'])}** comes back.")
                for bet in bets
            ]
        else:
            continue

        for bet, actual, status, returned, text in graded:
            balance = _settle(bet, status, returned, actual)
            if balance is None:
                continue
            settlements.append(Settlement(
                user_id=int(bet['user_id']),
                channel_id=int(bet['channel_id']),
                text=f"{text}\n{economy.outcome_line(bet['amount'], returned, balance)}",
            ))
    return settlements
