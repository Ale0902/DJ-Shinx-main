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

Tracks the *currently active* set of matching offers (not a permanently
growing "seen" list) -- one that ends and later starts again on the same
game gets announced again correctly, since it drops out of the active set
in between.
"""
import os
import json
import requests

BASE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE, 'steam_sales_state.json')

FEATURED_URL = 'https://store.steampowered.com/api/featuredcategories'
USER_AGENT = "DJ-Shinx-Bot/1.0 (+https://github.com/Ale0902/DJ-Shinx-main)"

# Only announce a "gone free" game if it normally costs at least this much
# (in cents) -- otherwise a $1 indie title going free would be as noisy as
# a real giveaway of something that's usually $20+.
FREE_PROMO_MIN_PRICE = 999


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

    free_promos = [
        s for s in specials
        if s.get('discount_percent') == 100 and s.get('original_price', 0) >= FREE_PROMO_MIN_PRICE
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


def check_steam_sales() -> list[str]:
    """Returns announcement strings for popular titles newly on sale, and
    normally-paid titles newly free, since the last check -- stays quiet
    on the very first check (seeds a baseline of whatever's already
    active) so it doesn't dump every currently-active offer as if it all
    just started."""
    try:
        popular_discounts, free_promos = _fetch_offers()
    except Exception:
        return []

    current_offers = {sale['id']: sale for sale in popular_discounts + free_promos}

    state = _load_state()
    is_first_ever = 'active_sale_ids' not in state
    previous_ids = set(state.get('active_sale_ids', []))

    state['active_sale_ids'] = list(current_offers.keys())
    _save_state(state)

    if is_first_ever:
        return []

    messages = []
    for sale_id, sale in current_offers.items():
        if sale_id in previous_ids:
            continue
        if sale.get('discount_percent') == 100:
            messages.append(_format_free_promo(sale))
        else:
            messages.append(_format_sale(sale))
    return messages
