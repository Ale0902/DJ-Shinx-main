import os
import sys
import re
import time
import logging
import asyncio
import threading
import requests

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

import memory_db

logger = logging.getLogger(__name__)

# Point this at the Ollama server via OLLAMA_URL/OLLAMA_MODEL in code.env.
# Read lazily (not at import time) since bot.py loads code.env after importing this module.
# bot.py wraps every reply in an embed, whose description allows up to 4096
# chars -- comfortably more room than a plain message's 2000-char cap.
MAX_DISCORD_LEN = 4000

# Each iteration is one Ollama round-trip; a typical "search, maybe fetch a
# page, then answer" exchange takes 2-3, so this caps worst-case latency
# without cutting off legitimate multi-step lookups.
MAX_TOOL_ITERATIONS = 4
# A single Ollama call can legitimately take a while on a 12B model split
# across modest GPUs, especially with a lot of context (conversation
# history + search results) or a longer response to generate -- 120s was
# too tight and cut off an otherwise-successful generation.
OLLAMA_TIMEOUT = 180

# Ollama silently truncates older context rather than erroring once a
# request exceeds this, and its own default (historically 2048 unless a
# model's Modelfile overrides it) is easy to blow past once the system
# prompt, tool descriptions, known facts, conversation history, and search
# results are all combined -- which would look like the model randomly
# "forgetting" earlier instructions or context with no visible cause.
# Override via OLLAMA_NUM_CTX in code.env if this doesn't fit your model's
# available VRAM.
DEFAULT_OLLAMA_NUM_CTX = 8192

BASE = os.path.dirname(os.path.abspath(__file__))
MCP_SERVER_SCRIPT = os.path.join(BASE, 'mcp_web_server.py')

# In-memory conversation state, keyed by whatever the caller passes as a
# conversation_id (bot.py uses (channel_id, user_id), so each person's
# conversation in a given channel is independent). Holds both the visible
# question/answer pairs (not each turn's internal TOOL_CALL/tool-result
# scaffolding, so it doesn't balloon with raw search dumps) and the set of
# URLs actually seen from a tool across the whole conversation, so a later
# "what's your source for that" can be verified against something found in
# an earlier turn, not just the current one. Cleared on bot restart; that's
# fine, this is meant to feel like a chat session, not a permanent record.
CONVERSATION_TTL_SECONDS = 20 * 60  # idle this long and the next /ask starts fresh
MAX_HISTORY_TURNS = 8  # question/answer pairs kept; oldest dropped first

_conversations: dict = {}
_conversations_lock = threading.Lock()


def _get_conversation(conversation_id) -> tuple[list[dict], set[str]]:
    with _conversations_lock:
        entry = _conversations.get(conversation_id)
        if not entry:
            return [], set()
        last_used, history, seen_urls = entry
        if time.monotonic() - last_used > CONVERSATION_TTL_SECONDS:
            del _conversations[conversation_id]
            return [], set()
        return list(history), set(seen_urls)


def _save_conversation(conversation_id, history: list[dict], seen_urls: set[str]) -> None:
    trimmed = history[-(MAX_HISTORY_TURNS * 2):]
    with _conversations_lock:
        _conversations[conversation_id] = (time.monotonic(), trimmed, seen_urls)


def forget(conversation_id) -> None:
    """Clears a conversation's history and remembered sources -- backs the
    /forget command."""
    with _conversations_lock:
        _conversations.pop(conversation_id, None)

# Ollama's native `tools` API (structured tool_calls) isn't supported by every
# model's chat template -- e.g. gemma3 rejects a request outright (400) if
# `tools` is even present. This prompts the model to request a tool by
# writing a specific line of text instead, which works with any chat model
# regardless of native tool-calling support.
#
# Group 2 backreferences the opening quote (\2) rather than banning quote
# characters from the argument outright -- an argument with an apostrophe
# in it (e.g. a "Biden's Term" chart label) would otherwise fail to match
# at all, silently breaking tool-call detection and leaking the raw
# TOOL_CALL: ... text into the user-facing answer instead. Greedy '.*'
# backtracks to the *last* matching-quote-before-close, so this still
# works even for a single-quoted argument that itself contains an
# apostrophe.
TOOL_CALL_RE = re.compile(r'TOOL_CALL:\s*(\w+)\(\s*(["\'])(.*)\2\s*\)')

