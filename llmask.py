import os
import sys
import json
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

SYSTEM_PROMPT = (
    "You can call the web_search and fetch_page tools to look up current "
    "information before answering. Use them when the question needs facts "
    "you're not confident about, then give a clear, direct final answer -- "
    "don't mention the tools themselves in your reply."
)


def _ollama_chat(messages: list[dict], tools: list[dict], ollama_url: str, ollama_model: str) -> dict:
    response = requests.post(
        f'{ollama_url}/api/chat',
        json={'model': ollama_model, 'messages': messages, 'tools': tools, 'stream': False},
        timeout=OLLAMA_TIMEOUT,
    )
    response.raise_for_status()
    return response.json()


def _parse_tool_args(raw_args) -> dict:
    if isinstance(raw_args, str):
        try:
            return json.loads(raw_args)
        except json.JSONDecodeError:
            return {}
    return raw_args or {}


def _describe_exception(e: BaseException) -> str:
    """Unwraps ExceptionGroups -- anyio's TaskGroup (used internally by the
    MCP client) wraps whatever actually failed in one, and printing the
    group itself just says "unhandled errors in a TaskGroup" with no
    detail. This digs out the real underlying error(s) instead."""
    if isinstance(e, BaseExceptionGroup):
        return "; ".join(_describe_exception(sub) for sub in e.exceptions)
    return f"{type(e).__name__}: {e}"


async def _ask_with_tools(question: str, ollama_url: str, ollama_model: str) -> str:
    """Runs the question through Ollama, giving it the MCP server's tools
    to call (web_search, fetch_page) before it settles on a final answer.
    Spawns mcp_web_server.py fresh as a stdio subprocess for the duration
    of this call -- simplest option given /ask's traffic doesn't need a
    persistent connection."""
    server_params = StdioServerParameters(command=sys.executable, args=[MCP_SERVER_SCRIPT])

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            mcp_tools = (await session.list_tools()).tools
            tools = [
                {
                    'type': 'function',
                    'function': {
                        'name': t.name,
                        'description': t.description or '',
                        'parameters': t.input_schema,
                    },
                }
                for t in mcp_tools
            ]

            messages = [
                {'role': 'system', 'content': SYSTEM_PROMPT},
                {'role': 'user', 'content': question},
            ]

            for _ in range(MAX_TOOL_ITERATIONS):
                data = _ollama_chat(messages, tools, ollama_url, ollama_model)
                message = data.get('message', {})
                tool_calls = message.get('tool_calls')

                if not tool_calls:
                    return (message.get('content') or '').strip()

                messages.append(message)
                for call in tool_calls:
                    function = call.get('function', {})
                    name = function.get('name')
                    args = _parse_tool_args(function.get('arguments'))
                    try:
                        result = await session.call_tool(name, args)
                        result_text = "\n".join(part.text for part in result.content if hasattr(part, 'text'))
                    except Exception as e:
                        result_text = f"Tool {name} failed: {e}"
                    messages.append({'role': 'tool', 'content': result_text})

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
