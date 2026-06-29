from __future__ import annotations

import asyncio
import math
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass

try:
    import httpx
except ImportError:  # pragma: no cover - the browser/WASM demo build ships without httpx
    httpx = None  # type: ignore[assignment]

from watchtower.auth import AuthConfig
from watchtower.models import AgentMetrics, AgentProfile, AgentStatus, AgentTelemetry


@dataclass(slots=True)
class ProviderSnapshot:
    telemetry: dict[str, AgentTelemetry]
    auth_mode: str
    source_label: str
    last_error: str = ""


class AgentDataProvider:
    def __init__(self, auth_config: AuthConfig | None = None) -> None:
        self.auth_config = auth_config or AuthConfig.from_env()
        self._started = time.monotonic()

    def configure(self, auth_config: AuthConfig) -> None:
        self.auth_config = auth_config

    async def fetch_all(self, profiles: Sequence[AgentProfile]) -> ProviderSnapshot:
        if not self.auth_config.is_remote_enabled:
            return self._demo_snapshot(profiles)

        try:
            timeout = httpx.Timeout(2.5, connect=1.0)
            async with httpx.AsyncClient(
                base_url=self.auth_config.api_base_url,
                headers=self.auth_config.headers(),
                timeout=timeout,
            ) as client:
                results = await asyncio.gather(
                    *(self._fetch_one(client, profile) for profile in profiles),
                    return_exceptions=True,
                )
        except httpx.HTTPError as exc:
            snapshot = self._demo_snapshot(profiles)
            snapshot.last_error = f"remote unavailable: {exc}"
            return snapshot

        telemetry: dict[str, AgentTelemetry] = {}
        errors: list[str] = []
        for profile, result in zip(profiles, results, strict=True):
            if isinstance(result, Exception):
                errors.append(f"{profile.id}: {result}")
                telemetry[profile.id] = self._demo_telemetry(profile, status=AgentStatus.DEGRADED)
            else:
                telemetry[profile.id] = result

        return ProviderSnapshot(
            telemetry=telemetry,
            auth_mode=self.auth_config.mode,
            source_label=self.auth_config.api_base_url,
            last_error="; ".join(errors),
        )

    async def _fetch_one(self, client: httpx.AsyncClient, profile: AgentProfile) -> AgentTelemetry:
        response = await client.get(f"/agents/{profile.id}/telemetry")
        response.raise_for_status()
        payload = response.json()
        metrics_payload = payload.get("metrics", payload)
        metrics = AgentMetrics(
            load=float(metrics_payload.get("load", 0.0)),
            latency_ms=float(metrics_payload.get("latency_ms", 0.0)),
            tokens_per_minute=float(metrics_payload.get("tokens_per_minute", 0.0)),
            error_rate=float(metrics_payload.get("error_rate", 0.0)),
            active_tasks=int(metrics_payload.get("active_tasks", 0)),
        ).normalized()
        status_value = str(payload.get("status", AgentStatus.IDLE.value))
        status = AgentStatus(status_value) if status_value in AgentStatus._value2member_map_ else AgentStatus.IDLE
        return AgentTelemetry(
            agent_id=profile.id,
            metrics=metrics,
            status=status,
            message=str(payload.get("message", "")),
            raw=payload,
        )

    def _demo_snapshot(self, profiles: Sequence[AgentProfile]) -> ProviderSnapshot:
        return ProviderSnapshot(
            telemetry={profile.id: self._demo_telemetry(profile) for profile in profiles},
            auth_mode=self.auth_config.mode,
            source_label="local demo telemetry",
        )

    def demo_snapshot(self, profiles: Sequence[AgentProfile]) -> ProviderSnapshot:
        """Synchronous demo telemetry snapshot (no thread, no network).

        Used by the single-threaded browser/WASM loop, which cannot run the
        background :class:`TelemetryPoller`.
        """
        return self._demo_snapshot(profiles)

    def _demo_telemetry(
        self,
        profile: AgentProfile,
        status: AgentStatus = AgentStatus.IDLE,
    ) -> AgentTelemetry:
        elapsed = time.monotonic() - self._started
        phase = (sum(ord(char) for char in profile.id) % 17) / 17
        load = 0.35 + 0.28 * math.sin(elapsed * 0.7 + phase * math.tau)
        latency = 210 + 95 * math.cos(elapsed * 0.55 + phase * math.tau)
        metrics = AgentMetrics(
            load=load,
            latency_ms=latency,
            tokens_per_minute=720 + 210 * max(0.0, load),
            error_rate=max(0.0, 0.04 * math.sin(elapsed * 0.31 + phase)),
            active_tasks=1 if load > 0.55 else 0,
        ).normalized()
        return AgentTelemetry(
            agent_id=profile.id,
            metrics=metrics,
            status=status,
            message="demo feed",
        )


class TelemetryPoller:
    def __init__(self, provider: AgentDataProvider, profiles: Sequence[AgentProfile], interval_seconds: float = 2.0) -> None:
        self._provider = provider
        self._profiles = list(profiles)
        self._interval_seconds = interval_seconds
        self._lock = threading.Lock()
        self._snapshot = ProviderSnapshot({}, provider.auth_config.mode, "starting")
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="watchtower-telemetry", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.0)

    def configure(self, auth_config: AuthConfig) -> None:
        self._provider.configure(auth_config)

    def set_profiles(self, profiles: Sequence[AgentProfile]) -> None:
        self._profiles = list(profiles)

    def latest(self) -> ProviderSnapshot:
        with self._lock:
            return self._snapshot

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                snapshot = asyncio.run(self._provider.fetch_all(self._profiles))
            except Exception as exc:
                snapshot = ProviderSnapshot({}, self._provider.auth_config.mode, "provider error", str(exc))
            with self._lock:
                self._snapshot = snapshot
            self._stop.wait(self._interval_seconds)
