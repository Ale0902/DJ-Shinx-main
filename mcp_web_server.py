"""MCP server exposing basic web-browsing tools (search + page fetch) for
DJ Shinx's /chat command. Run as a subprocess over stdio -- llmask.py
spawns it directly, so it's never started standalone in production.

IMPORTANT: this process's stdout is the MCP protocol channel (JSON-RPC
framed messages read by llmask.py's stdio_client). Never print()/log to
stdout here -- it will corrupt the protocol. Anything printed goes to
stderr instead, which is safe and flows through to journalctl since this
is a child process of the bot's own systemd-managed process.

Search tries a self-hosted SearXNG instance first (SEARXNG_URL, default
http://127.0.0.1:8080) -- it's free, has no query quota, and already
aggregates Brave/Google/Wikipedia results itself -- falling back to the
Brave Search API directly (free tier: 2,000 queries/month, needs
BRAVE_API_KEY in code.env) only if the SearXNG container is down or
unreachable, so search still works if that container ever needs a
restart. DuckDuckGo's endpoints were tried first but actively block
non-browser clients with a JS anomaly challenge, so they aren't a viable
option here.
"""
import os
import re
import sys
import ast
import uuid
import math
import operator
import datetime
import requests
from urllib.parse import quote
from zoneinfo import ZoneInfo
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from mcp.server.mcpserver import MCPServer

# Agg is a non-interactive, no-display backend -- must be selected before
# pyplot is imported anywhere, since this runs headless on the VM (no X
# server/display available for the default interactive backend).
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

BASE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(dotenv_path=os.path.join(BASE, 'code.env'))

mcp = MCPServer("dj-shinx-web")

USER_AGENT = "DJ-Shinx-Bot/1.0 (+https://github.com/Ale0902/DJ-Shinx-main)"
MAX_FETCH_CHARS = 4000
BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"
SEARXNG_URL = os.getenv('SEARXNG_URL', 'http://127.0.0.1:8080')
WIKIPEDIA_SUMMARY_URL = "https://en.wikipedia.org/api/rest_v1/page/summary/{}"
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{}"
EASTERN = ZoneInfo("America/New_York")
HTML_TAG_RE = re.compile(r"<[^<]+?>")

# charts/ is shared with llmask.py (same machine, separate process) purely
# by both sides agreeing on this path -- this tool renders a PNG here and
# returns its path in the result text; llmask.py picks that path back out
# and bot.py attaches the file to Discord, deleting it once sent.
CHARTS_DIR = os.path.join(BASE, 'charts')
MAX_CHART_PERIODS = 6

# A few common names for major indices -- anything else is assumed to
# already be a plain ticker symbol (e.g. AAPL, TSLA) and used as-is,
# uppercased, which is exactly the symbol Yahoo Finance expects.
STOCK_ALIASES = {
    's&p 500': '^GSPC', 's&p500': '^GSPC', 'sp500': '^GSPC', 's&p': '^GSPC',
    'smp': '^GSPC', 'smp500': '^GSPC',
    'dow': '^DJI', 'dow jones': '^DJI', 'dow jones industrial average': '^DJI',
    'nasdaq': '^IXIC', 'nasdaq composite': '^IXIC',
}
# A date field may be the literal word "today" instead of YYYY-MM-DD, for
# an ongoing period's end -- the model has repeatedly picked a wrong,
# stale "current" date on its own (e.g. defaulting to a date near its
# training cutoff instead of the real one), so this lets it defer to
# Python's own clock instead of guessing.
PERIOD_SPEC_RE = re.compile(r'^([^:|]+):(\d{4}-\d{2}-\d{2}|today):(\d{4}-\d{2}-\d{2}|today)$', re.IGNORECASE)
DEFAULT_HISTORY_DAYS = 365


