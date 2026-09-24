"""Checks for Steam sales on popular game titles, and separately for
normally-paid games that have gone temporarily free, backing the
/setchannel "steam_sales" feature.

Popular discounts come from Steam's store search, asked for the titles
that are both currently on special and ranked by top sellers, so
"popular" is Steam's own ranking rather than a hand-maintained title list
or a big-discount heuristic that would misfire on some obscure game.

That replaced featuredcategories, which is what this used to read for
them and which quietly made the whole feature near-useless: it returns
ten specials and ten top sellers, and "popular discount" was their
intersection -- two small rotating windows that mostly don't overlap, so
the answer was usually nothing at all. An entire Persona series sale
(Persona 3 Reload at 70% off ranked 14th among top-selling specials,
Persona 5 Royal 30th, Metaphor: ReFantazio 28th) went unannounced that
way, while the search ranking had 1757 discounted games to draw from.

A free promo (100% off) can't use that same top_sellers filter -- it
generates zero revenue, so it never appears in a revenue-ranked
top-sellers list no matter how popular the giveaway is. Instead it's
filtered to specials Steam is already front-page-featuring that normally
cost real money above FREE_PROMO_MIN_PRICE (excluding always-free F2P
titles, which have no real "discount" to begin with, and trivially cheap
indie games going free, which would just be noise).

Dedup is by "how long has this offer been gone", not "was it in the last
poll". Steam's featuredcategories only ever returns ten specials and ten
top sellers -- a small rotating window over thousands of live offers -- so
a game routinely drops out of the response and reappears a poll or a day
later without its sale ever having ended. A plain snapshot diff reads that
reappearance as a brand new sale and announces it again, which is exactly
what it was doing. Instead each matching offer's last-seen time is kept,
and it only becomes announceable again once it's been absent for
REANNOUNCE_AFTER_DAYS -- long enough that rotation gaps stay quiet, short
enough that a genuinely separate sale months later still gets announced.
"""
import os
import re
import json
import time
import html
import requests

BASE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE, 'steam_sales_state.json')

FEATURED_URL = 'https://store.steampowered.com/api/featuredcategories'
SEARCH_URL = 'https://store.steampowered.com/search/results/'
USER_AGENT = "DJ-Shinx-Bot/1.0 (+https://github.com/Ale0902/DJ-Shinx-main)"

# How far down Steam's "top sellers that are currently on special"
# ranking still counts as a popular title. The tail is long (1757
# discounted games the day this was written) and almost all of it is
# stuff nobody asked about.
POPULAR_RANK_DEPTH = 100

# A token 10-15% cut isn't news. Every Persona title in the sale that
# prompted this was 50-70% off.
MIN_DISCOUNT_PERCENT = 50

# A seasonal sale flips dozens of popular titles on at once -- 55 of the
# top 100 were at or past MIN_DISCOUNT_PERCENT the day this was written.
# Post the steepest few on their own (so each still gets its store-page
# preview) and roll the remainder into a single line rather than firing
# fifty messages into the channel.
MAX_INDIVIDUAL_ANNOUNCEMENTS = 5

# Only announce a "gone free" game if it normally costs at least this much
# (in cents) -- otherwise a $1 indie title going free would be as noisy as
# a real giveaway of something that's usually $20+.
FREE_PROMO_MIN_PRICE = 999

# How long a matching offer has to be absent from Steam's featured window
# before it counts as a new sale rather than the same one rotating back
# into view. Rotation gaps run hours to a few days; distinct sales on the
# same game are typically a seasonal event apart, so this sits well clear
# of the first without suppressing the second.
REANNOUNCE_AFTER_DAYS = 14


def _load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r') as f:
            return json.load(f)
    return {}


def _save_state(state):
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f)


# Steam's search endpoint answers with a blob of rendered HTML rather
# than structured items, but every field needed is in a machine-readable
# attribute rather than in the prose, so this reads those instead of
# trying to interpret the markup's shape. A row that's missing any of
# them is skipped rather than guessed at.
_ROW_RE = re.compile(r'data-ds-appid="(\d+)"(.*?)(?=data-ds-appid="|\Z)', re.S)
_TITLE_RE = re.compile(r'<span class="title">(.*?)</span>', re.S)
_DISCOUNT_RE = re.compile(r'data-discount="(\d+)"')
_FINAL_PRICE_RE = re.compile(r'data-price-final="(\d+)"')
_ORIGINAL_PRICE_RE = re.compile(r'discount_original_price">([^<]+)<')


