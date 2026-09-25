"""Finds the picture a link's own page advertises, so an announcement can
show it inside its embed instead of re-posting the bare URL underneath
just to make Discord draw a preview card.

The bare re-post worked, but it left a duplicate link hanging under every
announcement (see _send_announcement in bot.py). Pulling the artwork in
ourselves means one link, in the embed, with the picture attached to it.
"""
import re
import time
import logging
import threading
from urllib.parse import urljoin, urlparse

import requests

logger = logging.getLogger(__name__)

# Some sites serve a different (or no) preview to an obvious bot. This is
# honest about who's asking while still looking like a browser enough to
# be served the same markup Discord's own crawler gets.
USER_AGENT = "Mozilla/5.0 (compatible; DJ-Shinx-Bot/1.0; +https://github.com/Ale0902/DJ-Shinx-main)"

FETCH_TIMEOUT = 8

# og:image lives in <head>, so there's no reason to pull down a multi-MB
# page body to find it -- some of these pages are over a megabyte.
MAX_BYTES = 256 * 1024

# /recsong can be run repeatedly and the SOTD list is small, so the same
# handful of links get looked up over and over. An hour is long enough to
# make that free and short enough that swapped-out art still refreshes.
CACHE_TTL_SECONDS = 3600
_cache: dict[str, tuple[float, str | None]] = {}
_cache_lock = threading.Lock()

_YOUTUBE_RE = re.compile(
    r'(?:youtube\.com/(?:watch\?(?:.*&)?v=|shorts/|embed/)|youtu\.be/)([\w-]{11})',
    re.IGNORECASE,
)

# Two orderings because the attributes can appear either way round, and
# both property= (Open Graph) and name= (what some sites emit instead).
_META_IMAGE_RES = [
    re.compile(
        r'<meta[^>]+(?:property|name)=["\'](?:og:image|twitter:image)(?::url)?["\'][^>]+'
        r'content=["\']([^"\']+)["\']',
        re.IGNORECASE,
    ),
    re.compile(
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+'
        r'(?:property|name)=["\'](?:og:image|twitter:image)(?::url)?["\']',
        re.IGNORECASE,
    ),
]


def _youtube_thumbnail(url: str) -> str | None:
    """YouTube's thumbnail path is derived straight from the video id, so
    this needs no request at all. hqdefault rather than maxresdefault:
    every video has one, while maxres only exists for those uploaded above
    720p and 404s silently for the rest."""
    match = _YOUTUBE_RE.search(url)
    return f"https://i.ytimg.com/vi/{match.group(1)}/hqdefault.jpg" if match else None


def _fetch_meta_image(url: str) -> str | None:
    try:
        with requests.get(
            url, headers={'User-Agent': USER_AGENT}, timeout=FETCH_TIMEOUT, stream=True,
        ) as response:
            # Anything but a clean 200 is checked explicitly rather than
            # just parsed: Spotify's 404 page still carries an og:image
            # (a generic "download the app" promo), so trusting the body
            # of an error response attaches a confidently wrong picture.
            if response.status_code != 200:
                logger.debug(f"link_preview: {url} returned {response.status_code}")
                return None
            if 'html' not in response.headers.get('Content-Type', ''):
                return None

            body = b''
            for chunk in response.iter_content(8192):
                body += chunk
                if len(body) >= MAX_BYTES:
                    break
            text = body.decode('utf-8', errors='replace')
    except Exception as e:
        logger.debug(f"link_preview: couldn't fetch {url}: {e}")
        return None

    for pattern in _META_IMAGE_RES:
        match = pattern.search(text)
        if match:
            # Some pages give a path rather than a full URL.
            image_url = urljoin(url, match.group(1).strip())
            if urlparse(image_url).scheme in ('http', 'https'):
                return image_url
    return None


def artwork_for(url: str) -> str | None:
    """The image `url`'s page advertises -- album art for a Spotify track,
    the video thumbnail for YouTube, an article's hero image -- or None if
    it hasn't got one or can't be reached. Never raises: a missing picture
    just means the announcement goes out the way it always did."""
    if not url:
        return None

    now = time.time()
    with _cache_lock:
        cached = _cache.get(url)
        if cached and now - cached[0] < CACHE_TTL_SECONDS:
            return cached[1]

    artwork = _youtube_thumbnail(url) or _fetch_meta_image(url)

    with _cache_lock:
        # Bound the cache: these keys are whatever links have gone past,
        # so without this a long-running bot accumulates them forever.
        if len(_cache) > 512:
            _cache.clear()
        _cache[url] = (now, artwork)
    return artwork
