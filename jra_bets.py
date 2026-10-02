"""Horse betting for the play-money economy: /horsebet puts coins on a
horse to win one of the JRA graded stakes races jra.py covers, any time
before post time, and once the result is in the bettor gets pinged with
how it went -- by the same settlement loop as sportsbook.py's bets.

Payouts follow sportsbook.py's rule: fair odds plus PLAYER_EDGE_PCT. The
fair chance is the horse's Win % from jra.win_probabilities -- the
market's odds with the JRA's takeout stripped out -- so a horse given a
25% chance pays 4.08x, better than the track itself would. A bet's
payout is locked in when it's placed: before JRA betting opens that's
netkeiba's projected odds, after it the live pool.

Bets live in economy.db next to the wallets and the sports bets, for the
same reason: taking a stake and recording its bet commit together, as do
settling a bet and paying it out.
"""
import datetime
import logging
import sqlite3

import economy
import jra
import sportsbook
from sportsbook import PLAYER_EDGE_PCT, BetError, Settlement

logger = logging.getLogger(__name__)

# The most a winning bet pays back: 100x the stake. A 999.9 outsider's
# fair odds would otherwise pay thousands of times over -- fine on
# average, but one lucky ticket could mint more coins than a server earns
# from /work in a year, and projected odds that long are mostly guesswork.
MAX_PAYOUT_PCT = 10_000

# A JRA result is provisional until the stewards confirm it, which after
# an inquiry can take a while -- so bets wait this long past post time
# before they're settled on whatever the result page says.
SETTLE_AFTER = datetime.timedelta(minutes=20)

# A race with no result this long after post time (abandoned for
# weather, say) has its bets refunded.
STALE_AFTER = datetime.timedelta(days=3)


def payout_pct(chance: float) -> int:
    """What a winning bet pays back, as a percentage of the stake, for a
    horse with this chance of winning."""
    return min(MAX_PAYOUT_PCT, int((100 + PLAYER_EDGE_PCT) / chance))


def describe(race: jra.Race) -> str:
    """e.g. "🏇 G2 · MAINICHI OKAN · Tomorrow 2:45 AM ET · Tokyo R11" """
    return f"🏇 {jra.race_title(race)} · {sportsbook._kickoff(race.post_time)} · {race.venue} R{race.number}"


#=========================--PICKING A HORSE--================================#

def resolve_race(text: str) -> jra.Race:
    """The upcoming race a bettor means -- an autocomplete value (a race
    id) or part of a race's name typed by hand. Raises BetError if it
    isn't a graded race that's still to run."""
    race = jra.find_race(text, jra.upcoming_races()) if text.strip() else None
    if race is None:
        raise BetError(
            f"Couldn't find an upcoming JRA graded race matching **{text}**. "
            f"Start typing in /horsebet to pick from the card, or see /jra."
        )
    return race


def _field(race: jra.Race) -> list[tuple[jra.Runner, float]]:
    field = jra.contenders(race.race_id)
    if not field:
        raise BetError(
            f"There are no odds for **{jra.race_title(race)}** yet, so it can't be bet on. "
            f"Horses get their numbers and odds two days or so before the race."
        )
    return field


def resolve_horse(race: jra.Race, text: str) -> tuple[jra.Runner, float]:
    """(runner, win chance) for the horse a bettor picked, by number or
    by name, or BetError."""
    field = _field(race)
    needle = text.strip().lstrip('#').casefold()
    if needle.isdigit():
        matches = [pair for pair in field if pair[0].number == int(needle)]
    else:
        matches = [pair for pair in field if needle and needle in pair[0].name.casefold()]
        matches = [pair for pair in matches if pair[0].name.casefold() == needle] or matches
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        listed = ", ".join(f"#{runner.number} {runner.name}" for runner, _ in matches[:5])
        raise BetError(f"**{text}** matches more than one horse ({listed}). Be more specific, or use its number.")
    raise BetError(
        f"No horse running in **{jra.race_title(race)}** matches **{text}**. "
        f"Start typing in /horsebet to pick from the field."
    )


def horse_suggestions(race_text: str, current: str) -> list[tuple[str, str]]:
    """(label, value) choices for /horsebet's horse autocomplete: the field
    of whichever race is filled in, likeliest winner first."""
    try:
        race = resolve_race(race_text)
        field = _field(race)
    except BetError:
        return []
    needle = current.strip().lstrip('#').casefold()
    return [
        (
            f"#{runner.number} {runner.name} — {jra.percent(chance)} to win, "
            f"pays {economy.format_multiplier(payout_pct(chance))}"[:100],
            str(runner.number),
        )
        for runner, chance in field
        if needle in runner.name.casefold() or needle == str(runner.number)
    ][:25]


#=========================--BETS--===========================================#