# Fallback for a close-but-not-quite call: the small local model has been
# observed dropping the "TOOL_CALL:" prefix and the argument's quote marks
# entirely (e.g. a bare "stock_price_history(MSFT:2023-09-18:today)" on
# its own line) while still getting the tool name and argument content
# right. Only trusted when the captured name is an actual known tool (see
# _extract_tool_call) -- ordinary prose essentially never takes the shape
# of one of these specific names immediately followed by "(...)", so this
# doesn't risk misreading a normal sentence as a tool call. Anchored to a
# whole line (MULTILINE ^...$) rather than searched anywhere in the
# text, for the same reason.
LOOSE_TOOL_CALL_RE = re.compile(r'^(\w+)\(\s*["\']?(.*?)["\']?\s*\)\s*$', re.MULTILINE)

# Third, even looser fallback specifically for stock_price_history: the
# model has also been observed dropping the tool name AND parens
# entirely, leaving just a bare argument-shaped fragment as its WHOLE
# reply (e.g. "S&P 500:today" instead of a real call). Only matched
# against the ENTIRE stripped response (fullmatch, not a substring
# search), with sentence punctuation banned from the "symbol" part and a
# date/"today" suffix REQUIRED -- an ordinary short final answer never
# takes this exact shape (it has real sentence structure), so this is a
# narrow catch for that one specific failure, not a general "guess what
# this means" parser. Never routed to compare_stock_performance, whose
# format always has a "|", which this excludes.
BARE_STOCK_ARG_RE = re.compile(r'^[^|:.,!?\n]{1,40}(?::(?:\d{4}-\d{2}-\d{2}|today)){1,2}$', re.IGNORECASE)


def _extract_tool_call(content: str, known_tools) -> tuple[str, str] | None:
    """Returns (name, arg) if content contains a tool call in the strict,
    loose, or bare-stock-argument form, else None."""
    match = TOOL_CALL_RE.search(content)
    if match:
        return match.group(1), match.group(3)

    loose_match = LOOSE_TOOL_CALL_RE.search(content)
    if loose_match and loose_match.group(1) in known_tools:
        return loose_match.group(1), loose_match.group(2).strip('"\'')

    if 'stock_price_history' in known_tools and BARE_STOCK_ARG_RE.fullmatch(content.strip()):
        return 'stock_price_history', content.strip()

    return None

# For verifying the model's final "Source: <url>" citation against URLs it
# actually saw from a tool this turn, rather than trusting it not to cite
# something recalled from memory (it has, more than once).
URL_RE = re.compile(r'https?://\S+')
SOURCE_LINE_RE = re.compile(r'^Source:\s*(\S+)\s*$', re.MULTILINE)

# compare_stock_performance (mcp_web_server.py) renders a chart to a local
# PNG and tags its path with this marker in the tool result text -- pulled
# out here into its own side channel (not left for the model to repeat
# back verbatim in its final answer, which would be an unnecessary and
# fragile way to get a filesystem path in front of bot.py) so it can be
# returned up to bot.py to attach as a real Discord file.
CHART_PATH_RE = re.compile(r'^CHART_PATH:\s*(.+?)\s*$', re.MULTILINE)


def _extract_urls(text: str) -> set[str]:
    return {u.rstrip('.,)') for u in URL_RE.findall(text)}


def _normalize_url(url: str) -> str:
    """Strips scheme and a leading 'www.' and trailing slash, so citation
    matching isn't tripped up by http vs https or a trailing slash on an
    otherwise-identical URL."""
    return re.sub(r'^https?://(www\.)?', '', url.strip()).rstrip('/')


def _verify_citation(content: str, seen_urls: set[str]) -> str:
    """Strips the model's "Source: <url>" line if that URL never actually
    came back from a tool call this turn (or an earlier turn in the same
    conversation) -- catches the model citing a plausible-looking URL it
    recalled from training data instead of one it genuinely looked up."""
    match = SOURCE_LINE_RE.search(content)
    if not match:
        return content

    cited = _normalize_url(match.group(1).rstrip('.,)/'))
    normalized_seen = {_normalize_url(u) for u in seen_urls}
    # Exact match after normalization, or a same-page variant (query string
    # dropped, etc.) -- but require enough shared length that two merely
    # similar paths on the same site can't false-positive off each other.
    if any(cited == u or (len(cited) > 12 and (cited in u or u in cited)) for u in normalized_seen):
        return content

    stripped = SOURCE_LINE_RE.sub('', content).rstrip()
    return stripped + "\n\n(Note: I couldn't verify that source against what I actually looked up -- treat this with caution.)"