def _log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _searxng_search(query: str) -> list[dict] | None:
    """Returns [{title, url, description}, ...] from the self-hosted
    SearXNG instance, or None if it's unreachable (container down, not set
    up, etc.) so the caller can fall back to the Brave API directly."""
    try:
        response = requests.get(
            f'{SEARXNG_URL}/search',
            params={'q': query, 'format': 'json'},
            timeout=10,
        )
        response.raise_for_status()
        results = response.json().get('results', [])
    except Exception as e:
        _log(f"web_search: SearXNG unreachable, falling back to Brave: {e}")
        return None

    return [
        {'title': r.get('title', ''), 'url': r.get('url', ''), 'description': r.get('content', '')}
        for r in results[:5]
    ]


def _brave_search(query: str) -> list[dict] | None:
    """Returns [{title, url, description}, ...] from the Brave Search API
    directly, or None if it's unconfigured or the request failed for any
    reason (missing key, rate limited, network error)."""
    api_key = os.getenv('BRAVE_API_KEY')
    if not api_key:
        return None

    try:
        response = requests.get(
            BRAVE_SEARCH_URL,
            params={'q': query, 'count': 5},
            headers={'Accept': 'application/json', 'X-Subscription-Token': api_key},
            timeout=10,
        )
        response.raise_for_status()
        results = response.json().get('web', {}).get('results', [])
    except Exception as e:
        _log(f"web_search: Brave fallback also failed: {e}")
        return None

    return [
        {
            'title': r.get('title', ''),
            'url': r.get('url', ''),
            # Brave highlights matched terms with <strong> tags in the snippet.
            'description': HTML_TAG_RE.sub('', r.get('description', '')),
        }
        for r in results[:5]
    ]


@mcp.tool()
def web_search(query: str) -> str:
    """Searches the web and returns the top results as title/url/snippet
    entries. Use this to look up current events, facts, or anything you're
    not confident about before answering."""
    results = _searxng_search(query)
    source = "the local SearXNG instance"
    if results is None:
        results = _brave_search(query)
        source = "the Brave Search API (SearXNG was unreachable)"
    if results is None:
        return "Web search is currently unavailable -- both SearXNG and the Brave fallback failed."
    if not results:
        return f"No results found via {source}."

    lines = [f"{r['title']}\n{r['url']}\n{r['description']}" for r in results]
    return f"[Results via {source}]\n\n" + "\n\n".join(lines)


def _searxng_image_search(query: str) -> list[dict] | None:
    """Returns [{title, url, image_url}, ...] from the self-hosted SearXNG
    instance's image category, or None if it's unreachable."""
    try:
        response = requests.get(
            f'{SEARXNG_URL}/search',
            params={'q': query, 'format': 'json', 'categories': 'images'},
            timeout=10,
        )
        response.raise_for_status()
        results = response.json().get('results', [])
    except Exception as e:
        _log(f"image_search: SearXNG unreachable, falling back to Brave: {e}")
        return None

    return [
        {'title': r.get('title', ''), 'url': r.get('url', ''), 'image_url': r.get('img_src', '')}
        for r in results
        if r.get('img_src')
    ][:5]


def _brave_image_search(query: str) -> list[dict] | None:
    """Returns [{title, url, image_url}, ...] from the Brave Image Search
    API directly, or None if it's unconfigured or the request failed."""
    api_key = os.getenv('BRAVE_API_KEY')
    if not api_key:
        return None

    try:
        response = requests.get(
            "https://api.search.brave.com/res/v1/images/search",
            params={'q': query, 'count': 5},
            headers={'Accept': 'application/json', 'X-Subscription-Token': api_key},
            timeout=10,
        )
        response.raise_for_status()
        results = response.json().get('results', [])
    except Exception as e:
        _log(f"image_search: Brave fallback also failed: {e}")
        return None

    return [
        {
            'title': r.get('title', ''),
            'url': r.get('url', ''),
            'image_url': (r.get('properties') or {}).get('url', ''),
        }
        for r in results
        if (r.get('properties') or {}).get('url')
    ][:5]


