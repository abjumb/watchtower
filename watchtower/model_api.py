from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from watchtower.models import AgentProfile


@dataclass(slots=True)
class ModelCallResult:
    agent_id: str
    text: str
    latency_ms: float
    total_tokens: int = 0


@dataclass(slots=True)
class ModelApiConfig:
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    gemini_api_key: str = ""
    local_base_url: str = ""

    @classmethod
    def from_env(cls) -> ModelApiConfig:
        return cls(
            openai_api_key=os.getenv("OPENAI_API_KEY", "").strip(),
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", "").strip(),
            gemini_api_key=os.getenv("GEMINI_API_KEY", "").strip(),
            local_base_url=os.getenv("WATCHTOWER_LOCAL_BASE_URL", "").strip().rstrip("/"),
        )

    def has_key(self, provider: str) -> bool:
        return bool(
            {
                "openai": self.openai_api_key,
                "anthropic": self.anthropic_api_key,
                "gemini": self.gemini_api_key,
                "local": self.local_base_url,
            }.get(provider, "")
        )

    def set_key(self, provider: str, value: str) -> None:
        value = value.strip()
        if provider == "openai":
            self.openai_api_key = value
        elif provider == "anthropic":
            self.anthropic_api_key = value
        elif provider == "gemini":
            self.gemini_api_key = value
        elif provider == "local":
            self.local_base_url = value.rstrip("/")
        else:
            raise ValueError(f"Unknown provider: {provider}")


