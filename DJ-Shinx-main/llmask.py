import os
import requests

# Point this at the Ollama server running on the Linux Mint VM, e.g. http://192.168.1.50:11434
# Ollama only listens on localhost by default, so on the VM you need to set:
#   OLLAMA_HOST=0.0.0.0:11434
# before starting `ollama serve` (or in its systemd unit), and make sure the VM's
# firewall / network mode (bridged, not NAT-only) actually lets this machine reach it.
OLLAMA_URL = os.getenv('OLLAMA_URL', 'http://localhost:11434')
OLLAMA_MODEL = os.getenv('OLLAMA_MODEL', 'llama3')

MAX_DISCORD_LEN = 2000


def ask(question: str) -> str:
    """Sends a question to the Ollama LLM and returns its reply as a string."""
    try:
        response = requests.post(
            f'{OLLAMA_URL}/api/generate',
            json={
                'model': OLLAMA_MODEL,
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
