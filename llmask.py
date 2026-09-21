import os
import sys
import re
import logging
import asyncio
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
        "You are DJ Shinx, a friendly Discord bot chatting with server members. "
        "Answer naturally and conversationally, like you're texting a friend -- "
        "short and direct, not like a search engine or a formal report.\n\n"
        "You can look up current information using these tools before answering:\n"
        + "\n".join(tool_lines) +
        '\n\nTo use one, reply with EXACTLY one line in this form and nothing else:\n'
        'TOOL_CALL: tool_name("argument")\n\n'
        "Only do this when the question needs facts you're not confident about. "
        "Once you've looked something up, weave what you learned into your own "
        "words -- never paste raw search results, links, or page text back "
        "verbatim. Give a short, direct final answer as plain text with no "
        "prefix, and don't mention that you used any tools."
    )
    return system_prompt, tool_param


async def _ask_with_tools(question: str, ollama_url: str, ollama_model: str) -> str:
    """Runs the question through Ollama, prompting it to request the MCP
    server's tools (web_search, fetch_page) by name before it settles on a
    final answer. Spawns mcp_web_server.py fresh as a stdio subprocess for
    the duration of this call -- simplest option given /ask's traffic
    doesn't need a persistent connection."""
    server_params = StdioServerParameters(command=sys.executable, args=[MCP_SERVER_SCRIPT])

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            mcp_tools = (await session.list_tools()).tools
            system_prompt, tool_param = _build_system_prompt(mcp_tools)

            messages = [
                {'role': 'system', 'content': system_prompt},
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
                        "Now answer my original question in your own words based on "
                        "this -- summarize naturally, don't just repeat the raw text "
                        "back to me."
                    ),
                })

            return "I looked into that but couldn't settle on a final answer in time -- try asking again."


def ask(question: str) -> str:
    """Sends a question to the Ollama LLM, letting it call the web_search
    and fetch_page tools (served by mcp_web_server.py) when it needs
    current information, and returns its final reply as a string."""
    ollama_url = os.getenv('OLLAMA_URL', 'http://localhost:11434')
    ollama_model = os.getenv('OLLAMA_MODEL', 'gemma3:4b')
    try:
        answer = asyncio.run(_ask_with_tools(question, ollama_url, ollama_model))
        return answer or "DJ Shinx's brain came back empty. Try rephrasing that."
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
