"""Checks for Steam sales on popular game titles, and separately for
normally-paid games that have gone temporarily free, backing the
/setchannel "steam_sales" feature.

Uses Steam's public featuredcategories API (no key needed). "Popular" for
a regular discount is Steam's own top_sellers list intersected with the
specials list, rather than a hand-maintained title list or a big-discount
heuristic -- so this can't misfire announcing some obscure game just
because it happens to be steeply discounted.

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
import json
import time
import requests

BASE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE, 'steam_sales_state.json')

FEATURED_URL = 'https://store.steampowered.com/api/featuredcategories'
USER_AGENT = "DJ-Shinx-Bot/1.0 (+https://github.com/Ale0902/DJ-Shinx-main)"

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


def _fetch_offers():
    """Returns (popular_discounts, free_promos) from Steam's currently
    featured specials."""
    response = requests.get(
        FEATURED_URL, params={'cc': 'us', 'l': 'english'}, headers={'User-Agent': USER_AGENT}, timeout=10
    )
    response.raise_for_status()
    data = response.json()

    specials = data.get('specials', {}).get('items', [])
    top_seller_ids = {item['id'] for item in data.get('top_sellers', {}).get('items', [])}

    # `or 0` rather than a .get default: Steam sends original_price as an
    # explicit null for some items, and `None >= FREE_PROMO_MIN_PRICE`
    # raises a TypeError that _fetch_offers' caller swallows whole --
    # silently switching the entire feature off until the offending item
    # rotated back out of the window.
    free_promos = [
        s for s in specials
        if s.get('discount_percent') == 100 and (s.get('original_price') or 0) >= FREE_PROMO_MIN_PRICE
    ]
    free_ids = {s['id'] for s in free_promos}
    popular_discounts = [
        s for s in specials
        if s.get('discounted') and s['id'] in top_seller_ids and s['id'] not in free_ids
    ]

    return popular_discounts, free_promos


def _format_sale(sale) -> str:
    name = sale['name']
    discount = sale['discount_percent']
    price = sale['final_price'] / 100
    original = sale['original_price'] / 100
    url = f"https://store.steampowered.com/app/{sale['id']}/"
    return f"🛒 **Steam Sale: {name}**\n{discount}% off — ${price:.2f} (was ${original:.2f})\n{url}"


def _format_free_promo(sale) -> str:
    name = sale['name']
    original = sale['original_price'] / 100
    url = f"https://store.steampowered.com/app/{sale['id']}/"
    return f"🎉 **FREE ON STEAM: {name}**\nNormally ${original:.2f} — currently free to claim!\n{url}"


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

    messages = []
    for app_id, sale in current_offers.items():
        # Anything still in last_seen is either currently running or was
        # running recently enough to be the same sale rotating back in.
        if not is_first_ever and app_id not in last_seen:
            if sale.get('discount_percent') == 100:
                messages.append(_format_free_promo(sale))
            else:
                messages.append(_format_sale(sale))
        last_seen[app_id] = now

    state['last_seen'] = last_seen
    state.pop('active_sale_ids', None)  # superseded; don't leave it to be re-read
    _save_state(state)

    return messages
