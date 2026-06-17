from __future__ import annotations

import math
import uuid
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field

from watchtower.models import (
    AgentAction,
    AgentActionEvent,
    AgentMetrics,
    AgentProfile,
    AgentState,
    AgentStatus,
    AgentTelemetry,
    Position,
    SubmittedTask,
    TaskPriority,
    TaskStatus,
    utcnow,
)


WORLD_WIDTH = 900
WORLD_HEIGHT = 620

# Synthetic progress holds here while a real model call is in flight; the API
# result is what finally drives the task to 1.0.
_API_PROGRESS_CEILING = 0.9


def default_profiles() -> list[AgentProfile]:
    return [
        AgentProfile("gpt", "GPT", "OpenAI GPT", "reasoning", "#44b37f", "GPT", "openai", "gpt-5.1-mini"),
        AgentProfile("claude", "Claude", "Anthropic Claude", "analysis", "#d97842", "CLD", "anthropic", "claude-sonnet-4-5"),
        AgentProfile("gemini", "Gemini", "Google Gemini", "research", "#4d8df7", "GEM", "gemini", "gemini-3.5-flash"),
        AgentProfile("llama", "Llama", "Meta Llama", "local", "#9b72f2", "LMA", "local", ""),
        AgentProfile("mistral", "Mistral", "Mistral Large", "coding", "#e0b84f", "MST", "local", ""),
    ]


@dataclass(slots=True)
class SimulationSnapshot:
    agents: list[AgentState]
    tasks: list[SubmittedTask]
    events: list[AgentActionEvent]
    tick: int
    elapsed_seconds: float


