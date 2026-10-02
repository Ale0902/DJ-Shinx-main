"""Sports betting for the play-money economy: /bet puts coins on an NFL
or soccer game happening today or tomorrow, any time before it kicks off,
and once the game ends the bettor gets pinged with how it went.

Games, odds and results all come from the same ESPN API sports.py uses,
across the NFL and every soccer competition /soccer covers. Payouts start
from the sportsbook moneyline ESPN shows, but with the bookmaker's cut
taken out and a small edge for the player added instead -- the same
~102% the casino games pay back (see _payouts). A bet's payout is locked
in when it's placed: odds move, and ESPN stops showing them at kickoff.

Bets live in economy.db next to the wallets, so taking a stake and
recording its bet commit together, as do marking a bet settled and paying
it out -- a crash in between can't lose a stake or pay a bet twice.
"""
import datetime
import logging
import sqlite3
import time
from dataclasses import dataclass

import economy
import sports

logger = logging.getLogger(__name__)

# Fair odds plus this many percent: a bet that's truly a coin flip pays 2.04x.
PLAYER_EDGE_PCT = 2

# A bet whose game still hasn't finished this long after its scheduled
# start gets its stake back -- e.g. a postponement ESPN never resolved.
STALE_AFTER = datetime.timedelta(days=7)

# Betting on a game opens this many days before game day: 1 means
# today's and tomorrow's games are on the board.
DAYS_AHEAD = 1

# How long the list of games open for bets is reused before fetching it
# again. /bet's autocomplete asks on every keystroke, and it's one
# scoreboard request per league per day.
BOARD_TTL_SECONDS = 60

# league key -> (competition name, ESPN scoreboard URL). The key is 'nfl'
# or an ESPN soccer slug like 'eng.1'.
LEAGUES = {'nfl': ("NFL", sports.NFL_SCOREBOARD_URL)} | {
    slug: (name, sports._soccer_scoreboard_url(slug))
    for name, slugs in sports.SOCCER_COMPETITIONS
    for slug in slugs
}


class BetError(Exception):
    """A bet that can't be placed, carrying a message for the bettor."""


def _matchup(league: str, home: str, away: str) -> str:
    # Each sport's own convention: away @ home for the NFL, home vs away for soccer.
    return f"{away} @ {home}" if league == 'nfl' else f"{home} vs {away}"


def _sport_emoji(league: str) -> str:
    return '🏈' if league == 'nfl' else '⚽'


def _kickoff(starts_at: datetime.datetime) -> str:
    """e.g. "1:00 PM ET" for a game today, "Tomorrow 1:00 PM ET", or
    "Sun 1:00 PM ET" for any other day (an open bet from yesterday)."""
    local = starts_at.astimezone(sports.EASTERN)
    days_away = (local.date() - datetime.datetime.now(sports.EASTERN).date()).days
    time_text = sports._format_time(local)
    if days_away == 0:
        return time_text
    if days_away == 1:
        return f"Tomorrow {time_text}"
    return f"{local:%a} {time_text}"