# Whether *this turn's* message is asking to be shown a source/link, so the
# citation only gets displayed when actually requested rather than tacked
# onto every reply -- checked against the current message only, since a
# fresh request each turn naturally covers both asking up front and asking
# as a separate follow-up. Also covers asking for a specific piece of media
# (a video, an image, a page/article) -- those are implicitly asking for a
# link too, since there's no way to watch/view one without a URL.
SOURCE_REQUEST_RE = re.compile(
    r'\b(source|sources|link|links|url|urls|cite|citation|reference|proof|prove it|'
    r'video|videos|youtube|watch|picture|pictures|image|images|photo|photos|'
    r'website|webpage|web page|page|article|articles|tweet|post|clip|stream)\b',
    re.IGNORECASE,
)


def _wants_source(question: str) -> bool:
    return bool(SOURCE_REQUEST_RE.search(question))


# Narrower than SOURCE_REQUEST_RE above -- specifically "the user wants an
# actual image asset", not just "show me a link". Deliberately excludes
# "video": a request like "give me a youtube video of X" wants a page about
# X (which web_search already handles fine), not a thumbnail. Decides which
# tool the forced up-front search below uses, because a generic web_search
# for "picture of X" comes back with pages *about* X -- Pinterest boards,
# wallpaper-gallery listings -- never a direct image file, and the model
# doesn't reliably make the extra image_search call itself afterward; it
# just cites one of those gallery pages as if it were the image. Same
# failure pattern as the other soft/judgment instructions in this file,
# fixed the same way: detect it deterministically instead of trusting the
# model to juggle two competing instructions correctly.
IMAGE_REQUEST_RE = re.compile(
    r'\b(picture|pictures|pic|pics|image|images|photo|photos|wallpaper|wallpapers)\b',
    re.IGNORECASE,
)


def _wants_image(question: str) -> bool:
    return bool(IMAGE_REQUEST_RE.search(question))


def _strip_citation(content: str) -> str:
    return SOURCE_LINE_RE.sub('', content).rstrip()


# Whether the user explicitly asked for a complete enumeration ("name all
# the...", "list every...") -- the brevity instruction ("1-3 sentences")
# otherwise leads the model to substitute a short summary (e.g. "there are
# 89 characters") for the actual list that was asked for. Detected per-turn
# and used to swap in a different instruction, rather than trusting the
# model to correctly balance two competing instructions in one static
# prompt -- that's failed for other soft, judgment-based instructions
# enough times this session to not lean on it here either.
LIST_REQUEST_RE = re.compile(
    r'\b(list all|name all|list every|name every|all of the|every single|'
    r'complete list|full list|enumerate)\b',
    re.IGNORECASE,
)


def _wants_full_list(question: str) -> bool:
    return bool(LIST_REQUEST_RE.search(question))


# Phrases indicating the model itself doesn't trust its own answer (usually
# surfacing when challenged, e.g. "cite your source") -- a signal to retry
# with a fresh, better-targeted search instead of just accepting "I was
# wrong" / "I can't confirm this" as final.
SELF_CORRECTION_RE = re.compile(
    r"\b(i apologi[sz]e|inaccurate|unable to confirm|i'?m not sure|"
    r"i don'?t have (a |any )?(reliable |real )?source|i made a mistake|"
    r"that (was|is) (incorrect|wrong)|i cannot confirm|i can'?t verify|"
    r"i don'?t actually know)\b",
    re.IGNORECASE,
)


def _pick_retry_query(question: str, history: list[dict]) -> str:
    """Picks what to actually re-search on a self-correction retry. If
    this turn's message is itself just a meta request like "cite your
    source", that text makes a useless search query -- fall back to the
    last real question in history instead, so the retry searches for the
    League of Legends match, not for "cite your source"."""
    if not _wants_source(question):
        return question
    for msg in reversed(history):
        if msg.get('role') == 'user':
            return msg.get('content') or question
    return question