@mcp.tool()
def image_search(query: str) -> str:
    """Searches for images matching a description and returns direct
    image links (not just pages that mention the topic). Use this,
    instead of web_search, when asked for a picture, photo, image, or
    video thumbnail of something."""
    results = _searxng_image_search(query)
    source = "the local SearXNG instance"
    if not results:
        results = _brave_image_search(query)
        source = "the Brave Image Search API (SearXNG had no image results)"
    if not results:
        return "No images found for that."

    lines = [f"{r['title']}\nImage: {r['image_url']}\nPage: {r['url']}" for r in results]
    return f"[Image results via {source}]\n\n" + "\n\n".join(lines)


@mcp.tool()
def fetch_page(url: str) -> str:
    """Fetches a web page, such as one returned by web_search, and
    returns its readable text content, truncated to a few thousand
    characters."""
    try:
        response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=10)
        response.raise_for_status()
    except Exception as e:
        return f"Couldn't fetch {url}: {e}"

    soup = BeautifulSoup(response.text, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header"]):
        tag.decompose()

    text = re.sub(r"\n{3,}", "\n\n", soup.get_text("\n", strip=True))
    if len(text) > MAX_FETCH_CHARS:
        text = text[:MAX_FETCH_CHARS] + "... [truncated]"
    return text


@mcp.tool()
def current_datetime() -> str:
    """Returns the current real-world date and time (US Eastern). Use
    this whenever you need to know what "today", "now", "recent", or
    "current" actually means -- your training data has a fixed cutoff and
    doesn't know how much time has passed since, which has caused you to
    describe outdated things as current before."""
    now = datetime.datetime.now(EASTERN)
    return now.strftime("%A, %B %d, %Y, %I:%M %p ET").replace(" 0", " ")


@mcp.tool()
def wikipedia_summary(topic: str) -> str:
    """Returns a short factual summary of the Wikipedia article for a
    person, place, or thing, with its source link. Faster and more
    reliable than web_search for straightforward "who/what is X"
    questions. If the topic doesn't match an article title closely, this
    may come back empty -- fall back to web_search in that case."""
    try:
        response = requests.get(
            WIKIPEDIA_SUMMARY_URL.format(quote(topic.replace(' ', '_'))),
            headers={"User-Agent": USER_AGENT},
            timeout=10,
        )
        if response.status_code == 404:
            return f"No Wikipedia article found for '{topic}'. Try web_search instead."
        response.raise_for_status()
        data = response.json()
    except Exception as e:
        return f"Wikipedia lookup failed: {e}"

    extract = data.get('extract', '')
    if not extract:
        return f"No summary available for '{topic}'. Try web_search instead."

    url = data.get('content_urls', {}).get('desktop', {}).get('page', '')
    return f"{extract}\n\nSource: {url}" if url else extract


def _resolve_symbol(name: str) -> str:
    key = name.strip().lower()
    return STOCK_ALIASES.get(key, name.strip().upper())


def _parse_date_field(value: str) -> datetime.date:
    if value.strip().lower() == 'today':
        return datetime.datetime.now(EASTERN).date()
    return datetime.date.fromisoformat(value)


def _parse_periods(spec: str) -> list[tuple[str, datetime.date, datetime.date]]:
    """Parses 'Label:start:end | Label:start:end | ...' into
    [(label, start_date, end_date), ...], raising ValueError with a
    message the model can act on if a segment doesn't match."""
    periods = []
    for segment in spec.split('|'):
        segment = segment.strip()
        match = PERIOD_SPEC_RE.match(segment)
        if not match:
            raise ValueError(
                f"Couldn't parse period '{segment}' -- expected "
                "Label:YYYY-MM-DD:YYYY-MM-DD (or 'today' in place of a date)."
            )
        label, start, end = match.groups()
        periods.append((label.strip(), _parse_date_field(start), _parse_date_field(end)))
    return periods


def _fetch_daily_closes(symbol: str, start_ts: int, end_ts: int) -> list[tuple[datetime.date, float]] | None:
    """Returns [(date, close), ...] sorted ascending from Yahoo Finance's
    public chart API, or None if the request failed or the symbol doesn't
    exist. No API key needed -- unlike Stooq's download endpoint (which
    now sits behind a JS browser-verification challenge, the same kind of
    block that ruled out DuckDuckGo for search), this JSON endpoint has
    stayed reliably scriptable."""
    try:
        response = requests.get(
            YAHOO_CHART_URL.format(quote(symbol)),
            params={'period1': start_ts, 'period2': end_ts, 'interval': '1d'},
            headers={"User-Agent": USER_AGENT},
            timeout=10,
        )
        response.raise_for_status()
        result = response.json().get('chart', {}).get('result')
    except Exception as e:
        _log(f"compare_stock_performance: fetch failed for {symbol}: {e}")
        return None
    if not result:
        return None

    timestamps = result[0].get('timestamp') or []
    quote_block = (result[0].get('indicators', {}).get('quote') or [{}])[0] or {}
    closes = quote_block.get('close') or []
    points = [
        (datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).date(), close)
        for ts, close in zip(timestamps, closes)
        if close is not None
    ]
    return points or None