def _parse_time(text: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(text.replace('Z', '+00:00'))


@dataclass
class Game:
    league: str
    event_id: str
    starts_at: datetime.datetime
    state: str  # ESPN's 'pre', 'in' or 'post'
    home: str  # full names, e.g. "Washington Commanders"
    away: str
    home_short: str  # e.g. "Commanders"
    away_short: str
    home_abbr: str  # e.g. "WSH"
    away_abbr: str
    payouts: dict[str, int]  # 'home' / 'away' / 'draw' -> payout_pct; empty when no odds are posted

    @property
    def key(self) -> str:
        """What /bet's autocomplete hands back for this game."""
        return f"{self.league}:{self.event_id}"

    @property
    def matchup(self) -> str:
        return _matchup(self.league, self.home_short, self.away_short)

    @property
    def open_for_bets(self) -> bool:
        started = self.starts_at <= datetime.datetime.now(datetime.timezone.utc)
        return self.state == 'pre' and not started and bool(self.payouts)

    @property
    def picks(self) -> list[str]:
        """The picks on offer, in the order the matchup reads."""
        order = ['away', 'home'] if self.league == 'nfl' else ['home', 'draw', 'away']
        return [pick for pick in order if pick in self.payouts]

    def pick_name(self, pick: str) -> str:
        return {'home': self.home, 'away': self.away, 'draw': "Draw"}[pick]

    def describe(self) -> str:
        """e.g. "🏈 Colts @ Commanders · Tomorrow 1:00 PM ET · NFL" """
        return f"{_sport_emoji(self.league)} {self.matchup} · {_kickoff(self.starts_at)} · {LEAGUES[self.league][0]}"

    def matches(self, needle: str) -> bool:
        """Whether casefolded text typed by a bettor refers to this game."""
        names = [self.home, self.away, self.home_short, self.away_short, self.matchup]
        return any(needle in name.casefold() for name in names) or needle in (
            self.home_abbr.casefold(), self.away_abbr.casefold()
        )


#=========================--ODDS--===========================================#

def _american_odds_probability(odds) -> float | None:
    """The win probability a moneyline like "+160" or "-192" implies."""
    text = str(odds).strip().upper()
    if text == 'EVEN':
        return 0.5
    try:
        value = int(text.replace('+', ''))
    except ValueError:
        return None
    if value >= 100:
        return 100 / (value + 100)
    if value <= -100:
        return -value / (-value + 100)
    return None


def _payouts(competition: dict, soccer: bool) -> dict[str, int]:
    """payout_pct for each pick, from the sportsbook moneyline ESPN shows.

    A bookmaker's odds imply probabilities adding up to more than 100% --
    the excess is its cut. Scaling them back down to 100% gives fair odds,
    which then get PLAYER_EDGE_PCT on top. Empty when any side's line is
    missing, since the cut can't be worked out from only some of them."""
    try:
        odds_entry = competition['odds'][0]
    except (KeyError, IndexError, TypeError):
        return {}
    if not isinstance(odds_entry, dict):
        return {}  # ESPN sends "odds": [null] once a game has no line
    moneyline = odds_entry.get('moneyline') or {}
    probabilities = {}
    for pick in (('home', 'draw', 'away') if soccer else ('home', 'away')):
        line = moneyline.get(pick) or {}
        odds = (line.get('close') or line.get('open') or {}).get('odds')
        if odds is None and pick == 'draw':
            odds = (odds_entry.get('drawOdds') or {}).get('moneyLine')
        probability = _american_odds_probability(odds) if odds is not None else None
        if not probability:
            return {}
        probabilities[pick] = probability
    total = sum(probabilities.values())
    return {pick: int(total / p * (100 + PLAYER_EDGE_PCT)) for pick, p in probabilities.items()}


#=========================--THE BOARD--======================================#

def _parse_game(league: str, event: dict) -> Game | None:
    try:
        competition = event['competitions'][0]
        teams = {c['homeAway']: c['team'] for c in competition['competitors']}
        home, away = teams['home'], teams['away']
        return Game(
            league=league,
            event_id=str(event['id']),
            starts_at=_parse_time(event['date']),
            state=competition['status']['type']['state'],
            home=home['displayName'],
            away=away['displayName'],
            home_short=home.get('shortDisplayName') or home['displayName'],
            away_short=away.get('shortDisplayName') or away['displayName'],
            home_abbr=home.get('abbreviation', ''),
            away_abbr=away.get('abbreviation', ''),
            payouts=_payouts(competition, soccer=league != 'nfl'),
        )
    except (KeyError, IndexError, TypeError, ValueError, AttributeError) as e:
        logger.debug(f"sportsbook: skipping unreadable {league} event {event.get('id')}: {e}")
        return None


def _betting_dates() -> list[datetime.date]:
    """Today through DAYS_AHEAD days from now, Eastern time."""
    today = datetime.datetime.now(sports.EASTERN).date()
    return [today + datetime.timedelta(days=i) for i in range(DAYS_AHEAD + 1)]


def _league_games(league: str, date: datetime.date) -> list[Game]:
    """Every game in this league starting on this date, Eastern time,
    whether or not it's still open for bets."""
    events = sports._fetch_scoreboard(LEAGUES[league][1], date=date).get('events', [])
    games = [_parse_game(league, event) for event in events]
    return [g for g in games if g and g.starts_at.astimezone(sports.EASTERN).date() == date]


_board: tuple[float, list[Game]] | None = None


def board() -> list[Game]:
    """Every game from today through DAYS_AHEAD still taking bets,
    soonest first. Fetched at most once per BOARD_TTL_SECONDS; a game that
    kicks off in the meantime still drops out, since open_for_bets is
    checked fresh each call."""
    global _board
    if _board is None or time.monotonic() - _board[0] >= BOARD_TTL_SECONDS:
        def fetch(job):
            league, date = job
            try:
                return _league_games(league, date)
            except Exception as e:
                logger.debug(f"sportsbook: couldn't fetch {league} games for {date}: {e}")
                return []

        jobs = [(league, date) for league in LEAGUES for date in _betting_dates()]
        games = [game for batch in sports._executor.map(fetch, jobs) for game in batch]
        _board = (time.monotonic(), sorted(games, key=lambda g: g.starts_at))
    return [game for game in _board[1] if game.open_for_bets]


def game_suggestions(current: str) -> list[tuple[str, str]]:
    """(label, value) choices for /bet's game autocomplete."""
    needle = current.strip().casefold()
    games = [g for g in board() if g.matches(needle) or needle in LEAGUES[g.league][0].casefold()]
    return [(g.describe()[:100], g.key) for g in games[:25]]


def cached_game(text: str) -> Game | None:
    """The game a /bet game field refers to, from the cached list only --
    for the pick autocomplete, which has to answer fast."""
    games = board()
    matches = [g for g in games if g.key == text.strip()] or [g for g in games if g.matches(text.strip().casefold())]
    return matches[0] if len(matches) == 1 else None


def pick_suggestions(game: Game) -> list[tuple[str, str]]:
    return [
        (f"{game.pick_name(pick)} — pays {economy.format_multiplier(game.payouts[pick])}", pick)
        for pick in game.picks
    ]


def _fresh_game(league: str, event_id: str) -> Game | None:
    for date in _betting_dates():
        try:
            games = _league_games(league, date)
        except Exception as e:
            logger.warning(f"sportsbook: couldn't fetch {league} games for {date}: {e}")
            raise BetError("Couldn't reach ESPN to check that game. Try again in a minute.") from e
        game = next((g for g in games if g.event_id == event_id), None)
        if game:
            return game
    return None


def resolve_game(text: str) -> Game:
    """The game a bettor means -- an autocomplete value, or a team name
    or abbreviation typed by hand -- freshly fetched, so the kickoff check
    and the odds a bet locks in aren't up to a minute stale. Raises
    BetError if it isn't a game taking bets right now."""
    text = text.strip()
    league, _, event_id = text.partition(':')
    if league not in LEAGUES or not event_id:
        candidates = [g for g in board() if g.matches(text.casefold())]
        if len(candidates) > 1:
            listed = ", ".join(g.matchup for g in candidates[:5])
            raise BetError(f"**{text}** matches more than one game on the board ({listed}). Be more specific.")
        league, event_id = (candidates[0].league, candidates[0].event_id) if candidates else (None, None)

    game = _fresh_game(league, event_id) if league else None
    if game is None:
        raise BetError(
            f"Couldn't find a game open for bets matching **{text}**. "
            f"Start typing a team in /bet to pick from today's and tomorrow's games."
        )
    if game.state != 'pre' or game.starts_at <= datetime.datetime.now(datetime.timezone.utc):
        raise BetError(f"**{game.matchup}** has already kicked off — betting closes at kickoff.")
    if not game.payouts:
        raise BetError(f"No odds are posted for **{game.matchup}** yet, so it can't be bet on.")
    return game


def resolve_pick(game: Game, text: str) -> str:
    """'home', 'away' or 'draw' from what the bettor picked, or BetError."""
    needle = text.strip().casefold()
    if needle == 'tie':
        needle = 'draw'
    if needle in game.payouts:
        return needle
    teams = {
        'home': (game.home, game.home_short, game.home_abbr),
        'away': (game.away, game.away_short, game.away_abbr),
    }
    matches = [
        pick for pick, (name, short, abbr) in teams.items()
        if needle and (needle in name.casefold() or needle in short.casefold() or needle == abbr.casefold())
    ]
    if len(matches) == 1:
        return matches[0]
    options = " or ".join(f"**{game.pick_name(pick)}**" for pick in game.picks)
    raise BetError(f"Pick {options}.")


#=========================--BETS--===========================================#

def _connect() -> sqlite3.Connection:
    conn = economy.connect()
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE IF NOT EXISTS sports_bets ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "guild_id TEXT NOT NULL, "
        "user_id TEXT NOT NULL, "
        "channel_id TEXT NOT NULL, "  # where the result gets announced
        "league TEXT NOT NULL, "
        "event_id TEXT NOT NULL, "
        "home TEXT NOT NULL, "  # short team names, for the result message
        "away TEXT NOT NULL, "
        "starts_at TEXT NOT NULL, "
        "pick TEXT NOT NULL, "  # 'home' / 'away' / 'draw'
        "pick_name TEXT NOT NULL, "
        "amount INTEGER NOT NULL, "
        "payout_pct INTEGER NOT NULL, "  # locked in when placed
        "placed_at TEXT NOT NULL, "
        "status TEXT NOT NULL DEFAULT 'open', "  # open / won / lost / push / refunded
        "returned INTEGER, "
        "settled_at TEXT)"
    )
    return conn


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def place_bet(guild_id, user_id, channel_id, game: Game, pick: str, amount: int) -> int:
    """Takes the stake and records the bet in one transaction. Returns
    what the bet pays back if it wins. Raises BetError if the bettor
    can't cover it."""
    payout_pct = game.payouts[pick]
    with _connect() as conn:
        if not economy.take_bet(guild_id, user_id, amount, conn=conn):
            raise BetError(
                f"You only have {economy.format_coins(economy.get_balance(guild_id, user_id))} — "
                f"not enough to bet {economy.format_coins(amount)}."
            )
        conn.execute(
            "INSERT INTO sports_bets (guild_id, user_id, channel_id, league, event_id, home, away, "
            "starts_at, pick, pick_name, amount, payout_pct, placed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(guild_id), str(user_id), str(channel_id), game.league, game.event_id,
                game.home_short, game.away_short, game.starts_at.isoformat(), pick,
                game.pick_name(pick), amount, payout_pct, _now(),
            ),
        )
    return amount * payout_pct // 100


