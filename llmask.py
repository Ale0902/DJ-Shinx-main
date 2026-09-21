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
OLLAMA_TIMEOUT = 120

BASE = os.path.dirname(os.path.abspath(__file__))
MCP_SERVER_SCRIPT = os.path.join(BASE, 'mcp_web_server.py')

# In-memory conversation history, keyed by whatever the caller passes as a
# conversation_id (bot.py uses (channel_id, user_id), so each person's
# conversation in a given channel is independent). Only holds the visible
# question/answer pairs -- not each turn's internal TOOL_CALL/tool-result
# scaffolding, so it doesn't balloon with raw search dumps. Cleared on bot
# restart; that's fine, this is meant to feel like a chat session, not a
# permanent record.
CONVERSATION_TTL_SECONDS = 20 * 60  # idle this long and the next /ask starts fresh
MAX_HISTORY_TURNS = 8  # question/answer pairs kept; oldest dropped first

_conversations: dict = {}
_conversations_lock = threading.Lock()


def _get_history(conversation_id) -> list[dict]:
    with _conversations_lock:
        entry = _conversations.get(conversation_id)
        if not entry:
            return []
        last_used, history = entry
        if time.monotonic() - last_used > CONVERSATION_TTL_SECONDS:
            del _conversations[conversation_id]
            return []
        return list(history)


def _save_history(conversation_id, history: list[dict]) -> None:
    trimmed = history[-(MAX_HISTORY_TURNS * 2):]
    with _conversations_lock:
        _conversations[conversation_id] = (time.monotonic(), trimmed)


def forget(conversation_id) -> None:
    """Clears a conversation's history -- backs the /forget command."""
    with _conversations_lock:
        _conversations.pop(conversation_id, None)

# Ollama's native `tools` API (structured tool_calls) isn't supported by every
# model's chat template -- e.g. gemma3 rejects a request outright (400) if
# `tools` is even present. This prompts the model to request a tool by
# writing a specific line of text instead, which works with any chat model
# regardless of native tool-calling support.
TOOL_CALL_RE = re.compile(r'TOOL_CALL:\s*(\w+)\(\s*["\']([^"\']*)["\']\s*\)')


def _ollama_chat(messages: list[dict], ollama_url: str, ollama_model: str) -> dict:
    response = requests.post(
        f'{ollama_url}/api/chat',
        json={'model': ollama_model, 'messages': messages, 'stream': False},
        timeout=OLLAMA_TIMEOUT,
    )
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
        description = (t.description or '').strip().splitlines()[0] if t.description else ''
        tool_lines.append(f'- {t.name}("{param_name}") -- {description}')

    system_prompt = (
        "You are DJ Shinx, a Discord bot. Answer in a normal, direct "
        "conversational tone -- not overly casual, not full of slang or "
        "emoji, just a clear and accurate answer.\n\n"
        "You can look up information using these tools:\n"
        + "\n".join(tool_lines) +
        '\n\nTo use one, reply with EXACTLY one line in this form and nothing else:\n'
        'TOOL_CALL: tool_name("argument")\n\n'
        "Use a tool whenever the question is about current events, news, "
        "or anything that changes over time (scores, prices, schedules, "
        "who currently holds a role or record) -- your training data has a "
        "cutoff, so don't rely on it for anything that could be outdated. "
        "Also use one for any specific fact (names, dates, numbers, what "
        "happened in an incident) you aren't fully certain of. For general "
        "knowledge you're confident and certain about, answer directly "
        "without searching.\n\n"
        "Never guess or invent specific facts, names, dates, or sources -- "
        "if you don't actually have information to support a claim, say so "
        "honestly instead of making something up, including if asked for a "
        "source you don't have.\n\n"
        "Summarize what you found in your own words, not pasted verbatim, "
        "and end with the source URL on its own line, like 'Source: <url>', "
        "when you used one. Keep the rest of the answer short and direct, "
        "as plain text with no prefix, and don't mention that you searched."
    )
    return system_prompt, tool_param


async def _ask_with_tools(question: str, history: list[dict], ollama_url: str, ollama_model: str) -> str:
    """Runs the question through Ollama, prompting it to request the MCP
    server's tools (web_search, fetch_page) by name when it needs current
    or uncertain information before settling on a final answer -- the
    model decides whether a question warrants a search rather than one
    happening automatically every time, to avoid burning a search (and a
    few seconds of latency) on every casual message. `history` is the
    prior visible question/answer pairs from this conversation, if any.
    Spawns mcp_web_server.py fresh as a stdio subprocess for the duration
    of this call -- simplest option given /ask's traffic doesn't need a
    persistent connection."""
    server_params = StdioServerParameters(command=sys.executable, args=[MCP_SERVER_SCRIPT])

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            mcp_tools = (await session.list_tools()).tools
            system_prompt, tool_param = _build_system_prompt(mcp_tools)

            messages = [
                {'role': 'system', 'content': system_prompt},
                *history,
                {'role': 'user', 'content': question},
            ]

            for _ in range(MAX_TOOL_ITERATIONS):
                data = _ollama_chat(messages, ollama_url, ollama_model)
                content = (data.get('message', {}).get('content') or '').strip()

                match = TOOL_CALL_RE.search(content)
                if not match:
                    return content

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

                messages.append({
                    'role': 'user',
                    'content': (
                        f"Tool result:\n{result_text}\n\n"
                        "Based only on this, answer my original question in your own "
                        "words -- don't just repeat the raw text back to me. If this "
                        "result doesn't actually answer the question, say the search "
                        "didn't turn up a clear answer instead of guessing."
                    ),
                })

            return "I looked into that but couldn't settle on a final answer in time -- try asking again."


def ask(question: str, conversation_id=None) -> str:
    """Sends a question to the Ollama LLM, letting it call the web_search
    and fetch_page tools (served by mcp_web_server.py) when it needs
    current information, and returns its final reply as a string.

    conversation_id, if given, is an opaque hashable key (bot.py uses
    (channel_id, user_id)) used to remember prior question/answer pairs so
    follow-up questions have context -- pass None for a one-off question
    with no memory."""
    ollama_url = os.getenv('OLLAMA_URL', 'http://localhost:11434')
    ollama_model = os.getenv('OLLAMA_MODEL', 'gemma3:4b')
    history = _get_history(conversation_id) if conversation_id is not None else []
    try:
        answer = asyncio.run(_ask_with_tools(question, history, ollama_url, ollama_model))
        answer = answer or "DJ Shinx's brain came back empty. Try rephrasing that."
        if conversation_id is not None:
            updated = history + [
                {'role': 'user', 'content': question},
                {'role': 'assistant', 'content': answer},
            ]
            _save_history(conversation_id, updated)
        return answer
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
