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
# handful of links get looked up over and over. A found picture is kept
# for an hour. A miss is kept for only a few minutes: most misses are a
# timeout or a blip, and caching one for the full hour would pin the
# announcement without its artwork long after the site had recovered.
CACHE_TTL_SECONDS = 3600
MISS_CACHE_TTL_SECONDS = 300
_cache: dict[str, tuple[float, float, str | None]] = {}  # url -> (stored_at, ttl, artwork)
_cache_lock = threading.Lock()

_YOUTUBE_RE = re.compile(
    r'(?:youtube\.com/(?:watch\?(?:.*&)?v=|shorts/|embed/)|youtu\.be/)([\w-]{11})',
    re.IGNORECASE,
)

# Localised links ("/intl-de/track/...") point at the same item, so the
# prefix is skipped and the lookup is made against the plain URL.
_SPOTIFY_RE = re.compile(
    r'open\.spotify\.com/(?:intl-[a-z-]+/)?(track|album|playlist|episode|show|artist)/([A-Za-z0-9]+)',
    re.IGNORECASE,
)

# Spotify serves every cover at a fixed set of sizes, distinguished only by
# the prefix on the image id. oEmbed hands back the 300px one; the artwork
# is shown at the embed's full width, where 300px renders visibly soft, so
# it's swapped for the 640px variant of the same image.
_SPOTIFY_300PX = 'ab67616d00001e02'
_SPOTIFY_640PX = 'ab67616d0000b273'
_SPOTIFY_IMAGE_ID_RE = re.compile(r'/image/([0-9a-f]{40})\b')

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


# Longest Retry-After worth honouring inline. A /recsong caller is sat
# waiting on this, so a longer back-off is better skipped than slept on.
MAX_RETRY_AFTER_SECONDS = 2


def _get(url: str, **kwargs) -> requests.Response | None:
    """GET with one retry for the failures worth retrying -- a timeout, a
    dropped connection, a 5xx, or a 429. A 404 or 403 is an answer, not a
    blip, and is returned as-is for the caller to reject. Returns None
    only if both attempts failed to get a response at all.

    Spotify rate-limits with a 429 (typically "Retry-After: 0"), which
    arrives looking exactly like a missing picture unless it's called out
    -- so it's retried once after a short back-off and logged by name."""
    kwargs.setdefault('timeout', FETCH_TIMEOUT)
    headers = kwargs.pop('headers', {})
    headers.setdefault('User-Agent', USER_AGENT)

    last_error = None
    for attempt in (1, 2):
        try:
            response = requests.get(url, headers=headers, **kwargs)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            last_error = e
            continue

        retryable = response.status_code >= 500 or response.status_code == 429
        if retryable and attempt == 1:
            if response.status_code == 429:
                try:
                    wait = float(response.headers.get('Retry-After') or 1)
                except ValueError:
                    wait = 1
                last_error = "rate-limited (429)"
                response.close()
                if wait > MAX_RETRY_AFTER_SECONDS:
                    break
                time.sleep(max(wait, 0.5))
            else:
                last_error = f"HTTP {response.status_code}"
                response.close()
            continue

        if response.status_code == 429:
            logger.warning(f"link_preview: rate-limited (429) by {urlparse(url).netloc} after a retry")
        return response

    logger.warning(f"link_preview: {url} unreachable after a retry: {last_error}")
    return None


def _youtube_thumbnail(url: str) -> str | None:
    """YouTube's thumbnail path is derived straight from the video id, so
    this needs no request at all. hqdefault rather than maxresdefault:
    every video has one, while maxres only exists for those uploaded above
    720p and 404s silently for the rest."""
    match = _YOUTUBE_RE.search(url)
    return f"https://i.ytimg.com/vi/{match.group(1)}/hqdefault.jpg" if match else None