@dataclass(slots=True)
class SimulationState:
    profiles: list[AgentProfile] = field(default_factory=default_profiles)
    agents: dict[str, AgentState] = field(init=False)
    tasks: dict[str, SubmittedTask] = field(default_factory=dict)
    event_log: deque[AgentActionEvent] = field(default_factory=lambda: deque(maxlen=80))
    tick: int = 0
    elapsed_seconds: float = 0.0
    max_finished_tasks: int = 60

    def __post_init__(self) -> None:
        self.agents = {}
        self._layout_agents()

    def _layout_agents(self) -> None:
        spacing = WORLD_WIDTH / (len(self.profiles) + 1)
        for index, profile in enumerate(self.profiles, start=1):
            target = Position(spacing * index, WORLD_HEIGHT * 0.45)
            existing = self.agents.get(profile.id)
            if existing is None:
                self.agents[profile.id] = AgentState(profile=profile, position=target)
            else:
                existing.profile = profile

    def submit_task(
        self,
        prompt: str,
        submitted_by: str = "operator",
        requested_agent_id: str | None = None,
        priority: TaskPriority = TaskPriority.NORMAL,
        group_id: str | None = None,
    ) -> SubmittedTask:
        clean_prompt = prompt.strip()
        if not clean_prompt:
            raise ValueError("Task prompt cannot be empty.")
        if requested_agent_id and requested_agent_id not in self.agents:
            raise ValueError(f"Unknown agent: {requested_agent_id}")
        title = clean_prompt if len(clean_prompt) <= 42 else f"{clean_prompt[:39]}..."
        task = SubmittedTask(
            id=uuid.uuid4().hex[:8],
            title=title,
            prompt=clean_prompt,
            submitted_by=submitted_by,
            requested_agent_id=requested_agent_id,
            priority=priority,
            estimated_tokens=200 + len(clean_prompt) // 4,
            group_id=group_id,
        )
        self.tasks[task.id] = task
        message = "task submitted"
        if requested_agent_id:
            message = f"task submitted for {self.agents[requested_agent_id].profile.display_name}"
        self._record("system", AgentAction.REPORTING, Position(WORLD_WIDTH * 0.5, 24), AgentStatus.IDLE, task.id, message)
        return task

    def submit_comparison(
        self,
        prompt: str,
        submitted_by: str = "operator",
        priority: TaskPriority = TaskPriority.NORMAL,
    ) -> list[SubmittedTask]:
        """Fan the same prompt out to every agent so their answers can be compared."""
        group_id = uuid.uuid4().hex[:8]
        tasks = [
            self.submit_task(
                prompt,
                submitted_by=submitted_by,
                requested_agent_id=agent_id,
                priority=priority,
                group_id=group_id,
            )
            for agent_id in self.agents
        ]
        self._record("system", AgentAction.REPORTING, Position(WORLD_WIDTH * 0.5, 24), AgentStatus.IDLE, group_id, f"compare across {len(tasks)} agents")
        return tasks

    def create_todo_task(
        self,
        prompt: str,
        submitted_by: str = "operator",
        priority: TaskPriority = TaskPriority.NORMAL,
    ) -> SubmittedTask:
        task = self.submit_task(prompt, submitted_by=submitted_by, priority=priority)
        task.mark_todo()
        self._record("system", AgentAction.IDLE, Position(24, 24), AgentStatus.IDLE, task.id, "added to todo")
        return task

    def cancel_task(self, task_id: str) -> SubmittedTask:
        task = self.tasks[task_id]
        task.cancel()
        self._free_agent_for(task_id)
        self._record(task.assigned_agent_id or "system", AgentAction.IDLE, Position(WORLD_WIDTH * 0.5, 24), AgentStatus.IDLE, task_id, "cancelled")
        return task

    def retry_task(self, task_id: str) -> SubmittedTask:
        task = self.tasks[task_id]
        self._free_agent_for(task_id)
        task.reset_for_retry()
        self._record("system", AgentAction.REPORTING, Position(WORLD_WIDTH * 0.5, 24), AgentStatus.IDLE, task_id, "retry queued")
        return task

    def remove_task(self, task_id: str) -> None:
        self._free_agent_for(task_id)
        self.tasks.pop(task_id, None)

    def clear_finished(self) -> int:
        finished = [task_id for task_id, task in self.tasks.items() if task.is_finished]
        for task_id in finished:
            self.tasks.pop(task_id, None)
        return len(finished)

    def add_agent(self, profile: AgentProfile) -> AgentState:
        if profile.id in self.agents:
            raise ValueError(f"Agent already exists: {profile.id}")
        self.profiles.append(profile)
        self._layout_agents()
        self._record("system", AgentAction.IDLE, Position(WORLD_WIDTH * 0.5, 24), AgentStatus.IDLE, None, f"added agent {profile.display_name}")
        return self.agents[profile.id]

    def remove_agent(self, agent_id: str) -> list[str]:
        if agent_id not in self.agents:
            raise ValueError(f"Unknown agent: {agent_id}")
        requeued: list[str] = []
        for task in self.tasks.values():
            if task.assigned_agent_id == agent_id and task.is_active:
                task.reset_for_retry()  # clears assignment + api_started so it can rerun cleanly
                requeued.append(task.id)
            if task.requested_agent_id == agent_id:
                task.requested_agent_id = None
        self.agents.pop(agent_id)
        self.profiles = [profile for profile in self.profiles if profile.id != agent_id]
        self._layout_agents()
        self._record("system", AgentAction.IDLE, Position(WORLD_WIDTH * 0.5, 24), AgentStatus.IDLE, None, f"removed agent {agent_id}")
        return requeued

    def _free_agent_for(self, task_id: str) -> None:
        for agent in self.agents.values():
            if agent.current_task_id == task_id:
                agent.current_task_id = None
                agent.status = AgentStatus.IDLE

    def assign_todo_task(self, task_id: str, agent_id: str) -> SubmittedTask:
        task = self.tasks[task_id]
        if agent_id not in self.agents:
            raise ValueError(f"Unknown agent: {agent_id}")
        if task.status is not TaskStatus.TODO:
            raise ValueError(f"Task {task_id} is not in the todo list.")
        task.requested_agent_id = agent_id
        task.status = TaskStatus.SUBMITTED
        task.updated_at = utcnow()
        self._record(agent_id, AgentAction.REPORTING, self.agents[agent_id].position, AgentStatus.IDLE, task.id, "dropped on agent")
        return task

    def update(self, dt: float, telemetry: dict[str, AgentTelemetry] | None = None) -> None:
        self.tick += 1
        self.elapsed_seconds += dt
        telemetry = telemetry or {}
        self._assign_waiting_tasks()
        for index, agent in enumerate(self.agents.values()):
            remote = telemetry.get(agent.profile.id)
            if remote:
                agent.metrics = remote.metrics
                agent.status = remote.status if agent.current_task_id is None else AgentStatus.WORKING
            self._advance_agent(index, agent, dt)
        self._prune_finished()

    def snapshot(self) -> SimulationSnapshot:
        tasks = sorted(self.tasks.values(), key=lambda task: task.created_at, reverse=True)
        return SimulationSnapshot(
            agents=list(self.agents.values()),
            tasks=tasks,
            events=list(self.event_log),
            tick=self.tick,
            elapsed_seconds=self.elapsed_seconds,
        )

    def _assign_waiting_tasks(self) -> None:
        waiting = sorted(
            (task for task in self.tasks.values() if task.status is TaskStatus.SUBMITTED),
            key=lambda task: (task.priority.rank, task.created_at),
        )
        for task in waiting:
            candidates = self._candidate_agents(task)
            if not candidates:
                continue
            agent = candidates[0]
            task.assign_to(agent.profile.id)
            agent.current_task_id = task.id
            agent.status = AgentStatus.WORKING
            agent.action = AgentAction.PROCESSING_TASK
            self._record(agent.profile.id, agent.action, agent.position, agent.status, task.id, f"assigned {task.title}")

    def _candidate_agents(self, task: SubmittedTask) -> list[AgentState]:
        if task.requested_agent_id:
            agent = self.agents.get(task.requested_agent_id)
            if not agent or agent.current_task_id is not None:
                return []
            return [agent]
        available_agents = [agent for agent in self.agents.values() if agent.current_task_id is None]
        return sorted(
            available_agents,
            key=lambda agent: (agent.metrics.load, agent.profile.id),
        )

    def _advance_agent(self, index: int, agent: AgentState, dt: float) -> None:
        if agent.current_task_id:
            task = self.tasks.get(agent.current_task_id)
            if task is None:
                agent.current_task_id = None
                return
            target = self._task_station(index)
            agent.position = _move_toward(agent.position, target, 150 * dt)
            step = dt * (0.10 + 0.11 * (1.0 - agent.metrics.load))
            if task.api_started and not task.api_completed:
                # A real model call owns this task: ease toward a holding point and let
                # the actual response (not synthetic motion) mark it complete.
                task.mark_progress(min(_API_PROGRESS_CEILING, task.progress + step * 0.6))
            else:
                task.mark_progress(task.progress + step)
            agent.action = AgentAction.PROCESSING_TASK if task.progress < 0.86 else AgentAction.REPORTING
            agent.status = AgentStatus.WORKING
            if task.status is TaskStatus.COMPLETE:
                self._record(agent.profile.id, AgentAction.REPORTING, agent.position, agent.status, task.id, "completed")
                agent.current_task_id = None
                agent.action = AgentAction.REPORTING
            return

        angle = self.elapsed_seconds * 0.55 + index * math.tau / max(1, len(self.agents))
        radius = 72 + 18 * math.sin(self.elapsed_seconds * 0.7 + index)
        target = Position(
            WORLD_WIDTH * 0.5 + math.cos(angle) * radius * (1.7 + index * 0.05),
            WORLD_HEIGHT * 0.48 + math.sin(angle) * radius,
        )
        agent.position = _move_toward(agent.position, target, 90 * dt)
        agent.action = AgentAction.FETCHING_DATA if self.tick % 180 < 28 else AgentAction.PATROLLING
        if agent.status is AgentStatus.WORKING:
            agent.status = AgentStatus.IDLE

    def _task_station(self, index: int) -> Position:
        return Position(130 + index * 145, WORLD_HEIGHT - 92)

    def _prune_finished(self) -> None:
        finished = sorted(
            (task for task in self.tasks.values() if task.is_finished),
            key=lambda task: task.updated_at,
        )
        overflow = len(finished) - self.max_finished_tasks
        for task in finished[:max(0, overflow)]:
            self.tasks.pop(task.id, None)

    def _record(
        self,
        agent_id: str,
        action: AgentAction,
        position: Position,
        status: AgentStatus,
        task_id: str | None,
        message: str,
    ) -> None:
        self.event_log.append(
            AgentActionEvent(
                tick=self.tick,
                elapsed_seconds=self.elapsed_seconds,
                agent_id=agent_id,
                action=action,
                position=position,
                status=status,
                task_id=task_id,
                message=message,
            )
        )

    def apply_telemetry(self, telemetry: Iterable[AgentTelemetry]) -> None:
        for item in telemetry:
            agent = self.agents.get(item.agent_id)
            if not agent:
                continue
            agent.metrics = item.metrics.normalized()
            agent.status = item.status


def _move_toward(start: Position, target: Position, max_distance: float) -> Position:
    dx = target.x - start.x
    dy = target.y - start.y
    distance = math.hypot(dx, dy)
    if distance <= max_distance or distance == 0:
        return target
    scale = max_distance / distance
    return Position(start.x + dx * scale, start.y + dy * scale)