def open_bets_text(guild_id, user_id) -> str | None:
    """This user's unsettled bets in this server, one per line, or None."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM sports_bets WHERE guild_id = ? AND user_id = ? AND status = 'open' "
            "ORDER BY starts_at",
            (str(guild_id), str(user_id)),
        ).fetchall()
    if not rows:
        return None
    return "\n".join(
        f"{_sport_emoji(row['league'])} {_matchup(row['league'], row['home'], row['away'])} · "
        f"{_kickoff(_parse_time(row['starts_at']))} — {economy.format_coins(row['amount'])} on "
        f"**{row['pick_name']}**, pays {economy.format_coins(row['amount'] * row['payout_pct'] // 100)}"
        for row in rows
    )


#=========================--SETTLING--=======================================#

@dataclass
class Settlement:
    """A bet that just paid out (or didn't), for the bot to tell its bettor about."""
    user_id: int
    channel_id: int
    text: str


def _summary_url(league: str) -> str:
    sport = 'football/nfl' if league == 'nfl' else f'soccer/{league}'
    return f"{sports.ESPN_SITE_BASE}/{sport}/summary"


def _fetch_result(league: str, event_id: str) -> tuple[str, bool, int, int]:
    """(state, completed, home score, away score) for one game."""
    data = sports._get_json(_summary_url(league), params={'event': event_id})
    competition = data['header']['competitions'][0]
    status = competition['status']['type']
    scores = {c['homeAway']: int(c.get('score') or 0) for c in competition['competitors']}
    return status['state'], bool(status.get('completed')), scores['home'], scores['away']


def _grade(bet: sqlite3.Row, home_score: int, away_score: int) -> tuple[str, int]:
    """(status, coins returned) for a bet on a finished game. Goes by the
    final score -- in a soccer knockout tie that includes extra time,
    where a real sportsbook would settle on the 90 minutes alone."""
    winnings = bet['amount'] * bet['payout_pct'] // 100
    if home_score == away_score:
        if bet['pick'] == 'draw':
            return 'won', winnings
        if bet['league'] == 'nfl':
            return 'push', bet['amount']  # an NFL tie: there was no draw to bet on
        return 'lost', 0
    winner = 'home' if home_score > away_score else 'away'
    return ('won', winnings) if bet['pick'] == winner else ('lost', 0)


def _settle(bet: sqlite3.Row, status: str, returned: int) -> int | None:
    """Marks the bet settled and pays it out together. Returns the new
    balance, or None if something else already settled it."""
    with _connect() as conn:
        cursor = conn.execute(
            "UPDATE sports_bets SET status = ?, returned = ?, settled_at = ? WHERE id = ? AND status = 'open'",
            (status, returned, _now(), bet['id']),
        )
        if cursor.rowcount != 1:
            return None
        return economy.pay(bet['guild_id'], bet['user_id'], returned, conn=conn)


def _result_text(bet: sqlite3.Row, status: str, returned: int, balance: int, final: str | None, refund_reason: str) -> str:
    matchup = f"{_sport_emoji(bet['league'])} **{_matchup(bet['league'], bet['home'], bet['away'])}**"
    stake = economy.format_coins(bet['amount'])
    pick = f"**{bet['pick_name']}**"
    if status == 'refunded':
        lines = [f"{matchup} {refund_reason}, so your {stake} on {pick} comes back."]
    else:
        lines = [f"{matchup} — final: {final}"]
        if status == 'won':
            lines.append(f"Your {stake} on {pick} won! 🎉")
        elif status == 'push':
            lines.append(f"It ended in a tie, so your {stake} on {pick} comes back.")
        else:
            lines.append(f"Your {stake} on {pick} lost.")
    lines.append(economy.outcome_line(bet['amount'], returned, balance))
    return "\n".join(lines)


def settle_finished_bets() -> list[Settlement]:
    """Checks every game with open bets on it, settling the bets on any
    that finished (or were called off) and returning what to tell each
    bettor. Safe to run as often as you like: a game still in progress
    is just left for next time."""
    with _connect() as conn:
        open_bets = conn.execute("SELECT * FROM sports_bets WHERE status = 'open'").fetchall()
    by_game: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for bet in open_bets:
        by_game.setdefault((bet['league'], bet['event_id']), []).append(bet)

    now = datetime.datetime.now(datetime.timezone.utc)
    settlements = []
    for (league, event_id), bets in by_game.items():
        try:
            state, completed, home_score, away_score = _fetch_result(league, event_id)
        except Exception as e:
            logger.warning(f"sportsbook: couldn't check {league} game {event_id}: {e}")
            state = completed = home_score = away_score = None

        final = None
        refund_reason = ""
        if state == 'post' and completed:
            graded = [(bet, *_grade(bet, home_score, away_score)) for bet in bets]
            first = bets[0]
            if league == 'nfl':
                final = f"{first['away']} {away_score} – {home_score} {first['home']}"
            else:
                final = f"{first['home']} {home_score} – {away_score} {first['away']}"
        elif state == 'post':
            # Over without being completed: postponed, cancelled or abandoned.
            graded = [(bet, 'refunded', bet['amount']) for bet in bets]
            refund_reason = "was called off"
        elif now - _parse_time(bets[0]['starts_at']) > STALE_AFTER:
            graded = [(bet, 'refunded', bet['amount']) for bet in bets]
            refund_reason = "never finished"
        else:
            continue

        for bet, status, returned in graded:
            balance = _settle(bet, status, returned)
            if balance is None:
                continue
            settlements.append(Settlement(
                user_id=int(bet['user_id']),
                channel_id=int(bet['channel_id']),
                text=_result_text(bet, status, returned, balance, final, refund_reason),
            ))
    return settlements
