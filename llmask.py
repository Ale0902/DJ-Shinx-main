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

logger = logging.getLogger(__name__)

# Point this at the Ollama server via OLLAMA_URL/OLLAMA_MODEL in code.env.
# Read lazily (not at import time) since bot.py loads code.env after importing this module.
MAX_DISCORD_LEN = 2000

# Each iteration is one Ollama round-trip; a typical "search, maybe fetch a
# page, then answer" exchange takes 2-3, so this caps worst-case latency
# without cutting off legitimate multi-step lookups.
MAX_TOOL_ITERATIONS = 4
# A single Ollama call can legitimately take a while on a 12B model split
# across modest GPUs, especially with a lot of context (conversation
# history + search results) or a longer response to generate -- 120s was
# too tight and cut off an otherwise-successful generation.
OLLAMA_TIMEOUT = 180

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
TOOL_CALL_RE = re.compile(r'TOOL_CALL:\s*(\w+)\(\s*["\']([^"\']*)["\']\s*\)')

# For verifying the model's final "Source: <url>" citation against URLs it
# actually saw from a tool this turn, rather than trusting it not to cite
# something recalled from memory (it has, more than once).
URL_RE = re.compile(r'https?://\S+')
SOURCE_LINE_RE = re.compile(r'^Source:\s*(\S+)\s*$', re.MULTILINE)


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
# as a separate follow-up.
SOURCE_REQUEST_RE = re.compile(
    r'\b(source|sources|link|links|url|urls|cite|citation|reference|proof|prove it)\b',
    re.IGNORECASE,
)


def _wants_source(question: str) -> bool:
    return bool(SOURCE_REQUEST_RE.search(question))


def _strip_citation(content: str) -> str:
    return SOURCE_LINE_RE.sub('', content).rstrip()


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


def _ollama_chat(messages: list[dict], ollama_url: str, ollama_model: str, on_queued=None) -> dict:
    """on_queued(), if given, is called at most once if this call has to
    wait for another in-flight request to finish first -- lets the caller
    tell the user they're queued instead of leaving them sat waiting with
    no explanation."""
    if not _ollama_lock.acquire(blocking=False):
        if on_queued:
            on_queued()
        _ollama_lock.acquire()  # now block until it's actually our turn

    try:
        response = requests.post(
            f'{ollama_url}/api/chat',
            json={'model': ollama_model, 'messages': messages, 'stream': False},
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


def _build_system_prompt(mcp_tools) -> tuple[str, dict[str, str]]:
    """Returns (system_prompt, {tool_name: its single string param name}).
    Both of the MCP server's tools (web_search, fetch_page) take exactly
    one string argument, so the param name is read straight off each
    tool's JSON schema instead of being hardcoded here."""
    tool_param = {}
    tool_lines = []
    for t in mcp_tools:
        props = (t.input_schema or {}).get('properties', {})
        param_name = next(iter(props), 'value')
        tool_param[t.name] = param_name
        tool_lines.append(f'- {t.name}("{param_name}") -- {_tool_description(t)}')

    system_prompt = (
        "You are Agent Shinx, a Discord bot that works like a quick search "
        "engine: give a short, direct summary that answers the question -- "
        "1-3 sentences for most questions, more only if it genuinely needs "
        "detail. Plain, direct tone -- not overly casual, not full of "
        "slang or emoji.\n\n"
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
        "text, no prefix, and don't mention that you searched."
    )
    return system_prompt, tool_param


async def _ask_with_tools(
    question: str, history: list[dict], prior_urls: set[str], ollama_url: str, ollama_model: str, on_queued=None
) -> tuple[str, set[str]]:
    """Runs the question through Ollama, always searching the web first
    rather than leaving that decision to the model -- model-judgment
    triggering was tried and repeatedly failed (it kept answering current-
    events-style questions from stale training data instead of searching).
    The model can still call web_search/fetch_page itself afterward to
    refine the query or read a specific page. Also verifies the model's
    final "Source: <url>" citation against URLs actually seen from a tool
    this turn or an earlier one (`prior_urls`), since it has also cited
    plausible-looking URLs it never actually fetched. `history` is the
    prior visible question/answer pairs from this conversation, if any.
    Returns (answer, seen_urls) -- the caller merges seen_urls into the
    conversation's remembered sources for future turns. Spawns
    mcp_web_server.py fresh as a stdio subprocess for the duration of this
    call -- simplest option given /chat's traffic doesn't need a
    persistent connection."""
    server_params = StdioServerParameters(command=sys.executable, args=[MCP_SERVER_SCRIPT])

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            mcp_tools = (await session.list_tools()).tools
            system_prompt, tool_param = _build_system_prompt(mcp_tools)

            seen_urls: set[str] = set(prior_urls)

            try:
                search_result = await session.call_tool('web_search', {'query': question})
                search_text = "\n".join(part.text for part in search_result.content if hasattr(part, 'text'))
            except Exception as e:
                search_text = f"Search failed: {e}"
            seen_urls |= _extract_urls(search_text)

            messages = [
                {'role': 'system', 'content': system_prompt},
                *history,
                {'role': 'user', 'content': question},
                {
                    'role': 'user',
                    'content': (
                        f"Web search results for the question above:\n{search_text}\n\n"
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
                data = _ollama_chat(messages, ollama_url, ollama_model, on_queued)
                content = (data.get('message', {}).get('content') or '').strip()

                match = TOOL_CALL_RE.search(content)
                if not match:
                    if not retried and SELF_CORRECTION_RE.search(content):
                        # The model itself doesn't trust this answer -- try
                        # once more with a fresh, better-targeted search
                        # instead of just accepting "I was wrong" as final.
                        retried = True
                        try:
                            retry_result = await session.call_tool('web_search', {'query': retry_query})
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

                    return _verify_citation(content, seen_urls), seen_urls

                messages.append({'role': 'assistant', 'content': content})

                name, arg = match.group(1), match.group(2)
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

            return "I looked into that but couldn't settle on a final answer in time -- try asking again.", seen_urls


def ask(question: str, conversation_id=None, on_queued=None) -> str:
    """Sends a question to the Ollama LLM, letting it call the web_search
    and fetch_page tools (served by mcp_web_server.py) when it needs
    current information, and returns its final reply as a string.

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

    on_queued, if given, is called (from this function's own thread) if
    another /chat call is already talking to Ollama, so the caller can let
    the user know they're waiting in line instead of just sitting there."""
    ollama_url = os.getenv('OLLAMA_URL', 'http://localhost:11434')
    ollama_model = os.getenv('OLLAMA_MODEL', 'gemma3:4b')
    history, prior_urls = _get_conversation(conversation_id) if conversation_id is not None else ([], set())
    try:
        full_answer, seen_urls = asyncio.run(
            _ask_with_tools(question, history, prior_urls, ollama_url, ollama_model, on_queued)
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
        return displayed_answer
    except requests.exceptions.ConnectionError:
        return "Couldn't reach the LLM — is Ollama running on the VM and reachable from here?"
    except requests.exceptions.Timeout:
        return "The LLM took too long to respond. Try a shorter question."
    except requests.exceptions.RequestException as e:
        logger.warning(f"ask() request failed: {e}")
        return f"Something went wrong talking to the LLM: {e}"
    except Exception as e:
        detail = _describe_exception(e)
        logger.warning(f"ask() failed: {detail}")
        return f"Something went wrong talking to the LLM: {detail}"


def chunk_response(text: str, size: int = MAX_DISCORD_LEN):
    """Splits a long reply into Discord-message-sized chunks."""
    return [text[i:i + size] for i in range(0, len(text), size)] or ['']