# Serializes actual Ollama requests across concurrent /chat calls. The VM's
# two GPUs are already snug on VRAM for one gemma3:12b generation (~5.5GB of
# ~10GB combined) -- letting two users' requests hit Ollama at once doesn't
# give real parallelism, it just makes both generations slower and more
# likely to blow past OLLAMA_TIMEOUT (which only covers one request's own
# wait, not queueing behind someone else's). Serializing means the second
# user's request predictably waits its turn instead of both potentially
# timing out.
_ollama_lock = threading.Lock()


def _ollama_chat(
    messages: list[dict], ollama_url: str, ollama_model: str, ollama_num_ctx: int, on_status=None,
) -> dict:
    """on_status(text), if given, is called with a queued-notice if this
    call has to wait for another in-flight request to finish first -- lets
    the caller tell the user they're queued instead of leaving them sat
    waiting with no explanation."""
    if not _ollama_lock.acquire(blocking=False):
        if on_status:
            on_status(
                "⏳ Someone else is chatting with me right now -- you're queued, "
                "this might take a bit longer than usual..."
            )
        _ollama_lock.acquire()  # now block until it's actually our turn

    try:
        response = requests.post(
            f'{ollama_url}/api/chat',
            json={
                'model': ollama_model,
                'messages': messages,
                'stream': False,
                'options': {'num_ctx': ollama_num_ctx},
            },
            timeout=OLLAMA_TIMEOUT,
        )
    finally:
        _ollama_lock.release()
    if not response.ok:
        # Ollama's error responses are {"error": "<reason>"} -- surface that
        # instead of requests' generic "400 Client Error" (no body detail).
        try:
            detail = response.json().get('error', response.text)
        except ValueError:
            detail = response.text
        raise requests.exceptions.HTTPError(f"Ollama returned {response.status_code}: {detail}")
    return response.json()


def _describe_exception(e: BaseException) -> str:
    """Unwraps ExceptionGroups -- anyio's TaskGroup (used internally by the
    MCP client) wraps whatever actually failed in one, and printing the
    group itself just says "unhandled errors in a TaskGroup" with no
    detail. This digs out the real underlying error(s) instead."""
    if isinstance(e, BaseExceptionGroup):
        return "; ".join(_describe_exception(sub) for sub in e.exceptions)
    return f"{type(e).__name__}: {e}"


def _tool_description(tool) -> str:
    """The tool's docstring, trimmed to its first complete sentence --
    NOT just its first source-code *line*, which used to cut every one of
    these off mid-thought (e.g. wikipedia_summary's became "Returns a
    short factual summary of the Wikipedia article for a") since the
    docstrings are hand-wrapped at ~79 chars, not written one sentence
    per line."""
    if not tool.description:
        return ''
    joined = ' '.join(line.strip() for line in tool.description.strip().splitlines())
    first_sentence = joined.split('. ')[0]
    return first_sentence.rstrip('.') + '.'


