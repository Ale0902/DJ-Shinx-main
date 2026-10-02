"""A play-money economy for each server: /work pays a random amount every
two hours, and slots, roulette and blackjack let people bet it to try to grow
it. Coins are worth nothing outside the bot and can't be bought.

Every server has its own separate economy -- balances are keyed by
(guild, user), so a leaderboard only ranks people in that server and
being rich in one server means nothing in another. Stored in a local
SQLite file, same pattern as memory_db.py and channel_config.py.

The games lean slightly in the players' favor, so a server's economy can
slowly grow rather than drain: slots pays back about 102% on average,
roulette about 102%, blackjack about 101% with sensible play. Still a
gamble -- any one bet is more likely to lose than to win big -- but
someone who keeps playing tends to come out a little ahead.
"""
import contextlib
import datetime
import math
import os
import random
import sqlite3
from dataclasses import dataclass

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, 'economy.db')

COIN = "🪙"
# /work pays a random amount in this range, inclusive.
WORK_MIN = 200
WORK_MAX = 700
# How long after a /work shift until the next one pays.
WORK_COOLDOWN_HOURS = 2

# Flavor text for /work -- unrelated to how much it pays.
WORK_JOBS = [
    "DJ'd a wedding reception",
    "walked the neighbor's Shinx",
    "worked a double at the record store",
    "fixed the Minecraft server (it was DNS)",
    "untangled every aux cord in the building",
    "sold mixtapes out of a car trunk",
    "refereed a very heated game of Mario Kart",
    "reorganized the vinyl collection by vibe",
]