def _render_bar_chart(title: str, data: list[tuple[str, float]]) -> str:
    """Renders a labeled bar chart to a PNG under CHARTS_DIR and returns
    its path. Solid dark background (not transparent) so the white text
    stays legible regardless of the viewer's own Discord theme."""
    os.makedirs(CHARTS_DIR, exist_ok=True)
    labels = [d[0] for d in data]
    values = [d[1] for d in data]

    fig, ax = plt.subplots(figsize=(7, 4.5), dpi=120)
    fig.patch.set_facecolor('#313338')
    ax.set_facecolor('#313338')
    bars = ax.bar(labels, values, color='#00FFFF')
    ax.axhline(0, color='#888888', linewidth=0.8)
    ax.set_ylabel('% change', color='white')
    ax.set_title(title, color='white')
    ax.tick_params(colors='white')
    for spine in ax.spines.values():
        spine.set_color('#888888')
    for bar, value in zip(bars, values):
        ax.annotate(
            f'{value:+.1f}%',
            (bar.get_x() + bar.get_width() / 2, bar.get_height()),
            textcoords="offset points", xytext=(0, 4 if value >= 0 else -14),
            ha='center', color='white', fontsize=9,
        )

    path = os.path.join(CHARTS_DIR, f"{uuid.uuid4().hex}.png")
    fig.savefig(path, facecolor=fig.get_facecolor(), bbox_inches='tight')
    plt.close(fig)
    return path


