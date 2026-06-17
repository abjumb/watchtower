from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from watchtower.models import (
    AgentProfile,
    SubmittedTask,
    TaskPriority,
    TaskStatus,
    utcnow,
)

SESSION_VERSION = 1


def profile_to_dict(profile: AgentProfile) -> dict[str, Any]:
    return {
        "id": profile.id,
        "display_name": profile.display_name,
        "model_name": profile.model_name,
        "role": profile.role,
        "accent_color": profile.accent_color,
        "glyph": profile.glyph,
        "provider": profile.provider,
        "api_model": profile.api_model,
    }


def profile_from_dict(data: dict[str, Any]) -> AgentProfile:
    return AgentProfile(
        id=str(data["id"]),
        display_name=str(data.get("display_name", data["id"])),
        model_name=str(data.get("model_name", data["id"])),
        role=str(data.get("role", "general")),
        accent_color=str(data.get("accent_color", "#4f8cff")),
        glyph=str(data.get("glyph", "AI")),
        provider=str(data.get("provider", "local")),
        api_model=str(data.get("api_model", "")),
    )


def task_to_dict(task: SubmittedTask) -> dict[str, Any]:
    return {
        "id": task.id,
        "title": task.title,
        "prompt": task.prompt,
        "submitted_by": task.submitted_by,
        "requested_agent_id": task.requested_agent_id,
        "priority": task.priority.value,
        "status": task.status.value,
        "assigned_agent_id": task.assigned_agent_id,
        "progress": task.progress,
        "model_response": task.model_response,
        "model_error": task.model_error,
        "model_latency_ms": task.model_latency_ms,
        "group_id": task.group_id,
        "created_at": task.created_at.isoformat(),
        "updated_at": task.updated_at.isoformat(),
    }


def task_from_dict(data: dict[str, Any]) -> SubmittedTask:
    return SubmittedTask(
        id=str(data["id"]),
        title=str(data.get("title", "")),
        prompt=str(data.get("prompt", "")),
        submitted_by=str(data.get("submitted_by", "operator")),
        requested_agent_id=data.get("requested_agent_id"),
        priority=TaskPriority(data.get("priority", TaskPriority.NORMAL.value)),
        status=TaskStatus(data.get("status", TaskStatus.SUBMITTED.value)),
        assigned_agent_id=data.get("assigned_agent_id"),
        progress=float(data.get("progress", 0.0)),
        model_response=str(data.get("model_response", "")),
        model_error=str(data.get("model_error", "")),
        model_latency_ms=float(data.get("model_latency_ms", 0.0)),
        api_completed=bool(data.get("model_response") or data.get("model_error")),
        group_id=data.get("group_id"),
        created_at=_parse_dt(data.get("created_at")),
        updated_at=_parse_dt(data.get("updated_at")),
    )


def _parse_dt(value: Any) -> datetime:
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return utcnow()
    return utcnow()


def session_to_dict(profiles: list[AgentProfile], tasks: list[SubmittedTask]) -> dict[str, Any]:
    return {
        "version": SESSION_VERSION,
        "saved_at": utcnow().isoformat(),
        "profiles": [profile_to_dict(profile) for profile in profiles],
        "tasks": [task_to_dict(task) for task in tasks],
    }


def save_session(path: str | Path, profiles: list[AgentProfile], tasks: list[SubmittedTask]) -> Path:
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(session_to_dict(profiles, tasks), indent=2), encoding="utf-8")
    return target


def load_session(path: str | Path) -> tuple[list[AgentProfile], list[SubmittedTask]]:
    data = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    profiles = [profile_from_dict(item) for item in data.get("profiles", [])]
    tasks = [task_from_dict(item) for item in data.get("tasks", [])]
    return profiles, tasks


def export_task_text(path: str | Path, task: SubmittedTask) -> Path:
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# {task.title}",
        "",
        f"Agent: {task.assigned_agent_id or task.requested_agent_id or 'auto'}",
        f"Status: {task.status.value}",
        f"Latency: {task.model_latency_ms:.0f} ms",
        "",
        "## Prompt",
        task.prompt,
        "",
        "## Response",
        task.model_response or task.model_error or "(no response captured)",
        "",
    ]
    target.write_text("\n".join(lines), encoding="utf-8")
    return target
