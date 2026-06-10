from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any


def utcnow() -> datetime:
    return datetime.now(UTC)


def clamp(value: float, minimum: float = 0.0, maximum: float = 1.0) -> float:
    return max(minimum, min(maximum, value))


class AgentStatus(str, Enum):
    IDLE = "idle"
    WORKING = "working"
    WAITING = "waiting"
    DEGRADED = "degraded"
    OFFLINE = "offline"


class AgentAction(str, Enum):
    IDLE = "idle"
    PATROLLING = "patrolling"
    FETCHING_DATA = "fetching_data"
    PROCESSING_TASK = "processing_task"
    REPORTING = "reporting"
    ERROR = "error"
    OFFLINE = "offline"


class TaskStatus(str, Enum):
    SUBMITTED = "submitted"
    ASSIGNED = "assigned"
    IN_PROGRESS = "in_progress"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskPriority(str, Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass(slots=True)
class Position:
    x: float
    y: float

    def moved(self, dx: float, dy: float) -> Position:
        return Position(self.x + dx, self.y + dy)


@dataclass(slots=True)
class AgentProfile:
    id: str
    display_name: str
    model_name: str
    role: str = "general"
    accent_color: str = "#4f8cff"
    glyph: str = "AI"


@dataclass(slots=True)
class AgentMetrics:
    load: float = 0.0
    latency_ms: float = 0.0
    tokens_per_minute: float = 0.0
    error_rate: float = 0.0
    active_tasks: int = 0

    def normalized(self) -> AgentMetrics:
        return AgentMetrics(
            load=clamp(self.load),
            latency_ms=max(0.0, self.latency_ms),
            tokens_per_minute=max(0.0, self.tokens_per_minute),
            error_rate=clamp(self.error_rate),
            active_tasks=max(0, self.active_tasks),
        )


@dataclass(slots=True)
class AgentTelemetry:
    agent_id: str
    metrics: AgentMetrics
    status: AgentStatus = AgentStatus.IDLE
    message: str = ""
    observed_at: datetime = field(default_factory=utcnow)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SubmittedTask:
    id: str
    title: str
    prompt: str
    submitted_by: str
    requested_agent_id: str | None = None
    priority: TaskPriority = TaskPriority.NORMAL
    status: TaskStatus = TaskStatus.SUBMITTED
    assigned_agent_id: str | None = None
    progress: float = 0.0
    estimated_tokens: int = 1_000
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)

    def assign_to(self, agent_id: str) -> None:
        self.assigned_agent_id = agent_id
        self.status = TaskStatus.ASSIGNED
        self.updated_at = utcnow()

    def mark_progress(self, progress: float) -> None:
        self.progress = clamp(progress)
        self.status = TaskStatus.COMPLETE if self.progress >= 1.0 else TaskStatus.IN_PROGRESS
        self.updated_at = utcnow()


@dataclass(slots=True)
class AgentState:
    profile: AgentProfile
    position: Position
    status: AgentStatus = AgentStatus.IDLE
    action: AgentAction = AgentAction.IDLE
    metrics: AgentMetrics = field(default_factory=AgentMetrics)
    current_task_id: str | None = None
    last_updated: datetime = field(default_factory=utcnow)


@dataclass(slots=True)
class AgentActionEvent:
    tick: int
    elapsed_seconds: float
    agent_id: str
    action: AgentAction
    position: Position
    status: AgentStatus
    task_id: str | None = None
    message: str = ""