def _connect() -> sqlite3.Connection:
    conn = economy.connect()
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE IF NOT EXISTS horse_bets ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "guild_id TEXT NOT NULL, "
        "user_id TEXT NOT NULL, "
        "channel_id TEXT NOT NULL, "  # where the result gets announced
        "race_id TEXT NOT NULL, "
        "race_title TEXT NOT NULL, "  # e.g. "G2 · MAINICHI OKAN", for the result message
        "post_time TEXT NOT NULL, "
        "horse_number INTEGER NOT NULL, "
        "horse_name TEXT NOT NULL, "
        "amount INTEGER NOT NULL, "
        "payout_pct INTEGER NOT NULL, "  # locked in when placed
        "placed_at TEXT NOT NULL, "
        "status TEXT NOT NULL DEFAULT 'open', "  # open / won / lost / refunded
        "returned INTEGER, "
        "settled_at TEXT)"
    )
    return conn


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def place_bet(guild_id, user_id, channel_id, race: jra.Race, runner: jra.Runner, chance: float, amount: int) -> int:
    """Takes the stake and records the bet in one transaction. Returns
    the payout percentage it locked in. Raises BetError if the bettor
    can't cover it."""
    pct = payout_pct(chance)
    title = jra.race_title(race)
    with _connect() as conn:
        if not economy.take_bet(guild_id, user_id, amount, conn=conn):
            raise BetError(
                f"You only have {economy.format_coins(economy.get_balance(guild_id, user_id))} — "
                f"not enough to bet {economy.format_coins(amount)}."
            )
        conn.execute(
            "INSERT INTO horse_bets (guild_id, user_id, channel_id, race_id, race_title, post_time, "
            "horse_number, horse_name, amount, payout_pct, placed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(guild_id), str(user_id), str(channel_id), race.race_id, title,
                race.post_time.isoformat(), runner.number, runner.name, amount, pct, _now(),
            ),
        )
    return pct


def open_bets_text(guild_id, user_id) -> str | None:
    """This user's unsettled horse bets in this server, one per line, or None."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM horse_bets WHERE guild_id = ? AND user_id = ? AND status = 'open' "
            "ORDER BY post_time",
            (str(guild_id), str(user_id)),
        ).fetchall()
    if not rows:
        return None
    return "\n".join(
        f"🏇 {row['race_title']} · {sportsbook._kickoff(datetime.datetime.fromisoformat(row['post_time']))} — "
        f"{economy.format_coins(row['amount'])} on **#{row['horse_number']} {row['horse_name']}**, "
        f"pays {economy.format_coins(row['amount'] * row['payout_pct'] // 100)}"
        for row in rows
    )


#=========================--SETTLING--=======================================#

def _ordinal(n: int) -> str:
    suffix = 'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')
    return f"{n}{suffix}"


def _grade(bet: sqlite3.Row, results: list[dict]) -> tuple[str, int, str]:
    """(status, coins returned, what happened) for a bet on a race that's
    been run. A dead heat for first splits the payout between the tied
    horses, the usual bookmaker rule."""
    title = f"🏇 **{bet['race_title']}**"
    stake = economy.format_coins(bet['amount'])
    horse = f"**#{bet['horse_number']} {bet['horse_name']}**"
    winners = [f for f in results if f['place'] == 1]
    headline = f"{title} — won by " + " and ".join(f"**{w['horse']}**" for w in winners)

    finish = next((f for f in results if f['number'] == bet['horse_number']), None)
    if finish is None:
        # Not among the horses that ran: scratched after the bet went on.
        return 'refunded', bet['amount'], f"{title}\n{horse} was scratched, so your {stake} comes back."
    if finish['place'] == 1:
        winnings = bet['amount'] * bet['payout_pct'] // 100 // len(winners)
        dead_heat = f" Dead heat for first, so the payout is split {len(winners)} ways." if len(winners) > 1 else ""
        return 'won', winnings, f"{headline}\nYour {stake} on {horse} won! 🎉{dead_heat}"
    finished = f"finished {_ordinal(finish['place'])}" if finish['place'] else "didn't finish"
    return 'lost', 0, f"{headline}\nYour {stake} on {horse} lost — it {finished}."


def _settle(bet: sqlite3.Row, status: str, returned: int) -> int | None:
    """Marks the bet settled and pays it out together. Returns the new
    balance, or None if something else already settled it."""
    with _connect() as conn:
        cursor = conn.execute(
            "UPDATE horse_bets SET status = ?, returned = ?, settled_at = ? WHERE id = ? AND status = 'open'",
            (status, returned, _now(), bet['id']),
        )
        if cursor.rowcount != 1:
            return None
        return economy.pay(bet['guild_id'], bet['user_id'], returned, conn=conn)


def settle_finished_bets() -> list[Settlement]:
    """Checks every race with open bets on it, settling the bets on any
    whose result is in (or that never got one) and returning what to tell
    each bettor. Safe to run as often as you like: a race not yet run,
    or not yet confirmed, is just left for next time."""
    with _connect() as conn:
        open_bets = conn.execute("SELECT * FROM horse_bets WHERE status = 'open'").fetchall()
    by_race: dict[str, list[sqlite3.Row]] = {}
    for bet in open_bets:
        by_race.setdefault(bet['race_id'], []).append(bet)

    now = datetime.datetime.now(datetime.timezone.utc)
    settlements = []
    for race_id, bets in by_race.items():
        post_time = datetime.datetime.fromisoformat(bets[0]['post_time'])
        if now < post_time + SETTLE_AFTER:
            continue

        results = jra.race_results(race_id)
        if any(f['place'] == 1 for f in results):
            if any(f['number'] is None for f in results):
                # Can't tell which horse is which -- grading now would call
                # every bet a scratch. Leave them for a page that parses.
                logger.warning(f"jra_bets: race {race_id}'s result is missing horse numbers; not settling yet")
                continue
            graded = [(bet, *_grade(bet, results)) for bet in bets]
        elif now - post_time > STALE_AFTER:
            graded = [
                (bet, 'refunded', bet['amount'],
                 f"🏇 **{bet['race_title']}** never got a result, so your "
                 f"{economy.format_coins(bet['amount'])} on **#{bet['horse_number']} {bet['horse_name']}** comes back.")
                for bet in bets
            ]
        else:
            continue

        for bet, status, returned, text in graded:
            balance = _settle(bet, status, returned)
            if balance is None:
                continue
            settlements.append(Settlement(
                user_id=int(bet['user_id']),
                channel_id=int(bet['channel_id']),
                text=f"{text}\n{economy.outcome_line(bet['amount'], returned, balance)}",
            ))
    return settlements
