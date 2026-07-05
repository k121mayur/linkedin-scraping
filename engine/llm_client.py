"""Pluggable LLM client supporting OpenAI, Ollama (x-api-key), and Anthropic."""

from __future__ import annotations

from typing import Optional
import json
import time
import urllib.request
import urllib.error
from config import LLM_PROVIDER, LLM_API_KEY, LLM_BASE_URL, LLM_MODEL

# Transient HTTP statuses worth retrying (incl. Cloudflare tunnel/origin errors).
_RETRY_CODES = {429, 500, 502, 503, 504, 520, 521, 522, 523, 524, 530}


def _urlopen_json(req, timeout: int, attempts: int = 3):
    """POST with retry/backoff on transient errors; returns parsed JSON."""
    last_err = None
    for i in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code in _RETRY_CODES and i < attempts - 1:
                time.sleep(2 * (i + 1))
                continue
            raise
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = e
            if i < attempts - 1:
                time.sleep(2 * (i + 1))
                continue
            raise
    if last_err:
        raise last_err

# Some endpoints (e.g. the Silicon Mango Ollama reverse proxy) reject the default
# "Python-urllib/x" User-Agent with HTTP 403, so we present a browser-like UA.
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


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
            "User-Agent": _USER_AGENT,
        },
    )
    data = _urlopen_json(req, timeout=120)
    return data["choices"][0]["message"]["content"]


def _is_real_key(key: str) -> bool:
    """True only when a genuine API key is configured (not blank/placeholder)."""
    return bool(key) and key.strip().lower() not in {
        "", "your-x-api-key", "change-me", "change-me-key", "none", "null",
    }


def _call_ollama(messages: list[dict], temperature: float = 0.1) -> str:
    base = LLM_BASE_URL.rstrip("/")
    # gemma4:31b is a "thinking" model; its reasoning phase adds huge latency
    # (~90s for a 10-job batch). Disabling it keeps the answer quality while
    # making relevance/parse calls fast enough for an interactive run.
    body = json.dumps({
        "model": LLM_MODEL,
        "messages": messages,
        "stream": False,
        "think": False,
        "options": {"temperature": temperature},
    }).encode()
    headers = {"Content-Type": "application/json", "User-Agent": _USER_AGENT}
    # The Silicon Mango Ollama endpoint is keyless; only send the header when a
    # genuine key is configured (other deployments may gate on x-api-key).
    if _is_real_key(LLM_API_KEY):
        headers["x-api-key"] = LLM_API_KEY
    req = urllib.request.Request(f"{base}/api/chat", data=body, headers=headers)
    data = _urlopen_json(req, timeout=180)
    # gemma is a "thinking" model: the answer is in message.content (reasoning is
    # in a separate message.thinking field we deliberately ignore).
    return data.get("message", {}).get("content", "")


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
            "User-Agent": _USER_AGENT,
        },
    )
    data = _urlopen_json(req, timeout=120)
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


def _extract_json(text: str):
    """Parse JSON from a model response, tolerating fences and stray prose.

    Tries a direct parse first; otherwise extracts the first balanced JSON
    object or array found in the text. Raises ValueError if none is parseable.
    """
    text = (text or "").strip()
    # Strip markdown code fences if present (```json ... ```)
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

    try:
        return json.loads(text)
    except Exception:
        pass

    # Fall back to scanning for the first balanced {...} or [...] block.
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        start = text.find(open_ch)
        if start == -1:
            continue
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == open_ch:
                depth += 1
            elif ch == close_ch:
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except Exception:
                        break
    raise ValueError("No parseable JSON found in model response")


def _vision_openai(prompt: str, images_b64: list[str], temperature: float) -> str:
    content = [{"type": "text", "text": prompt}]
    for b64 in images_b64:
        content.append({"type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
    body = json.dumps({
        "model": LLM_MODEL,
        "messages": [{"role": "user", "content": content}],
        "temperature": temperature,
    }).encode()
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {LLM_API_KEY}",
            "User-Agent": _USER_AGENT,
        },
    )
    data = _urlopen_json(req, timeout=180)
    return data["choices"][0]["message"]["content"]


def _vision_ollama(prompt: str, images_b64: list[str], temperature: float) -> str:
    # Ollama multimodal: base64 images ride on the user message's "images" field.
    base = LLM_BASE_URL.rstrip("/")
    body = json.dumps({
        "model": LLM_MODEL,
        "messages": [{"role": "user", "content": prompt, "images": images_b64}],
        "stream": False,
        "think": False,
        "options": {"temperature": temperature},
    }).encode()
    headers = {"Content-Type": "application/json", "User-Agent": _USER_AGENT}
    if _is_real_key(LLM_API_KEY):
        headers["x-api-key"] = LLM_API_KEY
    req = urllib.request.Request(f"{base}/api/chat", data=body, headers=headers)
    data = _urlopen_json(req, timeout=240)
    return data.get("message", {}).get("content", "")


def _vision_anthropic(prompt: str, images_b64: list[str], temperature: float) -> str:
    content = []
    for b64 in images_b64:
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": b64},
        })
    content.append({"type": "text", "text": prompt})
    body = json.dumps({
        "model": LLM_MODEL,
        "messages": [{"role": "user", "content": content}],
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
            "User-Agent": _USER_AGENT,
        },
    )
    data = _urlopen_json(req, timeout=180)
    return data["content"][0]["text"]


_VISION_PROVIDERS = {
    "openai": _vision_openai,
    "ollama": _vision_ollama,
    "anthropic": _vision_anthropic,
}


def chat_vision(prompt: str, images_b64: list[str], temperature: float = 0.1) -> str:
    """Send text + base64 JPEG/PNG images to the model; return the text answer.

    Used to read text out of images attached to LinkedIn posts. Raises on
    provider/network failure — callers wrap this in try/except like every
    other LLM boundary.
    """
    caller = _VISION_PROVIDERS.get(LLM_PROVIDER)
    if caller is None:
        raise ValueError(f"Unsupported LLM_PROVIDER for vision: {LLM_PROVIDER}")
    return caller(prompt, images_b64, temperature)


def chat_json(prompt: str, system: Optional[str] = None, temperature: float = 0.1):
    """Send a chat request and return parsed JSON (object or list).

    For Ollama, appends a JSON-only instruction to the system prompt since
    Ollama doesn't support response_format natively.
    """
    if LLM_PROVIDER == "ollama":
        system = (system or "") + "\nRespond with ONLY valid JSON. No markdown, no explanation, no preamble."
    text = chat_text(prompt, system, temperature)
    return _extract_json(text)
