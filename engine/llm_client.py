"""Pluggable LLM client supporting OpenAI, Ollama (x-api-key), and Anthropic."""

from __future__ import annotations

from typing import Optional
import json
import urllib.request
import urllib.error
from config import LLM_PROVIDER, LLM_API_KEY, LLM_BASE_URL, LLM_MODEL


def _call_openai(messages: list[dict], temperature: float = 0.1) -> str:
    body = json.dumps({
        "model": LLM_MODEL,
        "messages": messages,
        "temperature": temperature,
        "response_format": {"type": "json_object"},
    }).encode()
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {LLM_API_KEY}",
        },
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read())
    return data["choices"][0]["message"]["content"]


def _call_ollama(messages: list[dict], temperature: float = 0.1) -> str:
    base = LLM_BASE_URL.rstrip("/")
    body = json.dumps({
        "model": LLM_MODEL,
        "messages": messages,
        "stream": False,
        "options": {"temperature": temperature},
    }).encode()
    req = urllib.request.Request(
        f"{base}/api/chat",
        data=body,
        headers={
            "Content-Type": "application/json",
            "x-api-key": LLM_API_KEY,
        },
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        data = json.loads(resp.read())
    return data["message"]["content"]


def _call_anthropic(messages: list[dict], temperature: float = 0.1) -> str:
    # Convert OpenAI-style messages to Anthropic format
    system = ""
    anthropic_msgs = []
    for m in messages:
        if m["role"] == "system":
            system = m["content"]
        else:
            anthropic_msgs.append({"role": m["role"], "content": m["content"]})

    body = json.dumps({
        "model": LLM_MODEL,
        "messages": anthropic_msgs,
        "system": system,
        "max_tokens": 4096,
        "temperature": temperature,
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "Content-Type": "application/json",
            "x-api-key": LLM_API_KEY,
            "anthropic-version": "2023-06-01",
        },
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read())
    return data["content"][0]["text"]


# Provider dispatch map
_PROVIDERS = {
    "openai": _call_openai,
    "ollama": _call_ollama,
    "anthropic": _call_anthropic,
}


def chat_text(prompt: str, system: Optional[str] = None, temperature: float = 0.1) -> str:
    """Send a chat request and return raw text response."""
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    caller = _PROVIDERS.get(LLM_PROVIDER)
    if caller is None:
        raise ValueError(f"Unsupported LLM_PROVIDER: {LLM_PROVIDER}")
    return caller(messages, temperature)


def chat_json(prompt: str, system: Optional[str] = None, temperature: float = 0.1) -> dict:
    """Send a chat request and return parsed JSON response.

    For Ollama, appends a JSON-only instruction to the system prompt since
    Ollama doesn't support response_format natively.
    """
    if LLM_PROVIDER == "ollama":
        system = (system or "") + "\nRespond with ONLY valid JSON. No markdown, no explanation."
    text = chat_text(prompt, system, temperature)
    # Strip markdown code fences if present
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        if text.endswith("```"):
            text = text[:-3]
    return json.loads(text)
