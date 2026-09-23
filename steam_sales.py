"""Checks for Steam sales on popular game titles, backing the /setchannel
"steam_sales" feature.

Uses Steam's public featuredcategories API (no key needed): "popular" is
deliberately Steam's own top_sellers list intersected with the specials
list, rather than a hand-maintained title list or a big-discount
heuristic -- so this can't misfire announcing some obscure game just
because it happens to be steeply discounted.

Tracks the *currently active* set of matching sales (not a permanently
growing "seen" list) -- a sale that ends and later starts again on the
same game gets announced again correctly, since it drops out of the
active set in between.
"""
import os
import json
import requests

BASE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE, 'steam_sales_state.json')

FEATURED_URL = 'https://store.steampowered.com/api/featuredcategories'
USER_AGENT = "DJ-Shinx-Bot/1.0 (+https://github.com/Ale0902/DJ-Shinx-main)"


def _load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r') as f:
            return json.load(f)
    return {}


def _save_state(state):
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f)


def _fetch_popular_sales():
    """Returns Steam's currently-featured specials that are also in its
    current top_sellers list, i.e. popular games actually on sale right
    now."""
    response = requests.get(
        FEATURED_URL, params={'cc': 'us', 'l': 'english'}, headers={'User-Agent': USER_AGENT}, timeout=10
    )
    response.raise_for_status()
    data = response.json()

    top_seller_ids = {item['id'] for item in data.get('top_sellers', {}).get('items', [])}
    specials = data.get('specials', {}).get('items', [])
    return [s for s in specials if s.get('discounted') and s['id'] in top_seller_ids]


def _format_sale(sale) -> str:
    name = sale['name']
    discount = sale['discount_percent']
    price = sale['final_price'] / 100
    original = sale['original_price'] / 100
    url = f"https://store.steampowered.com/app/{sale['id']}/"
    return f"🛒 **Steam Sale: {name}**\n{discount}% off — ${price:.2f} (was ${original:.2f})\n{url}"


def check_steam_sales() -> list[str]:
    """Returns announcement strings for popular titles that just went on
    sale since the last check -- stays quiet on the very first check
    (seeds a baseline of whatever's already on sale, same as the
    Direct/State of Play checkers) so it doesn't dump every currently-
    active sale as if they all just started."""
    try:
        popular_sales = _fetch_popular_sales()
    except Exception:
        return []

    state = _load_state()
    is_first_ever = 'active_sale_ids' not in state
    previous_ids = set(state.get('active_sale_ids', []))

    state['active_sale_ids'] = [sale['id'] for sale in popular_sales]
    _save_state(state)

    if is_first_ever:
        return []

    return [_format_sale(sale) for sale in popular_sales if sale['id'] not in previous_ids]
