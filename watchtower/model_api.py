from __future__ import annotations

import os
import time
from dataclasses import dataclass

import httpx

from watchtower.models import AgentProfile


@dataclass(slots=True)
class ModelCallResult:
    agent_id: str
    text: str
    latency_ms: float


@dataclass(slots=True)
class ModelApiConfig:
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    gemini_api_key: str = ""

    @classmethod
    def from_env(cls) -> ModelApiConfig:
        return cls(
            openai_api_key=os.getenv("OPENAI_API_KEY", "").strip(),
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", "").strip(),
            gemini_api_key=os.getenv("GEMINI_API_KEY", "").strip(),
        )

    def has_key(self, provider: str) -> bool:
        return bool(
            {
                "openai": self.openai_api_key,
                "anthropic": self.anthropic_api_key,
                "gemini": self.gemini_api_key,
            }.get(provider, "")
        )


class ModelApiClient:
    def __init__(self, config: ModelApiConfig | None = None) -> None:
        self.config = config or ModelApiConfig.from_env()

    def is_configured(self, profile: AgentProfile) -> bool:
        return self.config.has_key(profile.provider)

    async def run_task(self, profile: AgentProfile, prompt: str) -> ModelCallResult:
        started = time.monotonic()
        if profile.provider == "openai":
            text = await self._run_openai(profile.api_model, prompt)
        elif profile.provider == "anthropic":
            text = await self._run_anthropic(profile.api_model, prompt)
        elif profile.provider == "gemini":
            text = await self._run_gemini(profile.api_model, prompt)
        else:
            text = f"{profile.display_name} is a local demo agent. No remote API call was made."
        return ModelCallResult(
            agent_id=profile.id,
            text=text,
            latency_ms=(time.monotonic() - started) * 1000,
        )

    async def _run_openai(self, model: str, prompt: str) -> str:
        headers = {"Authorization": f"Bearer {self.config.openai_api_key}"}
        payload = {"model": model, "input": prompt}
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post("https://api.openai.com/v1/responses", headers=headers, json=payload)
        response.raise_for_status()
        data = response.json()
        if data.get("output_text"):
            return str(data["output_text"])
        return _extract_text(data)

    async def _run_anthropic(self, model: str, prompt: str) -> str:
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
        return _extract_text(response.json())

    async def _run_gemini(self, model: str, prompt: str) -> str:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        payload = {"contents": [{"parts": [{"text": prompt}]}]}
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(url, params={"key": self.config.gemini_api_key}, json=payload)
        response.raise_for_status()
        return _extract_text(response.json())


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
