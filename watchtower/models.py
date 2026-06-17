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
    TODO = "todo"
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

    @property
    def rank(self) -> int:
        """Lower sorts first when scheduling waiting tasks."""
        return {"critical": 0, "high": 1, "normal": 2, "low": 3}[self.value]


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
    provider: str = "local"
    api_model: str = ""


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
    model_response: str = ""
    model_partial: str = ""
    model_error: str = ""
    model_latency_ms: float = 0.0
    api_started: bool = False
    api_completed: bool = False
    group_id: str | None = None
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)

    @property
    def is_active(self) -> bool:
        return self.status in {TaskStatus.ASSIGNED, TaskStatus.IN_PROGRESS}

    @property
    def is_finished(self) -> bool:
        return self.status in {TaskStatus.COMPLETE, TaskStatus.FAILED, TaskStatus.CANCELLED}

    def assign_to(self, agent_id: str) -> None:
        self.assigned_agent_id = agent_id
        self.status = TaskStatus.ASSIGNED
        self.updated_at = utcnow()

    def mark_progress(self, progress: float) -> None:
        self.progress = clamp(progress)
        self.status = TaskStatus.COMPLETE if self.progress >= 1.0 else TaskStatus.IN_PROGRESS
        self.updated_at = utcnow()

    def mark_todo(self) -> None:
        self.status = TaskStatus.TODO
        self.updated_at = utcnow()

    def append_partial(self, delta: str) -> None:
        """Accumulate a streamed token delta while the model call is in flight."""
        if not delta:
            return
        self.model_partial += delta
        self.status = TaskStatus.IN_PROGRESS
        self.updated_at = utcnow()

    def mark_model_result(self, response: str, latency_ms: float = 0.0) -> None:
        self.model_response = response
        self.model_latency_ms = latency_ms
        self.api_completed = True
        self.progress = 1.0
        self.status = TaskStatus.COMPLETE
        self.updated_at = utcnow()

    def mark_model_error(self, error: str) -> None:
        self.model_error = error
        self.api_completed = True
        self.status = TaskStatus.FAILED
        self.updated_at = utcnow()

    def cancel(self) -> None:
        self.status = TaskStatus.CANCELLED
        self.updated_at = utcnow()

    def reset_for_retry(self) -> None:
        """Return a finished task to the routing queue for another attempt."""
        self.status = TaskStatus.SUBMITTED
        self.assigned_agent_id = None
        self.progress = 0.0
        self.model_response = ""
        self.model_partial = ""
        self.model_error = ""
        self.model_latency_ms = 0.0
        self.api_started = False
        self.api_completed = False
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