def _build_system_prompt(
    mcp_tools, wants_full_list: bool = False, extra_tools: list[tuple[str, str, str]] | None = None
) -> tuple[str, dict[str, str]]:
    """Returns (system_prompt, {tool_name: its single string param name}).
    Both of the MCP server's tools (web_search, fetch_page) take exactly
    one string argument, so the param name is read straight off each
    tool's JSON schema instead of being hardcoded here. extra_tools is for
    tools that aren't served by the MCP subprocess at all (e.g.
    remember_fact, handled locally since it needs the caller's user_id) --
    each is (name, param_name, description)."""
    tool_param = {}
    tool_lines = []
    for t in mcp_tools:
        props = (t.input_schema or {}).get('properties', {})
        param_name = next(iter(props), 'value')
        tool_param[t.name] = param_name
        tool_lines.append(f'- {t.name}("{param_name}") -- {_tool_description(t)}')

    for name, param_name, description in (extra_tools or []):
        tool_param[name] = param_name
        tool_lines.append(f'- {name}("{param_name}") -- {description}')

    if wants_full_list:
        brevity_instruction = (
            "The user explicitly asked you to list/name/enumerate everything "
            "of some kind -- give the actual complete list they asked for, "
            "not a short summary or just a count. Length isn't capped for "
            "this one."
        )
    else:
        brevity_instruction = (
            "Give a short, direct summary that answers the question -- 1-3 "
            "sentences for most questions, more only if it genuinely needs "
            "detail."
        )

    system_prompt = (
        f"You are Agent Shinx, a Discord bot that works like a quick search "
        f"engine. {brevity_instruction} Plain, direct tone -- not overly "
        f"casual, not full of slang or emoji.\n\n"
        "Every question already comes with fresh web search results "
        "attached below it -- that search already ran automatically, you "
        "don't need to decide whether to do it. Use those results to "
        "ground any factual claim -- names, dates, rankings, recent "
        "events, anything you aren't 100% certain of. If they're "
        "irrelevant (e.g. the question is just casual conversation, or "
        "something you're already completely certain about), ignore them "
        "and answer normally.\n\n"
        "If those results aren't enough, you can call one of these tools "
        "yourself for a follow-up -- e.g. read a specific page in full, "
        "search again with different terms, or look something up more "
        "precisely:\n"
        + "\n".join(tool_lines) +
        '\n\nTo use one, reply with EXACTLY one line in this form and nothing else:\n'
        'TOOL_CALL: tool_name("argument")\n\n'
        "Never guess or invent specific facts, names, dates, or sources -- "
        "if you don't actually have information to support a claim, say so "
        "honestly instead of making something up, including if asked for a "
        "source you don't have.\n\n"
        "When a tool result conflicts with what you think you know, trust "
        "the tool result, not your memory -- this matters especially for "
        "people, teams, or things with common or ambiguous names, where "
        "you might be thinking of a different one. Don't blend facts about "
        "a different person/thing with a similar name into your answer.\n\n"
        "Summarize what you found in your own words, not pasted verbatim, "
        "and end with the source URL on its own line, like 'Source: <url>', "
        "when you used one. Before you finish, check that your answer "
        "actually matches the source you're citing -- if it doesn't, you've "
        "made a mistake and should fix it or say you're not sure. Plain "
        "text, no prefix, and don't mention that you searched.\n\n"
        "If asked for a picture, photo, image, or video of something, use "
        "image_search (not web_search) and put the exact image_url it "
        "returns as your 'Source: <url>' line, copied exactly, not "
        "paraphrased or shortened -- Discord displays that image inline "
        "automatically, so you ARE able to show it. Don't say you're "
        "unable to provide images when a tool result actually gave you a "
        "direct image_url to use.\n\n"
        "Any question about how a stock or index has performed, moved, "
        "or changed -- not just an explicit request for a graph or "
        "chart -- MUST be answered using one of these two tools, never "
        "estimated or invented, and never answered from the general web "
        "search results above even if those happen to mention a number: "
        "they're not reliable for a specific statistic like this, only "
        "these tools compute it directly from real market data.\n"
        "- compare_stock_performance: a comparison across specific NAMED "
        "periods (e.g. two presidential terms, two different years). "
        "Format: \"SYMBOL | Label:YYYY-MM-DD:YYYY-MM-DD | Label:"
        "YYYY-MM-DD:YYYY-MM-DD\", for example \"S&P 500 | Trump Term "
        "1:2017-01-20:2021-01-19 | Biden Term:2021-01-20:2025-01-19\".\n"
        "- stock_price_history: a single ongoing trend -- \"how's X "
        "doing currently/lately/this year\". Just use \"SYMBOL\" alone "
        "(e.g. \"AAPL\") for almost all of these -- it already covers "
        "the trailing year up to today, which is close enough for "
        "\"this year\"/\"lately\"/\"currently\" too. Only add a date "
        "(\"SYMBOL:YYYY-MM-DD:YYYY-MM-DD\", or \"SYMBOL:YYYY-MM-DD\" for "
        "just a different end point) if the user names a genuinely "
        "different specific range.\n"
        "Both tools render an actual chart automatically, which the user "
        "will see -- you DO have this ability. Never say you're unable "
        "to show a graph/chart or suggest the user make one themselves "
        "in Excel or similar; if a stock/index graph is asked for, call "
        "the appropriate tool yourself instead of deflecting.\n"
        "For either tool, use the literal word \"today\" in place of a "
        "date for an ongoing period's end (e.g. \"Trump Term "
        "2:2025-01-20:today\") instead of guessing what today's date is "
        "yourself -- you're frequently wrong about that, defaulting to a "
        "date near your training cutoff instead of the real one. Use "
        "well-known public dates (like inauguration dates) for period "
        "starts. Don't use apostrophes in labels (write \"Biden Term\", "
        "not \"Biden's Term\"). Don't add a 'Source:' line for either "
        "tool -- it'll be stripped out automatically if you do."
    )
    return system_prompt, tool_param


