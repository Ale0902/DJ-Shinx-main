"""A play-money economy for each server: /work pays a flat amount once a
day, and slots, roulette and blackjack let people bet it to try to grow
it. Coins are worth nothing outside the bot and can't be bought.

Every server has its own separate economy -- balances are keyed by
(guild, user), so a leaderboard only ranks people in that server and
being rich in one server means nothing in another. Stored in a local
SQLite file, same pattern as memory_db.py and channel_config.py.

The games keep a modest house edge, like the real thing: slots pays back
about 98% on average, roulette (single zero) 97.3%, blackjack about 99.3%
with decent play. Gambling is how someone gets lucky and pulls ahead, not
a reliable way to beat /work.
"""
import datetime
import math
import os
import random
import sqlite3
from dataclasses import dataclass
from zoneinfo import ZoneInfo

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, 'economy.db')

EASTERN = ZoneInfo("America/New_York")

COIN = "🪙"
WORK_PAYOUT = 500

# Flavor text for /work -- the payout is always WORK_PAYOUT regardless.
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


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS wallets ("
        "guild_id TEXT NOT NULL, "
        "user_id TEXT NOT NULL, "
        "balance INTEGER NOT NULL DEFAULT 0, "
        "last_work TEXT, "  # Eastern date of the last /work, e.g. '2026-10-01'
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
    with _connect() as conn:
        row = conn.execute(
            "SELECT balance FROM wallets WHERE guild_id = ? AND user_id = ?",
            (str(guild_id), str(user_id)),
        ).fetchone()
    return row[0] if row else 0


def work(guild_id, user_id) -> int | None:
    """Pays WORK_PAYOUT if this user hasn't worked yet today (Eastern
    time) in this server. Returns the new balance, or None if they
    already have. A single statement does both the check and the
    payment, so two /work calls landing at once can't both pay out."""
    today = datetime.datetime.now(EASTERN).date().isoformat()
    with _connect() as conn:
        cursor = conn.execute(
            "INSERT INTO wallets (guild_id, user_id, balance, last_work) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (guild_id, user_id) DO UPDATE SET "
            "balance = balance + excluded.balance, last_work = excluded.last_work "
            "WHERE wallets.last_work IS NULL OR wallets.last_work != excluded.last_work",
            (str(guild_id), str(user_id), WORK_PAYOUT, today),
        )
        if cursor.rowcount == 0:
            return None
    return get_balance(guild_id, user_id)


def time_until_work_resets() -> datetime.timedelta:
    """How long until /work can be used again -- it resets at midnight
    Eastern, the same clock the rest of the bot's schedules run on."""
    now = datetime.datetime.now(EASTERN)
    tomorrow = datetime.datetime.combine(now.date() + datetime.timedelta(days=1), datetime.time(), EASTERN)
    return tomorrow - now


def take_bet(guild_id, user_id, amount: int) -> bool:
    """Removes a bet from the user's balance. Returns False, taking
    nothing, if they can't cover it. The balance check is part of the
    UPDATE itself, so rapid-fire bets can't spend the same coins twice."""
    with _connect() as conn:
        cursor = conn.execute(
            "UPDATE wallets SET balance = balance - ? "
            "WHERE guild_id = ? AND user_id = ? AND balance >= ?",
            (amount, str(guild_id), str(user_id), amount),
        )
        return cursor.rowcount == 1


def pay(guild_id, user_id, amount: int) -> int:
    """Adds coins to the user's balance and returns the new balance."""
    with _connect() as conn:
        conn.execute(
            "INSERT INTO wallets (guild_id, user_id, balance) VALUES (?, ?, ?) "
            "ON CONFLICT (guild_id, user_id) DO UPDATE SET balance = balance + excluded.balance",
            (str(guild_id), str(user_id), amount),
        )
    return get_balance(guild_id, user_id)


def leaderboard(guild_id, limit: int = 10) -> list[tuple[int, int]]:
    """Returns [(user_id, balance)] for this server's richest users,
    richest first. Anyone sitting at zero is left out."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT user_id, balance FROM wallets WHERE guild_id = ? AND balance > 0 "
            "ORDER BY balance DESC LIMIT ?",
            (str(guild_id), limit),
        ).fetchall()
    return [(int(user_id), balance) for user_id, balance in rows]


#=========================--SLOTS--==========================================#

# symbol -> (weight on each reel, three-of-a-kind payout, two-of-a-kind payout).
# Payouts are total returns as a multiple of the bet, so 1 means "your bet
# back" and 0 means it's lost. Tuned for ~98% payback, with about half of
# all spins returning at least the bet -- see the module docstring.
SLOT_SYMBOLS = {
    "🍒": (7, 4, 1),
    "🍋": (6, 6, 1),
    "🍇": (5, 10, 1),
    "🔔": (4, 25, 2),
    "⭐": (2, 60, 2),
    "7️⃣": (1, 300, 3),
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

# European wheel: a single green 0, so the house edge is 1/37 on every bet.
RED_NUMBERS = frozenset({1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36})


@dataclass(frozen=True)
class RouletteBet:
    label: str
    numbers: frozenset
    multiplier: int  # total return as a multiple of the bet


def _range(low, high):
    return frozenset(range(low, high + 1))


# name -> bet, for every outside bet. Aliases share one RouletteBet.
_RED = RouletteBet("Red", RED_NUMBERS, 2)
_BLACK = RouletteBet("Black", _range(1, 36) - RED_NUMBERS, 2)
_ODD = RouletteBet("Odd", frozenset(range(1, 37, 2)), 2)
_EVEN = RouletteBet("Even", frozenset(range(2, 37, 2)), 2)
_LOW = RouletteBet("Low (1-18)", _range(1, 18), 2)
_HIGH = RouletteBet("High (19-36)", _range(19, 36), 2)
_DOZEN_1 = RouletteBet("1st dozen (1-12)", _range(1, 12), 3)
_DOZEN_2 = RouletteBet("2nd dozen (13-24)", _range(13, 24), 3)
_DOZEN_3 = RouletteBet("3rd dozen (25-36)", _range(25, 36), 3)
ROULETTE_BETS = {
    'red': _RED, 'black': _BLACK,
    'odd': _ODD, 'even': _EVEN,
    'low': _LOW, '1-18': _LOW,
    'high': _HIGH, '19-36': _HIGH,
    '1-12': _DOZEN_1, '13-24': _DOZEN_2, '25-36': _DOZEN_3,
    'green': RouletteBet("Green (0)", frozenset({0}), 36),
}

# What /roulette's autocomplete offers, in this order: (shown text, value).
ROULETTE_SUGGESTIONS = [
    ("Red — pays 2×", 'red'),
    ("Black — pays 2×", 'black'),
    ("Odd — pays 2×", 'odd'),
    ("Even — pays 2×", 'even'),
    ("Low, 1-18 — pays 2×", '1-18'),
    ("High, 19-36 — pays 2×", '19-36'),
    ("1st dozen, 1-12 — pays 3×", '1-12'),
    ("2nd dozen, 13-24 — pays 3×", '13-24'),
    ("3rd dozen, 25-36 — pays 3×", '25-36'),
    ("Green 0 — pays 36×", 'green'),
]


def parse_roulette_bet(text: str) -> RouletteBet | None:
    """Turns what the user typed ("red", "1-12", "17", ...) into a bet,
    or None if it isn't one."""
    text = text.strip().lower()
    if text in ROULETTE_BETS:
        return ROULETTE_BETS[text]
    if text.isdecimal() and 0 <= int(text) <= 36:
        number = int(text)
        return RouletteBet(f"Number {number}", frozenset({number}), 36)
    return None


def roulette_suggestions(current: str) -> list[tuple[str, str]]:
    current = current.strip().lower()
    # Matched against the bet's name only -- not its payout, or typing "36" offers green.
    matches = [
        (name, value) for name, value in ROULETTE_SUGGESTIONS
        if current in name.split(" — ")[0].lower() or current in value
    ]
    if current.isdecimal() and 0 <= int(current) <= 36:
        # Typing "1" could mean the number or the start of "1-12"/"1-18".
        matches.insert(0, (f"Number {int(current)} — pays 36×", current))
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
    shuffled deck. Dealer stands on every 17, blackjack pays 3:2, and the
    player can double down on their first two cards. No splitting.

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
            return self.bet + self.bet * 3 // 2
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
            'blackjack': "🃏 **Blackjack!** Pays 3:2.",
            'win': "✅ **You win!**",
            'dealer_bust': "💥 **Dealer busts — you win!**",
            'push': "🤝 **Push.** Your bet comes back.",
            'lose': "❌ **Dealer wins.**",
            'bust': "💥 **Bust!** You went over 21.",
            'dealer_blackjack': "❌ **Dealer has blackjack.**",
        }[self.outcome]
