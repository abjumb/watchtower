from __future__ import annotations

import asyncio
import math
import queue
import threading
from dataclasses import dataclass

import pygame

from watchtower.auth import AuthConfig
from watchtower.data_provider import AgentDataProvider, TelemetryPoller
from watchtower.model_api import ModelApiClient, ModelCallResult
from watchtower.models import (
    AgentProfile,
    AgentState,
    AgentStatus,
    SubmittedTask,
    TaskPriority,
    TaskStatus,
)
from watchtower.persistence import export_task_text, load_session, save_session
from watchtower.simulation import WORLD_HEIGHT, WORLD_WIDTH, SimulationState


LEFT_PANEL_WIDTH = 236
WORLD_X = LEFT_PANEL_WIDTH + 24
WORLD_Y = 16
PANEL_X = WORLD_X + WORLD_WIDTH + 16
SCREEN_WIDTH = PANEL_X + 260
SCREEN_HEIGHT = 760
MIN_WIDTH = SCREEN_WIDTH
MIN_HEIGHT = SCREEN_HEIGHT

# Palette for agents added at runtime via /agent add.
AGENT_COLORS = ["#44b37f", "#d97842", "#4d8df7", "#9b72f2", "#e0b84f", "#5fd0c4", "#e76f9a"]

DEFAULT_SAVE_PATH = "watchtower_session.json"


@dataclass(frozen=True, slots=True)
class Theme:
    name: str
    bg: tuple[int, int, int]
    surface: tuple[int, int, int]
    surface_alt: tuple[int, int, int]
    text: tuple[int, int, int]
    muted: tuple[int, int, int]
    grid: tuple[int, int, int]
    accent: tuple[int, int, int]
    success: tuple[int, int, int]
    danger: tuple[int, int, int]
    warning: tuple[int, int, int]
    overlay: tuple[int, int, int]


DARK_THEME = Theme(
    name="dark",
    bg=(13, 17, 23),
    surface=(24, 31, 42),
    surface_alt=(33, 42, 55),
    text=(227, 232, 239),
    muted=(146, 157, 171),
    grid=(38, 48, 63),
    accent=(95, 168, 255),
    success=(77, 201, 129),
    danger=(236, 122, 122),
    warning=(224, 184, 79),
    overlay=(8, 11, 15),
)

LIGHT_THEME = Theme(
    name="light",
    bg=(238, 241, 246),
    surface=(255, 255, 255),
    surface_alt=(228, 233, 240),
    text=(28, 34, 44),
    muted=(104, 116, 132),
    grid=(212, 219, 228),
    accent=(51, 122, 221),
    success=(40, 158, 96),
    danger=(199, 72, 72),
    warning=(176, 130, 28),
    overlay=(206, 212, 220),
)

PRIORITY_NAMES = {p.value: p for p in TaskPriority}


