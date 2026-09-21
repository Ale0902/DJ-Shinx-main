"""MCP server exposing basic web-browsing tools (search + page fetch) for
DJ Shinx's /ask command. Run as a subprocess over stdio -- llmask.py spawns
it directly, so it's never started standalone in production.

Search uses the Brave Search API (free tier: 2,000 queries/month, needs
BRAVE_API_KEY in code.env) since DuckDuckGo's endpoints actively block
non-browser clients with a JS anomaly challenge.
"""
import os
import re
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
HTML_TAG_RE = re.compile(r"<[^<]+?>")


@mcp.tool()
def web_search(query: str) -> str:
    """Searches the web via the Brave Search API and returns the top
    results as title/url/snippet entries. Use this to look up current
    events, facts, or anything you're not confident about before
    answering."""
    api_key = os.getenv('BRAVE_API_KEY')
    if not api_key:
        return "Web search isn't configured (missing BRAVE_API_KEY in code.env)."

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
        return f"Search failed: {e}"

    if not results:
        return "No results found."

    lines = []
    for r in results[:5]:
        title = r.get('title', '')
        url = r.get('url', '')
        # Brave highlights matched terms with <strong> tags in the snippet.
        description = HTML_TAG_RE.sub('', r.get('description', ''))
        lines.append(f"{title}\n{url}\n{description}")

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