def connect() -> sqlite3.Connection:
    """Opens the economy database. Shared with sportsbook.py, which keeps
    its bets in here too so they can share transactions with wallets."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS wallets ("
        "guild_id TEXT NOT NULL, "
        "user_id TEXT NOT NULL, "
        "balance INTEGER NOT NULL DEFAULT 0, "
        "last_work TEXT, "  # UTC time of the last /work, e.g. '2026-10-01T19:30:00+00:00'
        "PRIMARY KEY (guild_id, user_id))"
    )
    return conn


def format_coins(amount: int) -> str:
    return f"**{amount:,}** {COIN}"


def format_duration(delta: datetime.timedelta) -> str:
    """e.g. "5h 12m", or "12m" under an hour. Rounds up, so it never
    says "0m" while there's still time left."""
    minutes = math.ceil(delta.total_seconds() / 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m" if hours else f"{minutes}m"


def outcome_line(bet: int, returned: int, balance: int) -> str:
    """The last line of every game: what the round did to the player's
    wallet, and where it stands now."""
    net = returned - bet
    if net > 0:
        change = f"Won {format_coins(net)}"
    elif net == 0:
        change = "Broke even"
    else:
        change = f"Lost {format_coins(-net)}"
    return f"{change} · Balance: {format_coins(balance)}"


#=========================--WALLETS--========================================#

def get_balance(guild_id, user_id) -> int:
    with connect() as conn:
        row = conn.execute(
            "SELECT balance FROM wallets WHERE guild_id = ? AND user_id = ?",
            (str(guild_id), str(user_id)),
        ).fetchone()
    return row[0] if row else 0


def _work_timestamp(moment: datetime.datetime) -> str:
    # Always the same UTC format, so two of these compare correctly as
    # plain text inside SQL. A last_work saved back when /work was once a
    # day is a bare date like '2026-10-01', which sorts before any of
    # these and so reads as long enough ago.
    return moment.astimezone(datetime.timezone.utc).isoformat(timespec='seconds')


def work(guild_id, user_id) -> tuple[int, int] | None:
    """Pays between WORK_MIN and WORK_MAX coins if this user hasn't worked
    in this server in the last WORK_COOLDOWN_HOURS. Returns (amount
    earned, new balance), or None if they have. A single statement does
    both the check and the payment, so two /work calls landing at once
    can't both pay out."""
    earned = random.randint(WORK_MIN, WORK_MAX)
    now = datetime.datetime.now(datetime.timezone.utc)
    cooldown_start = now - datetime.timedelta(hours=WORK_COOLDOWN_HOURS)
    with connect() as conn:
        cursor = conn.execute(
            "INSERT INTO wallets (guild_id, user_id, balance, last_work) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (guild_id, user_id) DO UPDATE SET "
            "balance = balance + excluded.balance, last_work = excluded.last_work "
            "WHERE wallets.last_work IS NULL OR wallets.last_work <= ?",
            (str(guild_id), str(user_id), earned, _work_timestamp(now), _work_timestamp(cooldown_start)),
        )
        if cursor.rowcount == 0:
            return None
    return earned, get_balance(guild_id, user_id)


def time_until_next_work(guild_id, user_id) -> datetime.timedelta:
    """How long until this user's next /work pays -- zero if it already would."""
    with connect() as conn:
        row = conn.execute(
            "SELECT last_work FROM wallets WHERE guild_id = ? AND user_id = ?",
            (str(guild_id), str(user_id)),
        ).fetchone()
    if not row or not row[0] or 'T' not in row[0]:  # never worked, or only under the old once-a-day rule
        return datetime.timedelta(0)
    next_shift = datetime.datetime.fromisoformat(row[0]) + datetime.timedelta(hours=WORK_COOLDOWN_HOURS)
    return max(next_shift - datetime.datetime.now(datetime.timezone.utc), datetime.timedelta(0))


def _transaction(conn: sqlite3.Connection | None):
    """The caller's transaction when it passes one in, so a wallet change
    can commit or roll back together with its other writes (sportsbook.py
    records a bet in the same breath as taking its stake); otherwise a
    transaction of its own."""
    return contextlib.nullcontext(conn) if conn is not None else connect()


def take_bet(guild_id, user_id, amount: int, conn: sqlite3.Connection | None = None) -> bool:
    """Removes a bet from the user's balance. Returns False, taking
    nothing, if they can't cover it. The balance check is part of the
    UPDATE itself, so rapid-fire bets can't spend the same coins twice."""
    with _transaction(conn) as c:
        cursor = c.execute(
            "UPDATE wallets SET balance = balance - ? "
            "WHERE guild_id = ? AND user_id = ? AND balance >= ?",
            (amount, str(guild_id), str(user_id), amount),
        )
        return cursor.rowcount == 1


def pay(guild_id, user_id, amount: int, conn: sqlite3.Connection | None = None) -> int:
    """Adds coins to the user's balance and returns the new balance."""
    with _transaction(conn) as c:
        c.execute(
            "INSERT INTO wallets (guild_id, user_id, balance) VALUES (?, ?, ?) "
            "ON CONFLICT (guild_id, user_id) DO UPDATE SET balance = balance + excluded.balance",
            (str(guild_id), str(user_id), amount),
        )
        return c.execute(
            "SELECT balance FROM wallets WHERE guild_id = ? AND user_id = ?",
            (str(guild_id), str(user_id)),
        ).fetchone()[0]


def give(guild_id, from_user_id, to_user_id, amount: int) -> tuple[int, int] | None:
    """Moves coins from one user's wallet to another's in this server.
    Returns (giver's new balance, receiver's new balance), or None,
    moving nothing, if the giver can't cover it. Both sides are one
    transaction, so coins can't leave one wallet without reaching the
    other, and the same balance-checked UPDATE take_bet uses means two
    gifts at once can't spend the same coins twice."""
    with connect() as conn:
        if not take_bet(guild_id, from_user_id, amount, conn=conn):
            return None
        received = pay(guild_id, to_user_id, amount, conn=conn)
        given = conn.execute(
            "SELECT balance FROM wallets WHERE guild_id = ? AND user_id = ?",
            (str(guild_id), str(from_user_id)),
        ).fetchone()[0]
    return given, received


def leaderboard(guild_id, limit: int = 10) -> list[tuple[int, int]]:
    """Returns [(user_id, balance)] for this server's richest users,
    richest first. Anyone sitting at zero is left out."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT user_id, balance FROM wallets WHERE guild_id = ? AND balance > 0 "
            "ORDER BY balance DESC LIMIT ?",
            (str(guild_id), limit),
        ).fetchall()
    return [(int(user_id), balance) for user_id, balance in rows]


#=========================--SLOTS--==========================================#

# symbol -> (weight on each reel, three-of-a-kind payout, two-of-a-kind payout).
# Payouts are total returns as a multiple of the bet, so 1 means "your bet
# back" and 0 means it's lost. Tuned for ~102% payback, with about half of
# all spins returning at least the bet -- see the module docstring.
SLOT_SYMBOLS = {
    "🍒": (7, 5, 1),
    "🍋": (6, 7, 1),
    "🍇": (5, 10, 1),
    "🔔": (4, 25, 2),
    "⭐": (2, 60, 2),
    "7️⃣": (1, 400, 3),
}
SLOT_HIDDEN = "❔"


def spin_slots() -> tuple[list[str], int]:
    """Spins three reels. Returns the symbols and the payout multiplier."""
    symbols = list(SLOT_SYMBOLS)
    weights = [SLOT_SYMBOLS[s][0] for s in symbols]
    reels = random.choices(symbols, weights=weights, k=3)
    return reels, slots_multiplier(reels)


def slots_multiplier(reels: list[str]) -> int:
    for symbol in set(reels):
        count = reels.count(symbol)
        if count == 3:
            return SLOT_SYMBOLS[symbol][1]
        if count == 2:
            return SLOT_SYMBOLS[symbol][2]
    return 0


def slots_paytable() -> str:
    lines = ["**Payouts** (× your bet)"]
    for symbol, (_, triple, pair) in SLOT_SYMBOLS.items():
        lines.append(f"{symbol}{symbol}{symbol} {triple}×  ·  {symbol}{symbol} {pair}×")
    return "\n".join(lines)


#=========================--ROULETTE--=======================================#

# European wheel: a single green 0. At standard payouts (2x / 3x / 36x)
# that zero gives the house a 2.7% edge on every bet, so payouts here run
# 5% higher instead, tipping every bet to about 102% in the player's favor.
RED_NUMBERS = frozenset({1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36})


@dataclass(frozen=True)
class RouletteBet:
    label: str
    numbers: frozenset
    payout_pct: int  # total return as a percentage of the bet: 210 means 2.1x

    def returned(self, bet: int) -> int:
        """Coins paid back on a win, stake included. Whole percentages
        keep this in integer math -- 10 * 3.15 in floats is 31.4999..."""
        return bet * self.payout_pct // 100


def _range(low, high):
    return frozenset(range(low, high + 1))


def format_multiplier(payout_pct: int) -> str:
    return f"{payout_pct / 100:g}×"  # 210 -> "2.1×", 3800 -> "38×"


EVEN_MONEY_PCT = 210  # red/black, odd/even, low/high
DOZEN_PCT = 315
NUMBER_PCT = 3800  # a single number, including green 0


# name -> bet, for every outside bet. Aliases share one RouletteBet.
_RED = RouletteBet("Red", RED_NUMBERS, EVEN_MONEY_PCT)
_BLACK = RouletteBet("Black", _range(1, 36) - RED_NUMBERS, EVEN_MONEY_PCT)
_ODD = RouletteBet("Odd", frozenset(range(1, 37, 2)), EVEN_MONEY_PCT)
_EVEN = RouletteBet("Even", frozenset(range(2, 37, 2)), EVEN_MONEY_PCT)
_LOW = RouletteBet("Low (1-18)", _range(1, 18), EVEN_MONEY_PCT)
_HIGH = RouletteBet("High (19-36)", _range(19, 36), EVEN_MONEY_PCT)
_DOZEN_1 = RouletteBet("1st dozen (1-12)", _range(1, 12), DOZEN_PCT)
_DOZEN_2 = RouletteBet("2nd dozen (13-24)", _range(13, 24), DOZEN_PCT)
_DOZEN_3 = RouletteBet("3rd dozen (25-36)", _range(25, 36), DOZEN_PCT)
ROULETTE_BETS = {
    'red': _RED, 'black': _BLACK,
    'odd': _ODD, 'even': _EVEN,
    'low': _LOW, '1-18': _LOW,
    'high': _HIGH, '19-36': _HIGH,
    '1-12': _DOZEN_1, '13-24': _DOZEN_2, '25-36': _DOZEN_3,
    'green': RouletteBet("Green (0)", frozenset({0}), NUMBER_PCT),
}

# What /roulette's autocomplete offers, in this order: (shown name, value).
# The "pays N×" part is added from the bet itself, so it can't go stale.
ROULETTE_SUGGESTIONS = [
    ("Red", 'red'),
    ("Black", 'black'),
    ("Odd", 'odd'),
    ("Even", 'even'),
    ("Low, 1-18", '1-18'),
    ("High, 19-36", '19-36'),
    ("1st dozen, 1-12", '1-12'),
    ("2nd dozen, 13-24", '13-24'),
    ("3rd dozen, 25-36", '25-36'),
    ("Green 0", 'green'),
]


def parse_roulette_bet(text: str) -> RouletteBet | None:
    """Turns what the user typed ("red", "1-12", "17", ...) into a bet,
    or None if it isn't one."""
    text = text.strip().lower()
    if text in ROULETTE_BETS:
        return ROULETTE_BETS[text]
    if text.isdecimal() and 0 <= int(text) <= 36:
        number = int(text)
        return RouletteBet(f"Number {number}", frozenset({number}), NUMBER_PCT)
    return None


def roulette_suggestions(current: str) -> list[tuple[str, str]]:
    current = current.strip().lower()
    matches = [
        (f"{name} — pays {format_multiplier(ROULETTE_BETS[value].payout_pct)}", value)
        for name, value in ROULETTE_SUGGESTIONS
        if current in name.lower() or current in value
    ]
    if current.isdecimal() and 0 <= int(current) <= 36:
        # Typing "1" could mean the number or the start of "1-12"/"1-18".
        matches.insert(0, (f"Number {int(current)} — pays {format_multiplier(NUMBER_PCT)}", current))
    return matches


def spin_roulette() -> int:
    return random.randint(0, 36)


def roulette_pocket(number: int) -> str:
    """How a winning number is shown, e.g. "🔴 32"."""
    if number == 0:
        color = "🟢"
    elif number in RED_NUMBERS:
        color = "🔴"
    else:
        color = "⚫"
    return f"{color} {number}"


#=========================--BLACKJACK--======================================#

SUITS = "♠♥♦♣"
RANKS = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]