def _price_to_cents(text: str) -> int | None:
    """"$59.99" -> 5999. Returns None for anything that isn't a plain
    amount ("Free", a range, an empty string), so the caller can drop the
    row instead of announcing a nonsense price."""
    digits = re.sub(r'[^\d.]', '', text or '')
    if not digits or digits.count('.') > 1:
        return None
    try:
        return round(float(digits) * 100)
    except ValueError:
        return None


def _parse_search_rows(results_html: str) -> list[dict]:
    """Normalizes search rows into the same shape featuredcategories uses
    for its items, so both sources feed the rest of this module
    unchanged."""
    offers = []
    for rank, match in enumerate(_ROW_RE.finditer(results_html)):
        app_id, body = match.group(1), match.group(2)

        title = _TITLE_RE.search(body)
        discount = _DISCOUNT_RE.search(body)
        final = _FINAL_PRICE_RE.search(body)
        original = _ORIGINAL_PRICE_RE.search(body)
        if not (title and discount and final and original):
            continue

        original_cents = _price_to_cents(original.group(1))
        if original_cents is None:
            continue

        offers.append({
            'id': int(app_id),
            'name': html.unescape(title.group(1)).strip(),
            'discount_percent': int(discount.group(1)),
            'final_price': int(final.group(1)),
            'original_price': original_cents,
            # Position in the response is Steam's own top-sellers rank,
            # which is the only popularity signal available here -- kept
            # so the most notable titles lead the announcements.
            'rank': rank,
        })
    return offers


def _fetch_popular_discounts() -> list[dict]:
    """The steepest discounts among Steam's top-selling games that are
    currently on special. category1=998 restricts this to games, so a
    season pass or a soundtrack going cheap doesn't read as a big title
    going on sale."""
    response = requests.get(
        SEARCH_URL,
        params={
            'query': '', 'start': 0, 'count': POPULAR_RANK_DEPTH,
            'filter': 'topsellers', 'specials': 1, 'category1': 998,
            'cc': 'us', 'l': 'english', 'json': 1, 'infinite': 1,
        },
        headers={'User-Agent': USER_AGENT},
        timeout=15,
    )
    response.raise_for_status()

    rows = _parse_search_rows(response.json().get('results_html', ''))
    return [row for row in rows if row['discount_percent'] >= MIN_DISCOUNT_PERCENT]


def _fetch_free_promos() -> list[dict]:
    """Normally-paid games gone temporarily free. Still read off
    featuredcategories rather than the search ranking above: a giveaway
    earns no revenue, so it never climbs a top-sellers list no matter how
    popular it is, but Steam does put a real one on the front page."""
    response = requests.get(
        FEATURED_URL, params={'cc': 'us', 'l': 'english'}, headers={'User-Agent': USER_AGENT}, timeout=10
    )
    response.raise_for_status()

    specials = response.json().get('specials', {}).get('items', [])
    # `or 0` rather than a .get default: Steam sends original_price as an
    # explicit null for some items, and `None >= FREE_PROMO_MIN_PRICE`
    # raises a TypeError that the caller swallows whole -- silently
    # switching the entire feature off until the offending item rotated
    # back out of the window.
    return [
        s for s in specials
        if s.get('discount_percent') == 100 and (s.get('original_price') or 0) >= FREE_PROMO_MIN_PRICE
    ]


def _fetch_offers():
    """Returns (popular_discounts, free_promos). The two come from
    different endpoints (see each helper) and are fetched independently,
    so one being down or changing shape doesn't take the other with it."""
    popular_discounts, free_promos = [], []

    try:
        free_promos = _fetch_free_promos()
    except Exception:
        pass
    try:
        popular_discounts = _fetch_popular_discounts()
    except Exception:
        pass

    free_ids = {s['id'] for s in free_promos}
    popular_discounts = [s for s in popular_discounts if s['id'] not in free_ids]

    return popular_discounts, free_promos


def _store_url(app_id) -> str:
    return f"https://store.steampowered.com/app/{app_id}/"


def _header_image(app_id) -> str:
    """Every Steam app serves its store banner at this fixed path, so the
    announcement gets artwork without a second request to look one up."""
    return f"https://cdn.cloudflare.steamstatic.com/steam/apps/{app_id}/header.jpg"


