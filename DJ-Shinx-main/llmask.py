import os
import requests

# Point this at the Ollama server via OLLAMA_URL/OLLAMA_MODEL in code.env.
# Read lazily (not at import time) since bot.py loads code.env after importing this module.
MAX_DISCORD_LEN = 2000


def ask(question: str) -> str:
    """Sends a question to the Ollama LLM and returns its reply as a string."""
    ollama_url = os.getenv('OLLAMA_URL', 'http://localhost:11434')
    ollama_model = os.getenv('OLLAMA_MODEL', 'llama3')
    try:
        response = requests.post(
            f'{ollama_url}/api/generate',
            json={
                'model': ollama_model,
                'prompt': question,
                'stream': False,
            },
            timeout=120,
        )
        response.raise_for_status()
        data = response.json()
        answer = data.get('response', '').strip()
        return answer or "DJ Shinx's brain came back empty. Try rephrasing that."
    except requests.exceptions.ConnectionError:
        return "Couldn't reach the LLM — is Ollama running on the VM and reachable from here?"
    except requests.exceptions.Timeout:
        return "The LLM took too long to respond. Try a shorter question."
    except requests.exceptions.RequestException as e:
        return f"Something went wrong talking to the LLM: {e}"


def chunk_response(text: str, size: int = MAX_DISCORD_LEN):
    """Splits a long reply into Discord-message-sized chunks."""
    return [text[i:i + size] for i in range(0, len(text), size)] or ['']