def _spotify_artwork(url: str) -> str | None:
    """Cover art for a Spotify link, via Spotify's oEmbed endpoint -- its
    published API for exactly this, answering in a few hundred bytes of
    JSON. Scraping og:image off the track page also works, but means
    pulling down ~170KB of app shell to read one tag, and breaks whenever
    that page's markup changes. The page is still tried as a fallback if
    this comes back empty.

    The link is reduced to its canonical form first: share links carry a
    "?si=" tracking parameter and sometimes a "/intl-xx/" locale prefix,
    neither of which changes what they point at."""
    match = _SPOTIFY_RE.search(url)
    if not match:
        return None
    canonical = f"https://open.spotify.com/{match.group(1).lower()}/{match.group(2)}"

    response = _get('https://open.spotify.com/oembed', params={'url': canonical})
    if response is None:
        return None
    with response:
        if response.status_code != 200:
            # Spotify answers 404 for an item that isn't available in the
            # region the request comes from, which a track the bot can
            # reach from one network may not be from another.
            logger.warning(f"link_preview: Spotify oEmbed returned {response.status_code} for {canonical}")
            return None
        try:
            thumbnail = response.json().get('thumbnail_url')
        except ValueError:
            logger.warning(f"link_preview: Spotify oEmbed sent non-JSON for {canonical}")
            return None

    if not thumbnail:
        logger.warning(f"link_preview: Spotify oEmbed answered 200 but with no thumbnail for {canonical}")
        return None
    image_id = _SPOTIFY_IMAGE_ID_RE.search(thumbnail)
    if not image_id:
        return thumbnail
    return f"https://i.scdn.co/image/{image_id.group(1).replace(_SPOTIFY_300PX, _SPOTIFY_640PX)}"


def _fetch_meta_image(url: str) -> str | None:
    response = _get(url, stream=True)
    if response is None:
        return None

    with response:
        # Anything but a clean 200 is checked explicitly rather than
        # just parsed: Spotify's 404 page still carries an og:image
        # (a generic "download the app" promo), so trusting the body
        # of an error response attaches a confidently wrong picture.
        if response.status_code != 200:
            logger.warning(f"link_preview: {url} returned {response.status_code}")
            return None
        content_type = response.headers.get('Content-Type', '')
        if 'html' not in content_type:
            # Also what a rate-limit or bot-challenge page can look like
            # when it's served with a 200, so it's worth seeing in the log.
            logger.warning(f"link_preview: {url} isn't an HTML page ({content_type or 'no content type'})")
            return None

        body = b''
        try:
            for chunk in response.iter_content(8192):
                body += chunk
                if len(body) >= MAX_BYTES:
                    break
        except requests.exceptions.RequestException as e:
            logger.warning(f"link_preview: {url} dropped mid-read: {e}")
            return None
        text = body.decode('utf-8', errors='replace')

    for pattern in _META_IMAGE_RES:
        match = pattern.search(text)
        if match:
            # Some pages give a path rather than a full URL.
            image_url = urljoin(url, match.group(1).strip())
            if urlparse(image_url).scheme in ('http', 'https'):
                return image_url

    logger.warning(f"link_preview: {url} has no og:image or twitter:image")
    return None


def artwork_for(url: str) -> str | None:
    """The image `url`'s page advertises -- album art for a Spotify track,
    the video thumbnail for YouTube, an article's hero image -- or None if
    it hasn't got one or can't be reached. Never raises: a missing picture
    just means the announcement goes out without one.

    Every miss is logged at warning level with the reason, since a
    picture that silently fails to appear is otherwise impossible to tell
    apart from the bot running out-of-date code."""
    if not url:
        return None

    now = time.time()
    with _cache_lock:
        cached = _cache.get(url)
        if cached and now - cached[0] < cached[1]:
            return cached[2]

    try:
        artwork = _youtube_thumbnail(url) or _spotify_artwork(url) or _fetch_meta_image(url)
    except Exception as e:
        logger.warning(f"link_preview: unexpected failure looking up {url}: {e}")
        artwork = None

    with _cache_lock:
        # Bound the cache: these keys are whatever links have gone past,
        # so without this a long-running bot accumulates them forever.
        if len(_cache) > 512:
            _cache.clear()
        _cache[url] = (now, CACHE_TTL_SECONDS if artwork else MISS_CACHE_TTL_SECONDS, artwork)
    return artwork