@mcp.tool()
def compare_stock_performance(query: str) -> str:
    """Looks up real historical closing prices for a stock or index and
    charts the percent change across one or more labeled date ranges.
    Use this for a comparison across specific NAMED periods (e.g. two
    presidential terms, two different years) -- for a single ongoing
    trend ("how's X doing currently/lately/this year"), use
    stock_price_history instead, which draws a line graph over time
    rather than one bar per period. Never estimate or invent a
    percentage yourself, always get it from this tool. Format: "SYMBOL |
    Label:YYYY-MM-DD:YYYY-MM-DD | Label:YYYY-MM-DD:YYYY-MM-DD", for
    example "S&P 500 | Trump Term 1:2017-01-20:2021-01-19 | Biden
    Term:2021-01-20:2025-01-19". For an ongoing period, use the literal
    word "today" instead of guessing a date, e.g. "Trump Term
    2:2025-01-20:today". Avoid apostrophes in labels -- write "Biden
    Term", not "Biden's Term"."""
    parts = query.split('|', 1)
    if len(parts) != 2:
        return "Couldn't parse that -- format is 'SYMBOL | Label:YYYY-MM-DD:YYYY-MM-DD | ...'."
    symbol_name, period_spec = parts[0].strip(), parts[1]

    try:
        periods = _parse_periods(period_spec)
    except ValueError as e:
        return str(e)
    if not periods:
        return "No periods given -- format is 'SYMBOL | Label:YYYY-MM-DD:YYYY-MM-DD | ...'."
    if len(periods) > MAX_CHART_PERIODS:
        return f"Too many periods (max {MAX_CHART_PERIODS}) -- try comparing fewer at once."

    symbol = _resolve_symbol(symbol_name)
    overall_start = min(p[1] for p in periods)
    overall_end = max(p[2] for p in periods)
    points = _fetch_daily_closes(
        symbol,
        int(datetime.datetime.combine(overall_start, datetime.time.min, tzinfo=datetime.timezone.utc).timestamp()),
        int(datetime.datetime.combine(overall_end + datetime.timedelta(days=1), datetime.time.min, tzinfo=datetime.timezone.utc).timestamp()),
    )
    if not points:
        return f"Couldn't fetch historical data for '{symbol_name}' ({symbol}) -- make sure you used its real ticker symbol (e.g. ACN for Accenture, AAPL for Apple), not the company name, and try again."

    lines = [f"{symbol_name} ({symbol}) performance by period:"]
    chart_data = []
    for label, start, end in periods:
        start_point = next((p for p in points if p[0] >= start), None)
        end_point = next((p for p in reversed(points) if p[0] <= end), None)
        if not start_point or not end_point or start_point[0] > end_point[0]:
            lines.append(f"- {label}: no trading data available in that range.")
            continue
        pct_change = (end_point[1] - start_point[1]) / start_point[1] * 100
        lines.append(
            f"- {label} ({start_point[0]} to {end_point[0]}): "
            f"{start_point[1]:.2f} -> {end_point[1]:.2f} ({pct_change:+.1f}%)"
        )
        chart_data.append((label, pct_change))

    if not chart_data:
        return "\n".join(lines) + "\n\nNo valid data available to chart."

    chart_path = _render_bar_chart(f"{symbol_name} % change by period", chart_data)
    lines.append(f"\nCHART_PATH: {chart_path}")
    return "\n".join(lines)


def _render_line_chart(title: str, points: list[tuple[datetime.date, float]]) -> str:
    """Renders a closing-price line chart to a PNG under CHARTS_DIR and
    returns its path. Same dark styling as _render_bar_chart."""
    os.makedirs(CHARTS_DIR, exist_ok=True)
    dates = [p[0] for p in points]
    closes = [p[1] for p in points]

    fig, ax = plt.subplots(figsize=(7, 4.5), dpi=120)
    fig.patch.set_facecolor('#313338')
    ax.set_facecolor('#313338')
    ax.plot(dates, closes, color='#00FFFF', linewidth=1.6)
    ax.set_ylabel('Price', color='white')
    ax.set_title(title, color='white')
    ax.tick_params(colors='white')
    for spine in ax.spines.values():
        spine.set_color('#888888')
    fig.autofmt_xdate()

    path = os.path.join(CHARTS_DIR, f"{uuid.uuid4().hex}.png")
    fig.savefig(path, facecolor=fig.get_facecolor(), bbox_inches='tight')
    plt.close(fig)
    return path