def _format_sale(sale) -> str:
    name = sale['name']
    discount = sale['discount_percent']
    price = sale['final_price'] / 100
    original = sale['original_price'] / 100
    return (
        f"🛒 **Steam Sale: {name}**\n"
        f"{discount}% off — ${price:.2f} (was ${original:.2f})\n"
        f"{_store_url(sale['id'])}\n"
        f"IMAGE: {_header_image(sale['id'])}"
    )


def _format_free_promo(sale) -> str:
    name = sale['name']
    original = sale['original_price'] / 100
    return (
        f"🎉 **FREE ON STEAM: {name}**\n"
        f"Normally ${original:.2f} — currently free to claim!\n"
        f"{_store_url(sale['id'])}\n"
        f"IMAGE: {_header_image(sale['id'])}"
    )


def _load_last_seen(state, now: float) -> tuple[dict[str, float], bool]:
    """Returns ({app_id: last_seen_epoch}, is_first_ever). Migrates a state
    file written by the older snapshot-diff version by treating whatever
    was active then as seen right now, so upgrading doesn't re-announce
    every offer that happens to be live at the time."""
    if 'last_seen' in state:
        return {str(k): float(v) for k, v in state['last_seen'].items()}, False
    if 'active_sale_ids' in state:
        return {str(sale_id): now for sale_id in state['active_sale_ids']}, False
    return {}, True


def check_steam_sales() -> list[str]:
    """Returns announcement strings for popular titles newly on sale, and
    normally-paid titles newly free -- "newly" meaning not seen in Steam's
    featured window for at least REANNOUNCE_AFTER_DAYS, not merely absent
    from the previous poll, since that window rotates (see module
    docstring). Stays quiet on the very first check, seeding a baseline of
    whatever's already active so it doesn't dump every live offer at once."""
    try:
        popular_discounts, free_promos = _fetch_offers()
    except Exception:
        return []

    current_offers = {str(sale['id']): sale for sale in popular_discounts + free_promos}

    now = time.time()
    state = _load_state()
    last_seen, is_first_ever = _load_last_seen(state, now)

    # An offer gone longer than the window is treated as finished: forget
    # it, so if it comes back it reads as a new sale. Also keeps the state
    # file from growing without bound. Note this deliberately drops stale
    # entries that are in current_offers too -- that case IS the relaunch
    # being detected, and exempting them would make a returning offer
    # permanently unannounceable. Anything genuinely running the whole
    # time never goes stale, since its timestamp is refreshed every poll.
    stale_cutoff = now - REANNOUNCE_AFTER_DAYS * 86400
    last_seen = {
        app_id: seen for app_id, seen in last_seen.items() if seen >= stale_cutoff
    }

    fresh = []
    for app_id, sale in current_offers.items():
        # Anything still in last_seen is either currently running or was
        # running recently enough to be the same sale rotating back in.
        if not is_first_ever and app_id not in last_seen:
            fresh.append(sale)
        last_seen[app_id] = now

    state['last_seen'] = last_seen
    state.pop('active_sale_ids', None)  # superseded; don't leave it to be re-read
    _save_state(state)

    return _build_messages(fresh)


def _build_messages(fresh: list[dict]) -> list[str]:
    """Announcement text for newly-found offers. A free giveaway always
    gets its own message -- there's rarely more than one and it's the most
    interesting thing this posts. Discounts are capped: the day a seasonal
    sale opens, dozens of popular titles turn over at once, and fifty
    consecutive messages is worse than useless. The steepest few get a
    message each and the rest one summary line."""
    free = [s for s in fresh if s.get('discount_percent') == 100]
    # By top-sellers rank, not by discount. Everything here already
    # cleared MIN_DISCOUNT_PERCENT, so they're all big cuts; what
    # separates them is whether anyone's heard of the game. Sorting by
    # percentage instead put a 90%-off decade-old Ubisoft back-catalogue
    # title above a 70%-off Persona 3 Reload, which is backwards.
    discounts = sorted(
        (s for s in fresh if s.get('discount_percent') != 100),
        key=lambda s: s.get('rank', len(fresh)),
    )

    messages = [_format_free_promo(sale) for sale in free]
    messages += [_format_sale(sale) for sale in discounts[:MAX_INDIVIDUAL_ANNOUNCEMENTS]]

    overflow = discounts[MAX_INDIVIDUAL_ANNOUNCEMENTS:]
    if overflow:
        listed = ", ".join(f"{s['name']} ({s['discount_percent']}%)" for s in overflow[:12])
        more = f" and {len(overflow) - 12} more" if len(overflow) > 12 else ""
        messages.append(f"🛒 **Also on sale:** {listed}{more}")

    return messages