class WatchtowerApp:
    def __init__(self) -> None:
        pygame.init()
        pygame.display.set_caption("Watchtower")
        self.screen_width = SCREEN_WIDTH
        self.screen_height = SCREEN_HEIGHT
        self.screen = pygame.display.set_mode((self.screen_width, self.screen_height), pygame.RESIZABLE)
        self.clock = pygame.time.Clock()
        self.font = pygame.font.SysFont("arial", 16)
        self.small_font = pygame.font.SysFont("arial", 13)
        self.title_font = pygame.font.SysFont("arial", 24, bold=True)
        self.badge_font = pygame.font.SysFont("arial", 14, bold=True)
        self.theme = DARK_THEME
        self.simulation = SimulationState()
        self.auth_config = AuthConfig.from_env()
        self.provider = AgentDataProvider(self.auth_config)
        self.poller = TelemetryPoller(self.provider, self.simulation.profiles)
        self.model_api = ModelApiClient()
        self.model_results: queue.Queue[tuple[str, str, object]] = queue.Queue()
        self.running_model_tasks: set[str] = set()
        self.input_text = ""
        self.flash_message = "Ready - F1 help, F2 theme"
        self.selected_agent_id: str | None = None
        self.selected_task_id: str | None = None
        self.show_help = False
        self.dragging_task_id: str | None = None
        self.task_scroll = 0
        self.effects: list[list[float]] = []
        self._completed_seen: set[str] = set()
        self._station_hits: list[tuple[pygame.Rect, str]] = []
        self._panel_task_hits: list[tuple[pygame.Rect, str]] = []
        self.running = True

    # ----- main loop ---------------------------------------------------------
    def run(self) -> None:
        self.poller.start()
        try:
            while self.running:
                dt = self.clock.tick(60) / 1000
                self._handle_events()
                provider_snapshot = self.poller.latest()
                self.simulation.update(dt, provider_snapshot.telemetry)
                self._drain_model_results()
                self._start_ready_model_calls()
                self._sync_completion_effects()
                self._update_effects(dt)
                self._draw(provider_snapshot)
                pygame.display.flip()
        finally:
            self.poller.stop()
            pygame.quit()

    # ----- events ------------------------------------------------------------
    def _handle_events(self) -> None:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                self.running = False
            elif event.type == pygame.VIDEORESIZE:
                self._resize(event.w, event.h)
            elif event.type == pygame.MOUSEWHEEL:
                self.task_scroll = max(0, self.task_scroll - event.y)
            elif event.type == pygame.KEYDOWN:
                self._handle_keydown(event)
            elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                self._handle_mouse_down(event.pos)
            elif event.type == pygame.MOUSEBUTTONUP and event.button == 1:
                self._finish_drag(event.pos)

    def _handle_keydown(self, event: pygame.event.Event) -> None:
        if event.key == pygame.K_ESCAPE:
            if self.show_help:
                self.show_help = False
            elif self.selected_task_id:
                self.selected_task_id = None
            else:
                self.running = False
        elif event.key == pygame.K_F1:
            self.show_help = not self.show_help
        elif event.key == pygame.K_F2:
            self._toggle_theme()
        elif event.key == pygame.K_RETURN:
            self._submit_input()
        elif event.key == pygame.K_BACKSPACE:
            self.input_text = self.input_text[:-1]
        elif self._is_agent_shortcut(event):
            self._select_agent_by_index(event.key - pygame.K_1)
        elif event.unicode and len(self.input_text) < 180:
            self.input_text += event.unicode

    def _handle_mouse_down(self, pos: tuple[int, int]) -> None:
        if self.show_help:
            self.show_help = False
            return
        if self.selected_task_id:
            self._handle_detail_click(pos)
            return
        todo_task_id = self._todo_task_at(pos)
        if todo_task_id:
            self.dragging_task_id = todo_task_id
            return
        if self._submit_rect().collidepoint(pos):
            self._submit_input()
            return
        station_task = self._station_at(pos)
        if station_task:
            self.selected_task_id = station_task
            return
        panel_task = self._panel_task_at(pos)
        if panel_task:
            self.selected_task_id = panel_task
            return
        if self._handle_selection_click(pos):
            return
        world_agent = self._agent_at(pos)
        if world_agent:
            self.selected_agent_id = world_agent
            self.flash_message = f"Selected {self._agent_label(world_agent)}"

    def _resize(self, width: int, height: int) -> None:
        self.screen_width = max(MIN_WIDTH, width)
        self.screen_height = max(MIN_HEIGHT, height)
        self.screen = pygame.display.set_mode((self.screen_width, self.screen_height), pygame.RESIZABLE)

    def _toggle_theme(self) -> None:
        self.theme = LIGHT_THEME if self.theme is DARK_THEME else DARK_THEME
        self.flash_message = f"{self.theme.name.title()} theme"

    # ----- command + task submission ----------------------------------------
    def _submit_input(self) -> None:
        text = self.input_text.strip()
        if not text:
            return
        self.input_text = ""
        if text.startswith("/") and self._handle_command(text):
            return

        compare = False
        requested_agent_id: str | None = None
        rest = text
        if rest.lower().startswith("@all ") or rest.lower() == "@all":
            compare = True
            rest = rest[4:].strip()
        else:
            requested_agent_id, rest = self._parse_targeted_prompt(rest)
        priority, rest = self._parse_priority(rest)

        if not rest:
            self.flash_message = "Type a task prompt"
            return
        if compare:
            tasks = self.simulation.submit_comparison(rest, priority=priority)
            self.flash_message = f"Comparing across {len(tasks)} agents"
            return
        target_agent_id = requested_agent_id or self.selected_agent_id
        if target_agent_id:
            task = self.simulation.submit_task(rest, requested_agent_id=target_agent_id, priority=priority)
            self.flash_message = f"Task {task.id} submitted to {self._agent_label(target_agent_id)}"
        else:
            task = self.simulation.create_todo_task(rest, priority=priority)
            self.flash_message = f"Todo {task.id} added"

    def _handle_command(self, text: str) -> bool:
        if text.startswith("/auth token "):
            self.auth_config = self.auth_config.with_token(text.removeprefix("/auth token ").strip())
            self.poller.configure(self.auth_config)
            self.flash_message = "OAuth token loaded"
            return True
        if text.startswith("/auth login "):
            parts = text.split(maxsplit=3)
            if len(parts) == 4:
                self.auth_config = self.auth_config.with_login(parts[2], parts[3])
                self.poller.configure(self.auth_config)
                self.flash_message = "Login credentials loaded"
            else:
                self.flash_message = "Use /auth login USER PASS"
            return True
        if text.startswith("/endpoint "):
            self.auth_config = self.auth_config.with_endpoint(text.removeprefix("/endpoint ").strip())
            self.poller.configure(self.auth_config)
            self.flash_message = "Telemetry endpoint set"
            return True
        if text == "/auto":
            self.selected_agent_id = None
            self.flash_message = "Task routing set to auto"
            return True
        if text in ("/help", "/?"):
            self.show_help = True
            return True
        if text.startswith("/theme"):
            arg = text[len("/theme"):].strip().lower()
            if arg == "light":
                self.theme = LIGHT_THEME
            elif arg == "dark":
                self.theme = DARK_THEME
            else:
                self._toggle_theme()
                return True
            self.flash_message = f"{self.theme.name.title()} theme"
            return True
        if text == "/clear":
            removed = self.simulation.clear_finished()
            self.task_scroll = 0
            self.flash_message = f"Cleared {removed} finished tasks"
            return True
        if text.startswith("/cancel "):
            self._safe_task_command(text.removeprefix("/cancel ").strip(), self._cancel_task)
            return True
        if text.startswith("/retry "):
            self._safe_task_command(text.removeprefix("/retry ").strip(), self._retry_task)
            return True
        if text.startswith("/compare "):
            tasks = self.simulation.submit_comparison(text.removeprefix("/compare ").strip())
            self.flash_message = f"Comparing across {len(tasks)} agents"
            return True
        if text.startswith("/key "):
            self._handle_key_command(text.removeprefix("/key ").strip())
            return True
        if text.startswith("/agent "):
            self._handle_agent_command(text.removeprefix("/agent ").strip())
            return True
        if text.startswith("/save"):
            path = text[len("/save"):].strip() or DEFAULT_SAVE_PATH
            saved = save_session(path, self.simulation.profiles, list(self.simulation.tasks.values()))
            self.flash_message = f"Saved to {saved.name}"
            return True
        if text.startswith("/load"):
            self._handle_load(text[len("/load"):].strip() or DEFAULT_SAVE_PATH)
            return True
        if text.startswith("/export "):
            self._handle_export(text.removeprefix("/export ").strip())
            return True
        self.flash_message = f"Unknown command: {text.split()[0]}"
        return True

    def _parse_targeted_prompt(self, text: str) -> tuple[str | None, str]:
        if not text.startswith("@"):
            return None, text
        target, _, prompt = text.partition(" ")
        agent_id = target[1:].strip().lower()
        if agent_id in self.simulation.agents and prompt.strip():
            return agent_id, prompt.strip()
        return None, text

    def _parse_priority(self, text: str) -> tuple[TaskPriority, str]:
        if text.startswith("!"):
            token, _, rest = text.partition(" ")
            name = token[1:].strip().lower()
            if name in PRIORITY_NAMES:
                return PRIORITY_NAMES[name], rest.strip()
        return TaskPriority.NORMAL, text

    def _safe_task_command(self, task_id, action) -> None:
        if task_id in self.simulation.tasks:
            action(task_id)
        else:
            self.flash_message = f"No task {task_id}"

    def _cancel_task(self, task_id: str) -> None:
        self.simulation.cancel_task(task_id)
        self.running_model_tasks.discard(task_id)
        self.flash_message = f"Cancelled {task_id}"

    def _retry_task(self, task_id: str) -> None:
        self.simulation.retry_task(task_id)
        self.running_model_tasks.discard(task_id)
        self.flash_message = f"Retrying {task_id}"

    def _handle_key_command(self, arg: str) -> None:
        parts = arg.split(maxsplit=1)
        if len(parts) != 2:
            self.flash_message = "Use /key PROVIDER VALUE"
            return
        provider, value = parts
        try:
            self.model_api.config.set_key(provider.lower(), value)
            self.flash_message = f"{provider.lower()} key set"
        except ValueError:
            self.flash_message = f"Unknown provider: {provider}"

    def _handle_agent_command(self, arg: str) -> None:
        if arg.startswith("remove "):
            agent_id = arg.removeprefix("remove ").strip().lower()
            if agent_id not in self.simulation.agents:
                self.flash_message = f"No agent {agent_id}"
                return
            self.simulation.remove_agent(agent_id)
            self.poller.set_profiles(self.simulation.profiles)
            if self.selected_agent_id == agent_id:
                self.selected_agent_id = None
            self.flash_message = f"Removed {agent_id}"
            return
        if arg.startswith("add "):
            parts = arg.removeprefix("add ").split(maxsplit=3)
            if len(parts) < 3:
                self.flash_message = "Use /agent add ID PROVIDER MODEL [Name]"
                return
            agent_id, provider, model = parts[0].lower(), parts[1].lower(), parts[2]
            display = parts[3] if len(parts) > 3 else agent_id.title()
            if provider not in {"openai", "anthropic", "gemini", "local"}:
                self.flash_message = f"Unknown provider: {provider}"
                return
            if agent_id in self.simulation.agents:
                self.flash_message = f"Agent {agent_id} exists"
                return
            color = AGENT_COLORS[len(self.simulation.agents) % len(AGENT_COLORS)]
            profile = AgentProfile(
                id=agent_id,
                display_name=display,
                model_name=f"{display} {model}".strip(),
                accent_color=color,
                glyph=agent_id[:3].upper(),
                provider=provider,
                api_model=model,
            )
            self.simulation.add_agent(profile)
            self.poller.set_profiles(self.simulation.profiles)
            self.flash_message = f"Added {display}"
            return
        self.flash_message = "Use /agent add|remove ..."

    def _handle_load(self, path: str) -> None:
        try:
            profiles, tasks = load_session(path)
        except (OSError, ValueError) as exc:
            self.flash_message = f"Load failed: {type(exc).__name__}"
            return
        if not profiles:
            self.flash_message = "Session had no agents"
            return
        self.simulation = SimulationState(profiles=profiles)
        for task in tasks:
            if not task.is_finished:
                task.reset_for_retry()
            self.simulation.tasks[task.id] = task
        self.poller.set_profiles(profiles)
        self.selected_agent_id = None
        self.selected_task_id = None
        self.running_model_tasks.clear()
        self._completed_seen = {t.id for t in tasks if t.status is TaskStatus.COMPLETE}
        self.flash_message = f"Loaded {len(tasks)} tasks"

    def _handle_export(self, arg: str) -> None:
        parts = arg.split(maxsplit=1)
        task_id = parts[0]
        task = self.simulation.tasks.get(task_id)
        if not task:
            self.flash_message = f"No task {task_id}"
            return
        path = parts[1] if len(parts) > 1 else f"{task_id}.md"
        saved = export_task_text(path, task)
        self.flash_message = f"Exported {saved.name}"

    # ----- model call plumbing ----------------------------------------------
    def _start_ready_model_calls(self) -> None:
        for task in self.simulation.tasks.values():
            if task.status not in {TaskStatus.ASSIGNED, TaskStatus.IN_PROGRESS}:
                continue
            if task.api_started or task.id in self.running_model_tasks or not task.assigned_agent_id:
                continue
            agent = self.simulation.agents.get(task.assigned_agent_id)
            if not agent or not self.model_api.is_configured(agent.profile):
                continue
            task.api_started = True
            self.running_model_tasks.add(task.id)
            thread = threading.Thread(
                target=self._run_model_task,
                args=(task.id, agent.profile, task.prompt),
                name=f"watchtower-model-{task.id}",
                daemon=True,
            )
            thread.start()

    def _run_model_task(self, task_id: str, profile: AgentProfile, prompt: str) -> None:
        def on_delta(delta: str) -> None:
            self.model_results.put(("delta", task_id, delta))

        try:
            result = asyncio.run(self.model_api.run_task(profile, prompt, on_delta=on_delta))
            self.model_results.put(("done", task_id, result))
        except Exception as exc:
            self.model_results.put(("error", task_id, f"{type(exc).__name__}: {exc}"))

    def _drain_model_results(self) -> None:
        while True:
            try:
                kind, task_id, payload = self.model_results.get_nowait()
            except queue.Empty:
                return
            task = self.simulation.tasks.get(task_id)
            if task is None:
                if kind in {"done", "error"}:
                    self.running_model_tasks.discard(task_id)
                continue
            if kind == "delta":
                task.append_partial(str(payload))
                continue
            self.running_model_tasks.discard(task_id)
            agent_id = task.assigned_agent_id
            if kind == "error":
                task.mark_model_error(str(payload)[:180])
                self.flash_message = f"{task.id} API error"
            else:
                result = payload
                assert isinstance(result, ModelCallResult)
                task.mark_model_result(result.text[:4000], latency_ms=result.latency_ms)
                self.flash_message = f"{task.id} completed by {self._agent_label(agent_id)}"
            if agent_id and agent_id in self.simulation.agents:
                self.simulation.agents[agent_id].current_task_id = None

    # ----- completion effects ------------------------------------------------
    def _sync_completion_effects(self) -> None:
        for task in self.simulation.tasks.values():
            if task.status is TaskStatus.COMPLETE and task.id not in self._completed_seen:
                self._completed_seen.add(task.id)
                center = self._effect_origin(task)
                self.effects.append([float(center[0]), float(center[1]), 0.0, 0.9])
        self._completed_seen &= set(self.simulation.tasks)

    def _effect_origin(self, task: SubmittedTask) -> tuple[int, int]:
        agent = self.simulation.agents.get(task.assigned_agent_id or task.requested_agent_id or "")
        if agent:
            return self._agent_screen_position(agent)
        return (WORLD_X + WORLD_WIDTH // 2, WORLD_Y + WORLD_HEIGHT // 2)

    def _update_effects(self, dt: float) -> None:
        for effect in self.effects:
            effect[2] += dt
        self.effects = [effect for effect in self.effects if effect[2] < effect[3]]

    # ----- drawing -----------------------------------------------------------
    def _draw(self, provider_snapshot) -> None:
        if self.selected_task_id and self.selected_task_id not in self.simulation.tasks:
            self.selected_task_id = None
        self.screen.fill(self.theme.bg)
        snapshot = self.simulation.snapshot()
        self._draw_todo_panel(snapshot.tasks)
        self._draw_world()
        for agent in snapshot.agents:
            self._draw_agent(agent)
        self._draw_task_stations(snapshot.tasks)
        self._draw_effects()
        self._draw_panel(snapshot, provider_snapshot)
        self._draw_input()
        if self.selected_task_id:
            self._draw_detail(self.simulation.tasks[self.selected_task_id])
        if self.show_help:
            self._draw_help()

    def _draw_world(self) -> None:
        theme = self.theme
        world = pygame.Rect(WORLD_X, WORLD_Y, WORLD_WIDTH, WORLD_HEIGHT)
        pygame.draw.rect(self.screen, theme.surface, world, border_radius=8)
        pygame.draw.rect(self.screen, theme.grid, world, width=1, border_radius=8)
        for x in range(64, WORLD_WIDTH, 64):
            pygame.draw.line(self.screen, theme.grid, (WORLD_X + x, WORLD_Y), (WORLD_X + x, WORLD_Y + WORLD_HEIGHT), 1)
        for y in range(64, WORLD_HEIGHT, 64):
            pygame.draw.line(self.screen, theme.grid, (WORLD_X, WORLD_Y + y), (WORLD_X + WORLD_WIDTH, WORLD_Y + y), 1)
        self._text("Watchtower", WORLD_X + 18, 30, self.title_font, theme.text)
        self._text(self.flash_message, WORLD_X + 20, 60, self.small_font, theme.muted)

    def _draw_agent(self, agent: AgentState) -> None:
        theme = self.theme
        x, y = self._agent_screen_position(agent)
        color = _hex_to_rgb(agent.profile.accent_color, theme.accent)
        bob = 0
        if agent.status is AgentStatus.WORKING:
            bob = int(round(2.5 * math.sin(self.simulation.elapsed_seconds * 6 + x)))
        cy = y + bob
        pygame.draw.circle(self.screen, _dim(color, 0.22), (x, cy), 30)
        if agent.profile.id == self.selected_agent_id:
            pygame.draw.circle(self.screen, theme.text, (x, cy), 34, width=2)
        pygame.draw.circle(self.screen, color, (x, cy), 23)
        pygame.draw.circle(self.screen, theme.bg, (x, cy), 17)
        self._draw_face(x, cy, agent.status, color)
        name = self.small_font.render(agent.profile.display_name, True, theme.text)
        self.screen.blit(name, name.get_rect(center=(x, cy + 38)))
        action = self.small_font.render(agent.action.value.replace("_", " "), True, theme.muted)
        self.screen.blit(action, action.get_rect(center=(x, cy + 54)))
        load_width = 42
        pygame.draw.rect(self.screen, theme.surface_alt, (x - 21, cy - 38, load_width, 5), border_radius=3)
        pygame.draw.rect(self.screen, color, (x - 21, cy - 38, int(load_width * agent.metrics.load), 5), border_radius=3)

    def _draw_face(self, cx: int, cy: int, status: AgentStatus, color: tuple[int, int, int]) -> None:
        feature = self.theme.muted if status is AgentStatus.OFFLINE else self.theme.text
        blink = (self.simulation.elapsed_seconds + cx * 0.13) % 3.4 < 0.12
        if status is AgentStatus.OFFLINE or blink:
            pygame.draw.line(self.screen, feature, (cx - 8, cy - 3), (cx - 4, cy - 3), 2)
            pygame.draw.line(self.screen, feature, (cx + 4, cy - 3), (cx + 8, cy - 3), 2)
        else:
            pygame.draw.circle(self.screen, feature, (cx - 6, cy - 3), 2)
            pygame.draw.circle(self.screen, feature, (cx + 6, cy - 3), 2)
        if status is AgentStatus.WORKING:
            pygame.draw.circle(self.screen, feature, (cx, cy + 6), 2, width=1)
        elif status is AgentStatus.IDLE:
            pygame.draw.lines(self.screen, feature, False, [(cx - 5, cy + 4), (cx, cy + 7), (cx + 5, cy + 4)], 2)
        elif status is AgentStatus.DEGRADED:
            pygame.draw.lines(self.screen, self.theme.danger, False, [(cx - 5, cy + 7), (cx, cy + 4), (cx + 5, cy + 7)], 2)
        else:
            pygame.draw.line(self.screen, feature, (cx - 5, cy + 6), (cx + 5, cy + 6), 2)

    def _draw_task_stations(self, tasks: list[SubmittedTask]) -> None:
        theme = self.theme
        self._station_hits = []
        active = [task for task in tasks if task.status in {TaskStatus.ASSIGNED, TaskStatus.IN_PROGRESS}]
        for index, task in enumerate(active[:5]):
            rect = pygame.Rect(WORLD_X + 84 + index * 145, WORLD_Y + WORLD_HEIGHT - 40, 106, 30)
            pygame.draw.rect(self.screen, theme.surface_alt, rect, border_radius=6)
            pygame.draw.rect(self.screen, _priority_color(task.priority, theme), rect, width=1, border_radius=6)
            pygame.draw.rect(self.screen, theme.accent, (rect.x, rect.y, int(rect.width * task.progress), 4), border_radius=2)
            label = self.small_font.render(task.id, True, theme.text)
            self.screen.blit(label, label.get_rect(center=rect.center))
            self._station_hits.append((rect, task.id))

    def _draw_effects(self) -> None:
        for x, y, age, ttl in self.effects:
            progress = age / ttl
            radius = int(20 + 36 * progress)
            color = _blend(self.theme.success, self.theme.bg, progress)
            pygame.draw.circle(self.screen, color, (int(x), int(y)), radius, width=2)

    def _draw_panel(self, snapshot, provider_snapshot) -> None:
        theme = self.theme
        panel = pygame.Rect(PANEL_X, 16, self.screen_width - PANEL_X - 16, WORLD_HEIGHT)
        pygame.draw.rect(self.screen, theme.surface, panel, border_radius=8)
        self._text("Models", PANEL_X + 18, 34, self.title_font, theme.text)
        y = 72
        for index, agent in enumerate(snapshot.agents):
            color = _hex_to_rgb(agent.profile.accent_color, theme.accent)
            row = self._agent_row_rect(index)
            if agent.profile.id == self.selected_agent_id:
                pygame.draw.rect(self.screen, theme.surface_alt, row, border_radius=6)
                pygame.draw.rect(self.screen, color, row, width=1, border_radius=6)
            pygame.draw.circle(self.screen, color, (PANEL_X + 28, y + 9), 7)
            self._text(agent.profile.model_name[:26], PANEL_X + 44, y, self.font, theme.text)
            connection = "live key" if self.model_api.is_configured(agent.profile) else agent.profile.provider
            status = f"{agent.status.value} | {connection} | {agent.metrics.latency_ms:.0f} ms"
            self._text(status, PANEL_X + 44, y + 18, self.small_font, theme.muted)
            y += 48

        route = f"Route: {self._agent_label(self.selected_agent_id)}"
        self._text(route, PANEL_X + 18, y + 6, self.small_font, theme.muted)
        y += 36
        self._text("Tasks", PANEL_X + 18, y, self.title_font, theme.text)
        y += 38
        self._draw_task_list(snapshot.tasks, y)

        activity_y = WORLD_HEIGHT - 96
        self._text("Activity", PANEL_X + 18, activity_y, self.title_font, theme.text)
        ey = activity_y + 30
        for event in snapshot.events[-4:][::-1]:
            message = f"{event.elapsed_seconds:05.1f}s {event.agent_id}: {event.message or event.action.value}"
            self._text(message[:38], PANEL_X + 18, ey, self.small_font, theme.muted)
            ey += 18

        auth = f"Auth: {provider_snapshot.auth_mode} | Feed: {provider_snapshot.source_label}"
        self._text(auth[:42], PANEL_X + 18, WORLD_HEIGHT + 2, self.small_font, theme.muted)
        if provider_snapshot.last_error:
            self._text(provider_snapshot.last_error[:42], PANEL_X + 18, WORLD_HEIGHT + 20, self.small_font, theme.danger)

    def _draw_task_list(self, tasks: list[SubmittedTask], y: int) -> None:
        theme = self.theme
        self._panel_task_hits = []
        visible_count = 4
        max_scroll = max(0, len(tasks) - visible_count)
        self.task_scroll = min(self.task_scroll, max_scroll)
        window = tasks[self.task_scroll:self.task_scroll + visible_count]
        for task in window:
            rect = pygame.Rect(PANEL_X + 18, y, 224, 50)
            color = theme.success if task.status is TaskStatus.COMPLETE else _priority_color(task.priority, theme)
            if task.status is TaskStatus.FAILED:
                color = theme.danger
            pygame.draw.rect(self.screen, theme.surface_alt, rect, border_radius=6)
            pygame.draw.rect(self.screen, color, (rect.x, rect.y, 4, 50), border_radius=2)
            self._text(task.title[:30], PANEL_X + 30, y + 7, self.small_font, theme.text)
            route_label = self._agent_label(task.assigned_agent_id or task.requested_agent_id)
            meta = f"{task.status.value} {task.progress * 100:>3.0f}% | {route_label}"
            if task.model_error:
                meta = f"api error | {route_label}"
            elif task.model_response:
                meta = f"done via api | {route_label}"
            elif task.model_partial:
                meta = f"streaming... | {route_label}"
            self._text(meta[:34], PANEL_X + 30, y + 27, self.small_font, theme.muted)
            self._panel_task_hits.append((rect, task.id))
            y += 58
        if max_scroll:
            self._text(f"scroll {self.task_scroll}/{max_scroll}", PANEL_X + 18, y, self.small_font, theme.muted)

    def _draw_input(self) -> None:
        theme = self.theme
        rect = pygame.Rect(16, self.screen_height - 86, self.screen_width - 150, 52)
        pygame.draw.rect(self.screen, theme.surface, rect, border_radius=8)
        pygame.draw.rect(self.screen, theme.accent, rect, width=1, border_radius=8)
        target = self._agent_label(self.selected_agent_id)
        placeholder = f"Task to {target} | @gpt .. | @all .. | !high .. | /compare | F1 help"
        text = self.input_text or placeholder
        color = theme.text if self.input_text else theme.muted
        self._text(text[-120:], rect.x + 16, rect.y + 17, self.font, color)
        submit = self._submit_rect()
        pygame.draw.rect(self.screen, theme.accent, submit, border_radius=8)
        self._text("Submit", submit.x + 26, submit.y + 17, self.font, theme.bg)

    def _draw_detail(self, task: SubmittedTask) -> None:
        theme = self.theme
        self._draw_backdrop()
        rect = self._detail_rect()
        pygame.draw.rect(self.screen, theme.surface, rect, border_radius=10)
        pygame.draw.rect(self.screen, theme.accent, rect, width=1, border_radius=10)
        pad = 18
        x = rect.x + pad
        y = rect.y + pad
        self._text(task.title[:48], x, y, self.title_font, theme.text)
        y += 32
        route = self._agent_label(task.assigned_agent_id or task.requested_agent_id)
        meta = f"{task.status.value} | {route} | {task.priority.value} | {task.model_latency_ms:.0f} ms"
        self._text(meta, x, y, self.small_font, theme.muted)
        y += 26
        text_width = rect.width - 2 * pad
        self._text("Prompt", x, y, self.font, theme.accent)
        y += 22
        for line in self._wrap(task.prompt, self.small_font, text_width)[:3]:
            self._text(line, x, y, self.small_font, theme.text)
            y += 18
        y += 8
        body = task.model_response or task.model_partial or task.model_error or "(waiting for response)"
        label = "Error" if task.model_error else "Response"
        self._text(label, x, y, self.font, theme.danger if task.model_error else theme.accent)
        y += 22
        buttons_top = rect.bottom - 56
        max_lines = max(1, (buttons_top - y) // 18)
        for line in self._wrap(body, self.small_font, text_width)[:max_lines]:
            self._text(line, x, y, self.small_font, theme.text)
            y += 18
        for name, brect in self._detail_button_rects(task).items():
            danger = name in {"Delete", "Cancel"}
            pygame.draw.rect(self.screen, theme.danger if danger else theme.accent, brect, border_radius=6)
            self._text(name, brect.x + 10, brect.y + 7, self.small_font, theme.bg)

    def _draw_help(self) -> None:
        theme = self.theme
        self._draw_backdrop()
        rect = pygame.Rect(0, 0, min(560, self.screen_width - 80), min(560, self.screen_height - 80))
        rect.center = (self.screen_width // 2, self.screen_height // 2)
        pygame.draw.rect(self.screen, theme.surface, rect, border_radius=10)
        pygame.draw.rect(self.screen, theme.accent, rect, width=1, border_radius=10)
        x, y = rect.x + 20, rect.y + 18
        self._text("Watchtower - keys & commands", x, y, self.title_font, theme.text)
        y += 38
        lines = [
            "Enter           submit task / command",
            "Drag todo       drop a todo card onto an agent",
            "Click task      open its detail (prompt + response)",
            "Click agent     select for routing",
            "Ctrl/Alt+1-5    select an agent",
            "Mouse wheel     scroll the task list",
            "F1              toggle this help    F2  toggle theme",
            "Esc             close overlay / quit",
            "",
            "@gpt <prompt>   target one agent",
            "@all <prompt>   compare across every agent",
            "!high <prompt>  set priority (low/normal/high/critical)",
            "/auto           load-based routing",
            "/compare <p>    fan a prompt to every agent",
            "/cancel <id>    /retry <id>    /clear",
            "/key PROVIDER VALUE   set an API key (local = base url)",
            "/agent add ID PROVIDER MODEL [Name]   /agent remove ID",
            "/theme [dark|light]   /save [path]   /load [path]",
            "/export <id> [path]   write a response to disk",
            "/endpoint URL   /auth token TOK   /auth login U P",
        ]
        for line in lines:
            self._text(line, x, y, self.small_font, theme.muted if line else theme.text)
            y += 20

    def _draw_backdrop(self) -> None:
        overlay = pygame.Surface((self.screen_width, self.screen_height), pygame.SRCALPHA)
        overlay.fill((*self.theme.overlay, 190))
        self.screen.blit(overlay, (0, 0))

    # ----- detail interaction ------------------------------------------------
    def _detail_rect(self) -> pygame.Rect:
        rect = pygame.Rect(0, 0, min(620, self.screen_width - 80), min(460, self.screen_height - 120))
        rect.center = (self.screen_width // 2, self.screen_height // 2)
        return rect

    def _detail_button_rects(self, task: SubmittedTask) -> dict[str, pygame.Rect]:
        rect = self._detail_rect()
        names: list[str] = []
        if task.status in {TaskStatus.COMPLETE, TaskStatus.FAILED, TaskStatus.CANCELLED}:
            names.append("Retry")
        if task.is_active or task.status is TaskStatus.SUBMITTED:
            names.append("Cancel")
        if task.model_response or task.model_error:
            names.append("Export")
        names.extend(["Delete", "Close"])
        buttons: dict[str, pygame.Rect] = {}
        bx = rect.x + 18
        by = rect.bottom - 46
        for name in names:
            width = max(60, self.small_font.size(name)[0] + 20)
            buttons[name] = pygame.Rect(bx, by, width, 30)
            bx += width + 8
        return buttons

    def _handle_detail_click(self, pos: tuple[int, int]) -> None:
        task = self.simulation.tasks.get(self.selected_task_id or "")
        if task is None or not self._detail_rect().collidepoint(pos):
            self.selected_task_id = None
            return
        for name, brect in self._detail_button_rects(task).items():
            if brect.collidepoint(pos):
                self._detail_action(name, task)
                return

    def _detail_action(self, name: str, task: SubmittedTask) -> None:
        if name == "Close":
            self.selected_task_id = None
        elif name == "Cancel":
            self._cancel_task(task.id)
        elif name == "Retry":
            self._retry_task(task.id)
        elif name == "Delete":
            self.simulation.remove_task(task.id)
            self.running_model_tasks.discard(task.id)
            self.selected_task_id = None
        elif name == "Export":
            saved = export_task_text(f"{task.id}.md", task)
            self.flash_message = f"Exported {saved.name}"

    def _station_at(self, pos: tuple[int, int]) -> str | None:
        for rect, task_id in self._station_hits:
            if rect.collidepoint(pos):
                return task_id
        return None

    def _panel_task_at(self, pos: tuple[int, int]) -> str | None:
        for rect, task_id in self._panel_task_hits:
            if rect.collidepoint(pos):
                return task_id
        return None

    # ----- helpers -----------------------------------------------------------
    def _submit_rect(self) -> pygame.Rect:
        return pygame.Rect(self.screen_width - 120, self.screen_height - 86, 104, 52)

    def _text(self, text: str, x: int, y: int, font: pygame.font.Font, color: tuple[int, int, int]) -> None:
        self.screen.blit(font.render(text, True, color), (x, y))

    def _wrap(self, text: str, font: pygame.font.Font, max_width: int) -> list[str]:
        lines: list[str] = []
        for paragraph in text.split("\n"):
            if not paragraph:
                lines.append("")
                continue
            current = ""
            for word in paragraph.split(" "):
                trial = f"{current} {word}".strip()
                if font.size(trial)[0] <= max_width or not current:
                    current = trial
                else:
                    lines.append(current)
                    current = word
            if current:
                lines.append(current)
        return lines

    def _agent_row_rect(self, index: int) -> pygame.Rect:
        return pygame.Rect(PANEL_X + 14, 66 + index * 48, 232, 42)

    def _draw_todo_panel(self, tasks: list[SubmittedTask]) -> None:
        theme = self.theme
        panel = pygame.Rect(16, 16, LEFT_PANEL_WIDTH, WORLD_HEIGHT)
        pygame.draw.rect(self.screen, theme.surface, panel, border_radius=8)
        self._text("Todo", 34, 34, self.title_font, theme.text)
        self._text("Drag tasks onto agents", 34, 64, self.small_font, theme.muted)
        todo_tasks = [task for task in tasks if task.status is TaskStatus.TODO]
        if not todo_tasks:
            self._text("Type a task below", 34, 104, self.font, theme.muted)
            self._text("then drag it into play", 34, 126, self.font, theme.muted)
        for index, task in enumerate(todo_tasks[:8]):
            rect = self._todo_task_rect(index)
            if task.id == self.dragging_task_id:
                continue
            self._draw_todo_card(task, rect)
        if self.dragging_task_id:
            task = self.simulation.tasks.get(self.dragging_task_id)
            if task:
                mouse_x, mouse_y = pygame.mouse.get_pos()
                self._draw_todo_card(task, pygame.Rect(mouse_x - 88, mouse_y - 24, 176, 48), ghost=True)

    def _draw_todo_card(self, task: SubmittedTask, rect: pygame.Rect, ghost: bool = False) -> None:
        theme = self.theme
        fill = theme.surface_alt if not ghost else _blend(theme.surface_alt, theme.accent, 0.35)
        pygame.draw.rect(self.screen, fill, rect, border_radius=8)
        pygame.draw.rect(self.screen, _priority_color(task.priority, theme), rect, width=1, border_radius=8)
        self._text(task.title[:24], rect.x + 10, rect.y + 8, self.small_font, theme.text)
        self._text("grab and drop", rect.x + 10, rect.y + 27, self.small_font, theme.muted)

    def _todo_task_rect(self, index: int) -> pygame.Rect:
        return pygame.Rect(32, 96 + index * 58, LEFT_PANEL_WIDTH - 32, 48)

    def _todo_task_at(self, position: tuple[int, int]) -> str | None:
        todo_tasks = [task for task in self.simulation.snapshot().tasks if task.status is TaskStatus.TODO]
        for index, task in enumerate(todo_tasks[:8]):
            if self._todo_task_rect(index).collidepoint(position):
                return task.id
        return None

    def _finish_drag(self, position: tuple[int, int]) -> None:
        if not self.dragging_task_id:
            return
        task_id = self.dragging_task_id
        self.dragging_task_id = None
        agent_id = self._agent_at(position)
        if not agent_id:
            self.flash_message = "Drop the todo on an agent"
            return
        self.simulation.assign_todo_task(task_id, agent_id)
        self.flash_message = f"Todo assigned to {self._agent_label(agent_id)}"

    def _agent_at(self, position: tuple[int, int]) -> str | None:
        px, py = position
        for agent in self.simulation.snapshot().agents:
            ax, ay = self._agent_screen_position(agent)
            if (px - ax) ** 2 + (py - ay) ** 2 <= 38**2:
                return agent.profile.id
        return None

    def _agent_screen_position(self, agent: AgentState) -> tuple[int, int]:
        return int(WORLD_X + agent.position.x), int(WORLD_Y + agent.position.y)

    def _handle_selection_click(self, position: tuple[int, int]) -> bool:
        for index, agent in enumerate(self.simulation.snapshot().agents):
            if self._agent_row_rect(index).collidepoint(position):
                self.selected_agent_id = agent.profile.id
                self.flash_message = f"Selected {agent.profile.display_name}"
                return True
        return False

    def _select_agent_by_index(self, index: int) -> None:
        agents = self.simulation.snapshot().agents
        if 0 <= index < len(agents):
            self.selected_agent_id = agents[index].profile.id
            self.flash_message = f"Selected {agents[index].profile.display_name}"

    def _is_agent_shortcut(self, event: pygame.event.Event) -> bool:
        modifier = event.mod & (pygame.KMOD_CTRL | pygame.KMOD_ALT | pygame.KMOD_META)
        return bool(modifier and pygame.K_1 <= event.key <= pygame.K_5)

    def _agent_label(self, agent_id: str | None) -> str:
        if not agent_id:
            return "auto"
        agent = self.simulation.agents.get(agent_id)
        return agent.profile.display_name if agent else agent_id


def _hex_to_rgb(value: str, fallback: tuple[int, int, int]) -> tuple[int, int, int]:
    clean = value.strip().lstrip("#")
    if len(clean) != 6:
        return fallback
    return tuple(int(clean[index : index + 2], 16) for index in (0, 2, 4))


def _dim(color: tuple[int, int, int], factor: float) -> tuple[int, int, int]:
    return tuple(max(0, min(255, int(channel * factor))) for channel in color)


def _blend(color: tuple[int, int, int], other: tuple[int, int, int], amount: float) -> tuple[int, int, int]:
    amount = max(0.0, min(1.0, amount))
    return tuple(int(c + (o - c) * amount) for c, o in zip(color, other))


def _priority_color(priority: TaskPriority, theme: Theme) -> tuple[int, int, int]:
    return {
        TaskPriority.CRITICAL: theme.danger,
        TaskPriority.HIGH: theme.warning,
        TaskPriority.NORMAL: theme.accent,
        TaskPriority.LOW: theme.muted,
    }[priority]