def hand_value(cards: list[str]) -> tuple[int, bool]:
    """Returns (best total, whether it's soft) -- soft meaning an ace is
    still counting as 11 and could drop to 1 instead of busting."""
    total = 0
    aces = 0
    for card in cards:
        rank = card[:-1]
        if rank == "A":
            aces += 1
            total += 1
        elif rank in ("J", "Q", "K"):
            total += 10
        else:
            total += int(rank)
    if aces and total + 10 <= 21:
        return total + 10, True
    return total, False


class BlackjackGame:
    """One hand of blackjack against the dealer, dealt from a freshly
    shuffled deck. Dealer stands on every 17, blackjack pays 2:1 (instead
    of the usual 3:2 -- that's what tips it into the player's favor), and
    the player can double down on their first two cards. No splitting.

    Holds no money itself: the caller takes the bet before dealing, takes
    a second bet before double_down(), and pays out payout() once
    finished is True."""

    def __init__(self, bet: int):
        self.bet = bet
        self.deck = [rank + suit for suit in SUITS for rank in RANKS]
        random.shuffle(self.deck)
        self.player = [self.deck.pop(), self.deck.pop()]
        self.dealer = [self.deck.pop(), self.deck.pop()]
        self.outcome: str | None = None  # set once the hand is over

        # A natural on either side ends the hand on the spot, the same as
        # a dealer peeking for blackjack at a real table.
        player_natural = hand_value(self.player)[0] == 21
        dealer_natural = hand_value(self.dealer)[0] == 21
        if player_natural and dealer_natural:
            self.outcome = 'push'
        elif player_natural:
            self.outcome = 'blackjack'
        elif dealer_natural:
            self.outcome = 'dealer_blackjack'

    @property
    def finished(self) -> bool:
        return self.outcome is not None

    @property
    def can_double(self) -> bool:
        return not self.finished and len(self.player) == 2

    def hit(self) -> None:
        self.player.append(self.deck.pop())
        total = hand_value(self.player)[0]
        if total > 21:
            self.outcome = 'bust'
        elif total == 21:
            self.stand()  # nothing left to gain from hitting

    def double_down(self) -> None:
        self.bet *= 2
        self.hit()
        if not self.finished:
            self.stand()

    def stand(self) -> None:
        while hand_value(self.dealer)[0] < 17:
            self.dealer.append(self.deck.pop())
        player_total = hand_value(self.player)[0]
        dealer_total = hand_value(self.dealer)[0]
        if dealer_total > 21:
            self.outcome = 'dealer_bust'
        elif player_total > dealer_total:
            self.outcome = 'win'
        elif player_total == dealer_total:
            self.outcome = 'push'
        else:
            self.outcome = 'lose'

    def payout(self) -> int:
        """Total coins returned to the player, including their stake."""
        if self.outcome == 'blackjack':
            return self.bet * 3
        if self.outcome in ('win', 'dealer_bust'):
            return self.bet * 2
        if self.outcome == 'push':
            return self.bet
        return 0

    def render(self) -> str:
        """The table as text. The dealer's second card stays face down
        until the hand is over."""
        if self.finished:
            dealer_cards = " ".join(self.dealer)
            dealer_total = str(hand_value(self.dealer)[0])
        else:
            dealer_cards = f"{self.dealer[0]} 🂠"
            dealer_total = "?"
        player_total, soft = hand_value(self.player)
        soft_note = " (soft)" if soft and player_total < 21 else ""
        return (
            f"**Dealer** — {dealer_total}\n{dealer_cards}\n\n"
            f"**You** — {player_total}{soft_note}\n{' '.join(self.player)}"
        )

    def result_line(self) -> str:
        return {
            'blackjack': "🃏 **Blackjack!** Pays 2:1.",
            'win': "✅ **You win!**",
            'dealer_bust': "💥 **Dealer busts — you win!**",
            'push': "🤝 **Push.** Your bet comes back.",
            'lose': "❌ **Dealer wins.**",
            'bust': "💥 **Bust!** You went over 21.",
            'dealer_blackjack': "❌ **Dealer has blackjack.**",
        }[self.outcome]