REMEMBER_FACT_DESCRIPTION = (
    "Saves a short fact about this user to remember in future conversations "
    "(e.g. their favorite team, where they live, a preference they "
    "mentioned) -- use this when they tell you something personal worth "
    "remembering long-term, not for trivia about the search topic itself."
)

# User-facing status text shown while a tool call is in flight, so /chat's
# progress message says something more specific than just "Thinking..." the
# whole time a multi-step lookup is running. Falls back to a generic label
# for any tool not listed here, so a new tool added later still shows
# something reasonable instead of silently showing nothing.
TOOL_STATUS_LABELS = {
    'web_search': "🔍 Searching the web...",
    'image_search': "🖼️ Searching for images...",
    'fetch_page': "📄 Reading a page...",
    'wikipedia_summary': "📖 Checking Wikipedia...",
    'calculate': "🧮 Calculating...",
    'compare_stock_performance': "📈 Pulling stock data...",
    'stock_price_history': "📈 Pulling stock data...",
    'remember_fact': "💾 Saving that...",
}


def _tool_status_label(name: str) -> str:
    return TOOL_STATUS_LABELS.get(name, f"🔧 Using {name}...")


async def _ask_with_tools(
    question: str, history: list[dict], prior_urls: set[str], ollama_url: str, ollama_model: str,
    ollama_num_ctx: int, on_status=None, user_id=None,
) -> tuple[str, set[str], str | None]:
    """Runs the question through Ollama, always searching first rather
    than leaving that decision to the model -- model-judgment triggering
    was tried and repeatedly failed (it kept answering current-events-
    style questions from stale training data instead of searching). Uses
    image_search instead of web_search for that forced first call when
    the question is clearly asking for a picture/photo/image (see
    _wants_image) -- otherwise the model ends up with gallery/listing
    pages instead of a direct image link, and won't reliably make the
    extra tool call itself to fix that. The model can still call
    web_search/fetch_page/image_search itself afterward to refine the
    query or read a specific page. Also verifies the model's
    final "Source: <url>" citation against URLs actually seen from a tool
    this turn or an earlier one (`prior_urls`), since it has also cited
    plausible-looking URLs it never actually fetched. `history` is the
    prior visible question/answer pairs from this conversation, if any.

    user_id, if given, is used two ways: any long-term facts memory_db has
    for this user are given to the model as background context up front,
    and the model is offered a remember_fact tool (handled locally, not
    through the MCP subprocess, since it needs this same user_id) to save
    new ones -- both skipped entirely if user_id is None.

    Returns (answer, seen_urls, chart_path) -- the caller merges seen_urls
    into the conversation's remembered sources for future turns, and
    forwards chart_path (the PNG compare_stock_performance rendered, if
    any this turn) up to bot.py to attach as a real Discord file, deleting
    the local copy once sent. Spawns mcp_web_server.py fresh as a stdio
    subprocess for the duration of this call -- simplest option given
    /chat's traffic doesn't need a persistent connection.

    on_status(text), if given, is called with a short human-readable status
    ("Searching the web...", "Reading a page...", etc.) at each stage of
    the lookup, so the caller can show live progress instead of a single
    static "Thinking..." for the whole duration -- see TOOL_STATUS_LABELS."""
    server_params = StdioServerParameters(command=sys.executable, args=[MCP_SERVER_SCRIPT])

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            mcp_tools = (await session.list_tools()).tools
            extra_tools = [('remember_fact', 'fact', REMEMBER_FACT_DESCRIPTION)] if user_id is not None else []
            system_prompt, tool_param = _build_system_prompt(mcp_tools, _wants_full_list(question), extra_tools)

            seen_urls: set[str] = set(prior_urls)
            chart_path: str | None = None

            initial_tool = 'image_search' if _wants_image(question) else 'web_search'
            if on_status:
                on_status(_tool_status_label(initial_tool))
            try:
                search_result = await session.call_tool(initial_tool, {'query': question})
                search_text = "\n".join(part.text for part in search_result.content if hasattr(part, 'text'))
            except Exception as e:
                search_text = f"Search failed: {e}"
            seen_urls |= _extract_urls(search_text)

            messages = [{'role': 'system', 'content': system_prompt}]

            known_facts = memory_db.get_facts(user_id) if user_id is not None else []
            if known_facts:
                messages.append({
                    'role': 'system',
                    'content': (
                        "What you already know about this user from past conversations: "
                        + "; ".join(known_facts)
                        + ". Only bring these up if actually relevant to the current "
                        "question -- don't force them into unrelated answers."
                    ),
                })

            result_label = "Image search results" if initial_tool == 'image_search' else "Web search results"
            messages += [
                *history,
                {'role': 'user', 'content': question},
                {
                    'role': 'user',
                    'content': (
                        f"{result_label} for the question above:\n{search_text}\n\n"
                        "Answer using these if they're relevant. If they're not "
                        "relevant (e.g. this is just casual conversation), ignore "
                        "them and answer normally. Only cite a URL that actually "
                        "appears in a tool result you received this conversation -- "
                        "never one from memory."
                    ),
                },
            ]

            retried = False
            retry_query = _pick_retry_query(question, history)

            for _ in range(MAX_TOOL_ITERATIONS):
                if on_status:
                    on_status("🧠 Thinking...")
                data = _ollama_chat(messages, ollama_url, ollama_model, ollama_num_ctx, on_status)
                content = (data.get('message', {}).get('content') or '').strip()

                tool_call = _extract_tool_call(content, tool_param)
                if not tool_call:
                    if not retried and SELF_CORRECTION_RE.search(content):
                        # The model itself doesn't trust this answer -- try
                        # once more with a fresh, better-targeted search
                        # instead of just accepting "I was wrong" as final.
                        retried = True
                        retry_tool = 'image_search' if _wants_image(retry_query) else 'web_search'
                        if on_status:
                            on_status("🔁 Double-checking that...")
                        try:
                            retry_result = await session.call_tool(retry_tool, {'query': retry_query})
                            retry_text = "\n".join(
                                part.text for part in retry_result.content if hasattr(part, 'text')
                            )
                        except Exception as e:
                            retry_text = f"Search failed: {e}"
                        seen_urls |= _extract_urls(retry_text)

                        messages.append({'role': 'assistant', 'content': content})
                        messages.append({
                            'role': 'user',
                            'content': (
                                f"You weren't confident in that answer. Here are fresh "
                                f"search results for '{retry_query}':\n{retry_text}\n\n"
                                "Try again using these. If they give a clear answer, use "
                                "it; if they still don't, it's fine to honestly say you "
                                "couldn't find a reliable answer -- just don't repeat the "
                                "same unconfirmed claim."
                            ),
                        })
                        continue

                    if chart_path:
                        # compare_stock_performance/stock_price_history are
                        # told not to add a Source line -- there's no real
                        # URL for tool-computed chart data, so strip one off
                        # if the model added one anyway, rather than run it
                        # through _verify_citation and show a "couldn't
                        # verify" caveat under an otherwise fully
                        # tool-grounded, real chart.
                        return _strip_citation(content), seen_urls, chart_path

                    return _verify_citation(content, seen_urls), seen_urls, chart_path

                messages.append({'role': 'assistant', 'content': content})

                name, arg = tool_call
                if on_status:
                    on_status(_tool_status_label(name))

                if name == 'remember_fact' and user_id is not None:
                    # Handled locally, not via the MCP subprocess -- it
                    # needs this user_id, which the subprocess never has.
                    memory_db.add_fact(user_id, arg)
                    result_text = "Saved -- you'll remember this about them in future conversations too."
                else:
                    param_name = tool_param.get(name)
                    if not param_name:
                        result_text = f"Unknown tool: {name}"
                    else:
                        try:
                            result = await session.call_tool(name, {param_name: arg})
                            result_text = "\n".join(part.text for part in result.content if hasattr(part, 'text'))
                        except Exception as e:
                            result_text = f"Tool {name} failed: {e}"
                        seen_urls |= _extract_urls(result_text)
                        if param_name == 'url':
                            seen_urls.add(arg)
                        chart_match = CHART_PATH_RE.search(result_text)
                        if chart_match:
                            chart_path = chart_match.group(1)
                            result_text = CHART_PATH_RE.sub('', result_text).rstrip()

                messages.append({
                    'role': 'user',
                    'content': (
                        f"Tool result:\n{result_text}\n\n"
                        "Answer my original question using ONLY what this result "
                        "actually says -- if it conflicts with anything you thought "
                        "you knew, the result is correct, not your memory, and don't "
                        "blend in facts about a different person/thing with a "
                        "similar name. Summarize in your own words, don't repeat the "
                        "raw text back to me, and double check your answer doesn't "
                        "contradict this result before you finish. If this result "
                        "doesn't actually answer the question, say the search didn't "
                        "turn up a clear answer instead of guessing."
                    ),
                })

            return "I looked into that but couldn't settle on a final answer in time -- try asking again.", seen_urls, chart_path