class ModelApiClient:
    def __init__(self, config: ModelApiConfig | None = None) -> None:
        self.config = config or ModelApiConfig.from_env()

    def is_configured(self, profile: AgentProfile) -> bool:
        return self.config.has_key(profile.provider)

    async def run_task(
        self,
        profile: AgentProfile,
        prompt: str,
        on_delta: Callable[[str], None] | None = None,
    ) -> ModelCallResult:
        started = time.monotonic()
        if on_delta is not None and profile.provider in _STREAMING_PROVIDERS:
            try:
                text = await self._run_streaming(profile, prompt, on_delta)
                return ModelCallResult(profile.id, text, (time.monotonic() - started) * 1000)
            except _StreamUnavailable:
                pass  # nothing streamed yet — fall back to a normal blocking call
        text, tokens = await self._run_blocking(profile, prompt)
        return ModelCallResult(profile.id, text, (time.monotonic() - started) * 1000, tokens)

    async def _run_blocking(self, profile: AgentProfile, prompt: str) -> tuple[str, int]:
        if profile.provider == "openai":
            return await self._run_openai(profile.api_model, prompt)
        if profile.provider == "anthropic":
            return await self._run_anthropic(profile.api_model, prompt)
        if profile.provider == "gemini":
            return await self._run_gemini(profile.api_model, prompt)
        if profile.provider == "local" and self.config.local_base_url:
            return await self._run_local(profile.api_model, prompt)
        return f"{profile.display_name} is a local demo agent. No remote API call was made.", 0

    async def _run_openai(self, model: str, prompt: str) -> tuple[str, int]:
        headers = {"Authorization": f"Bearer {self.config.openai_api_key}"}
        payload = {"model": model, "input": prompt}
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post("https://api.openai.com/v1/responses", headers=headers, json=payload)
        response.raise_for_status()
        data = response.json()
        text = str(data["output_text"]) if data.get("output_text") else _extract_text(data)
        return text, _total_tokens(data)

    async def _run_anthropic(self, model: str, prompt: str) -> tuple[str, int]:
        headers = {
            "x-api-key": self.config.anthropic_api_key,
            "anthropic-version": "2023-06-01",
        }
        payload = {
            "model": model,
            "max_tokens": 800,
            "messages": [{"role": "user", "content": prompt}],
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post("https://api.anthropic.com/v1/messages", headers=headers, json=payload)
        response.raise_for_status()
        data = response.json()
        return _extract_text(data), _total_tokens(data)

    async def _run_gemini(self, model: str, prompt: str) -> tuple[str, int]:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        payload = {"contents": [{"parts": [{"text": prompt}]}]}
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(url, params={"key": self.config.gemini_api_key}, json=payload)
        response.raise_for_status()
        data = response.json()
        return _extract_text(data), _total_tokens(data)

    async def _run_local(self, model: str, prompt: str) -> tuple[str, int]:
        url = f"{self.config.local_base_url}/chat/completions"
        payload = {"model": model or "local", "messages": [{"role": "user", "content": prompt}]}
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(url, json=payload)
        response.raise_for_status()
        data = response.json()
        choices = data.get("choices") or []
        text = ""
        if choices:
            message = choices[0].get("message") or {}
            text = str(message.get("content") or "")
        return text or _extract_text(data), _total_tokens(data)

    async def _run_streaming(self, profile: AgentProfile, prompt: str, on_delta: Callable[[str], None]) -> str:
        emitted = 0

        def emit(delta: str) -> None:
            nonlocal emitted
            if delta:
                emitted += 1
                on_delta(delta)

        try:
            return await self._stream_provider(profile, prompt, emit)
        except Exception:
            if emitted:
                raise  # partial output already shown; surface as a real failure
            raise _StreamUnavailable from None

    async def _stream_provider(self, profile: AgentProfile, prompt: str, emit: Callable[[str], None]) -> str:
        provider = profile.provider
        model = profile.api_model
        headers: dict[str, str] = {}
        params: dict[str, str] | None = None
        if provider == "openai":
            url = "https://api.openai.com/v1/responses"
            headers = {"Authorization": f"Bearer {self.config.openai_api_key}"}
            payload: dict = {"model": model, "input": prompt, "stream": True}
            delta_fn = _openai_responses_delta
        elif provider == "anthropic":
            url = "https://api.anthropic.com/v1/messages"
            headers = {"x-api-key": self.config.anthropic_api_key, "anthropic-version": "2023-06-01"}
            payload = {"model": model, "max_tokens": 800, "stream": True, "messages": [{"role": "user", "content": prompt}]}
            delta_fn = _anthropic_delta
        elif provider == "gemini":
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:streamGenerateContent"
            params = {"key": self.config.gemini_api_key, "alt": "sse"}
            payload = {"contents": [{"parts": [{"text": prompt}]}]}
            delta_fn = _gemini_delta
        else:  # local OpenAI-compatible chat completions
            url = f"{self.config.local_base_url}/chat/completions"
            payload = {"model": model or "local", "messages": [{"role": "user", "content": prompt}], "stream": True}
            delta_fn = _openai_chat_delta

        timeout = httpx.Timeout(120.0, connect=5.0)
        parts: list[str] = []
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("POST", url, headers=headers, params=params, json=payload) as response:
                response.raise_for_status()
                async for raw in response.aiter_lines():
                    line = raw.strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                    except ValueError:
                        continue
                    delta = delta_fn(obj)
                    if delta:
                        parts.append(delta)
                        emit(delta)
        return "".join(parts)


def _extract_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(part for item in value if (part := _extract_text(item)))
    if not isinstance(value, dict):
        return ""

    texts: list[str] = []
    for key in ("text", "output_text"):
        text = value.get(key)
        if isinstance(text, str):
            texts.append(text)
    for key in ("content", "parts", "output", "candidates"):
        nested = value.get(key)
        if nested is not None:
            text = _extract_text(nested)
            if text:
                texts.append(text)
    return "\n".join(texts)


def _total_tokens(data: object) -> int:
    """Pull a total token count out of the differently-named provider usage blocks."""
    if not isinstance(data, dict):
        return 0
    usage = data.get("usage")
    if isinstance(usage, dict):
        if usage.get("total_tokens"):
            return int(usage["total_tokens"])
        inp = usage.get("input_tokens") or usage.get("prompt_tokens") or 0
        out = usage.get("output_tokens") or usage.get("completion_tokens") or 0
        if inp or out:
            return int(inp) + int(out)
    meta = data.get("usageMetadata")
    if isinstance(meta, dict) and meta.get("totalTokenCount"):
        return int(meta["totalTokenCount"])
    return 0


class _StreamUnavailable(Exception):
    """Raised when a streaming attempt failed before emitting any text."""


_STREAMING_PROVIDERS = {"openai", "anthropic", "gemini", "local"}


def _openai_responses_delta(obj: dict) -> str:
    if obj.get("type") == "response.output_text.delta":
        return str(obj.get("delta", ""))
    return ""


def _anthropic_delta(obj: dict) -> str:
    if obj.get("type") == "content_block_delta":
        delta = obj.get("delta") or {}
        if delta.get("type") == "text_delta":
            return str(delta.get("text", ""))
    return ""


def _gemini_delta(obj: dict) -> str:
    candidates = obj.get("candidates")
    return _extract_text(candidates) if candidates is not None else ""


def _openai_chat_delta(obj: dict) -> str:
    choices = obj.get("choices") or []
    if choices:
        delta = choices[0].get("delta") or {}
        return str(delta.get("content") or "")
    return ""