@mcp.tool()
def stock_price_history(query: str) -> str:
    """Looks up a stock or index's real closing-price history and charts
    it as a line graph over time. Use this for a single ongoing trend --
    "how's X doing currently, lately, or this year" -- not a comparison
    across specific named periods (use compare_stock_performance for
    that instead). Format: just "SYMBOL" for the trailing year up to
    today (use this for almost everything -- it already covers
    "lately"/"currently"/"this year"), "SYMBOL:YYYY-MM-DD" (or
    "SYMBOL:today") for the trailing year up to a specific end date, or
    "SYMBOL:YYYY-MM-DD:YYYY-MM-DD" for an explicit start and end.
    Never estimate the price or trend yourself -- this always reflects
    today's actual date and real market data, which you're often wrong
    about on your own."""
    parts = [p.strip() for p in query.split(':')]
    symbol_name = parts[0]
    if not symbol_name:
        return "Couldn't parse that -- format is 'SYMBOL' or 'SYMBOL:YYYY-MM-DD:YYYY-MM-DD'."
    symbol = _resolve_symbol(symbol_name)

    try:
        if len(parts) == 1:
            end = datetime.datetime.now(EASTERN).date()
            start = end - datetime.timedelta(days=DEFAULT_HISTORY_DAYS)
        elif len(parts) == 2:
            # Shorthand for "the trailing year up to this end date" --
            # accepted because the model has been observed trying to
            # write exactly this (e.g. "S&P 500:today") when it only
            # wants a different end point, not a specific start too.
            end = _parse_date_field(parts[1])
            start = end - datetime.timedelta(days=DEFAULT_HISTORY_DAYS)
        elif len(parts) == 3:
            start = _parse_date_field(parts[1])
            end = _parse_date_field(parts[2])
        else:
            return "Couldn't parse that -- format is 'SYMBOL' or 'SYMBOL:YYYY-MM-DD:YYYY-MM-DD'."
    except ValueError:
        return "Couldn't parse those dates -- format is 'SYMBOL' or 'SYMBOL:YYYY-MM-DD:YYYY-MM-DD'."

    if start > end:
        return "Start date is after the end date -- swap them and try again."

    points = _fetch_daily_closes(
        symbol,
        int(datetime.datetime.combine(start, datetime.time.min, tzinfo=datetime.timezone.utc).timestamp()),
        int(datetime.datetime.combine(end + datetime.timedelta(days=1), datetime.time.min, tzinfo=datetime.timezone.utc).timestamp()),
    )
    if not points:
        return f"Couldn't fetch historical data for '{symbol_name}' ({symbol}) -- make sure you used its real ticker symbol (e.g. ACN for Accenture, AAPL for Apple), not the company name, and try again."

    first_date, first_close = points[0]
    last_date, last_close = points[-1]
    pct_change = (last_close - first_close) / first_close * 100
    high = max(p[1] for p in points)
    low = min(p[1] for p in points)

    chart_path = _render_line_chart(f"{symbol_name} ({symbol}) closing price", points)
    return (
        f"{symbol_name} ({symbol}) from {first_date} to {last_date}: "
        f"{first_close:.2f} -> {last_close:.2f} ({pct_change:+.1f}%). "
        f"Range over that span: {low:.2f} to {high:.2f}.\n"
        f"\nCHART_PATH: {chart_path}"
    )


# A restricted arithmetic evaluator for the calculate() tool -- walks the
# expression's AST and only permits numbers, basic operators, and a
# whitelisted set of math functions/constants, rather than using eval()
# (which would let an LLM-generated string run arbitrary Python).
_BINOPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod, ast.Pow: operator.pow,
}
_UNARYOPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_FUNCS = {
    'abs': abs, 'round': round, 'min': min, 'max': max,
    'sqrt': math.sqrt, 'sin': math.sin, 'cos': math.cos, 'tan': math.tan,
    'log': math.log, 'log10': math.log10, 'exp': math.exp,
    'floor': math.floor, 'ceil': math.ceil,
}
_CONSTANTS = {'pi': math.pi, 'e': math.e}


def _safe_eval(node):
    if isinstance(node, ast.Expression):
        return _safe_eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _BINOPS:
        return _BINOPS[type(node.op)](_safe_eval(node.left), _safe_eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARYOPS:
        return _UNARYOPS[type(node.op)](_safe_eval(node.operand))
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCS:
        return _FUNCS[node.func.id](*(_safe_eval(a) for a in node.args))
    if isinstance(node, ast.Name) and node.id in _CONSTANTS:
        return _CONSTANTS[node.id]
    raise ValueError("unsupported expression")


@mcp.tool()
def calculate(expression: str) -> str:
    """Evaluates a math expression (+, -, *, /, //, %, **, and functions
    like sqrt/sin/cos/log/round) and returns the result. Use this for any
    calculation instead of doing the arithmetic yourself -- you're
    unreliable at multi-digit math."""
    try:
        result = _safe_eval(ast.parse(expression, mode='eval'))
    except Exception as e:
        return f"Couldn't evaluate '{expression}': {e}"
    return str(result)


if __name__ == "__main__":
    mcp.run()