def ask(question: str, conversation_id=None, on_status=None, user_id=None) -> tuple[str, str | None]:
    """Sends a question to the Ollama LLM, letting it call the web_search
    and fetch_page tools (served by mcp_web_server.py) when it needs
    current information, and returns (reply, chart_path) -- chart_path is
    the local PNG path compare_stock_performance rendered this turn, if
    any, or None. The caller (bot.py) attaches it as a Discord file and
    deletes the local copy once sent.

    The reply only includes a "Source: <url>" citation if this message
    itself asks for one (e.g. "what's your source", "give me a link") --
    otherwise it's held back from what's shown, even though the model is
    still told to work one out internally so a *later* "what was your
    source" follow-up can recall it from conversation history instead of
    needing to re-search.

    conversation_id, if given, is an opaque hashable key (bot.py uses
    (channel_id, user_id)) used to remember prior question/answer pairs
    (and every source URL seen along the way) so follow-up questions have
    context -- pass None for a one-off question with no memory.

    user_id, if given, lets the model read (and, via remember_fact, add
    to) long-term facts memory_db has stored about this specific person --
    unlike conversation_id, this follows them across channels/DMs and
    survives a bot restart, until they clear it with /forgetme.

    on_status, if given, is called (from this function's own thread) with a
    short human-readable status -- "Searching the web...", "Reading a
    page...", a queued notice if another /chat call is already talking to
    Ollama, etc. -- at each stage of the lookup, so the caller can show
    live progress instead of a single static message for the whole
    duration."""
    ollama_url = os.getenv('OLLAMA_URL', 'http://localhost:11434')
    ollama_model = os.getenv('OLLAMA_MODEL', 'gemma3:4b')
    try:
        ollama_num_ctx = int(os.getenv('OLLAMA_NUM_CTX', str(DEFAULT_OLLAMA_NUM_CTX)))
    except ValueError:
        logger.warning("OLLAMA_NUM_CTX must be an integer; using default %d", DEFAULT_OLLAMA_NUM_CTX)
        ollama_num_ctx = DEFAULT_OLLAMA_NUM_CTX
    history, prior_urls = _get_conversation(conversation_id) if conversation_id is not None else ([], set())
    try:
        full_answer, seen_urls, chart_path = asyncio.run(
            _ask_with_tools(
                question, history, prior_urls, ollama_url, ollama_model, ollama_num_ctx, on_status, user_id,
            )
        )
        full_answer = full_answer or "DJ Shinx's brain came back empty. Try rephrasing that."
        displayed_answer = full_answer if _wants_source(question) else _strip_citation(full_answer)
        if conversation_id is not None:
            updated_history = history + [
                {'role': 'user', 'content': question},
                # Keep the citation in memory even when hidden this turn,
                # so a later "what was your source" can recall it as-is.
                {'role': 'assistant', 'content': full_answer},
            ]
            _save_conversation(conversation_id, updated_history, prior_urls | seen_urls)
        return displayed_answer, chart_path
    except requests.exceptions.ConnectionError:
        return "Couldn't reach the LLM — is Ollama running on the VM and reachable from here?", None
    except requests.exceptions.Timeout:
        return "The LLM took too long to respond. Try a shorter question.", None
    except requests.exceptions.RequestException as e:
        logger.warning(f"ask() request failed: {e}")
        return f"Something went wrong talking to the LLM: {e}", None
    except Exception as e:
        detail = _describe_exception(e)
        logger.warning(f"ask() failed: {detail}")
        return f"Something went wrong talking to the LLM: {detail}", None


def chunk_response(text: str, size: int = MAX_DISCORD_LEN):
    """Splits a long reply into Discord-message-sized chunks."""
    return [text[i:i + size] for i in range(0, len(text), size)] or ['']
