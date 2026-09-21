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
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from mcp.server.mcpserver import MCPServer

BASE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(dotenv_path=os.path.join(BASE, 'code.env'))

mcp = MCPServer("dj-shinx-web")

USER_AGENT = "DJ-Shinx-Bot/1.0 (+https://github.com/Ale0902/DJ-Shinx-main)"
MAX_FETCH_CHARS = 4000
BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"
SEARXNG_URL = os.getenv('SEARXNG_URL', 'http://127.0.0.1:8080')
HTML_TAG_RE = re.compile(r"<[^<]+?>")


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
    if results is None:
        results = _brave_search(query)
    if results is None:
        return "Web search is currently unavailable -- both SearXNG and the Brave fallback failed."
    if not results:
        return "No results found."

    lines = [f"{r['title']}\n{r['url']}\n{r['description']}" for r in results]
    return "\n\n".join(lines)


@mcp.tool()
def fetch_page(url: str) -> str:
    """Fetches a web page (e.g. one returned by web_search) and returns
    its readable text content, truncated to a few thousand characters."""
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


if __name__ == "__main__":
    mcp.run()
