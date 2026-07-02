from __future__ import annotations

import asyncio
import math
import os
import queue
import threading
from collections import deque
from dataclasses import dataclass
from pathlib import Path

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
from watchtower.widgets import Button, Dropdown, TextInput, Toggle, rounded_alpha_surface


LEFT_PANEL_WIDTH = 236
WORLD_X = LEFT_PANEL_WIDTH + 24
WORLD_Y = 16
PANEL_X = WORLD_X + WORLD_WIDTH + 16
SCREEN_WIDTH = PANEL_X + 260
SCREEN_HEIGHT = 760
MIN_WIDTH = SCREEN_WIDTH
MIN_HEIGHT = SCREEN_HEIGHT

# Palette for agents added at runtime via /agent add.
AGENT_COLORS = ["#67e8b9", "#ff9f6e", "#84a7ff", "#c09bff", "#f4cf6f", "#6ee7f2", "#ff8bb8"]

DEFAULT_SAVE_PATH = "watchtower_session.json"
AUTOSAVE_PATH = Path.home() / ".watchtower" / "autosave.json"
AUTOSAVE_INTERVAL = 20.0
SPARK_INTERVAL = 0.25
SPARK_SAMPLES = 40

# Rough $/1k-token estimates for a token/cost readout. Deliberately coarse and
# clearly labelled "est." in the UI; not meant to be billing-accurate.
PRICE_PER_1K = {"openai": 0.005, "anthropic": 0.006, "gemini": 0.001, "local": 0.0}


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
    bg=(4, 5, 7),
    surface=(13, 14, 18),
    surface_alt=(29, 31, 38),
    text=(239, 241, 246),
    muted=(145, 149, 161),
    grid=(54, 57, 66),
    accent=(141, 164, 255),
    success=(92, 229, 170),
    danger=(255, 116, 132),
    warning=(245, 200, 95),
    overlay=(3, 4, 6),
)

LIGHT_THEME = Theme(
    name="light",
    bg=(230, 232, 236),
    surface=(247, 248, 251),
    surface_alt=(221, 224, 231),
    text=(20, 22, 27),
    muted=(99, 105, 117),
    grid=(193, 199, 209),
    accent=(75, 101, 225),
    success=(35, 152, 103),
    danger=(202, 67, 84),
    warning=(173, 124, 28),
    overlay=(204, 208, 216),
)

PRIORITY_NAMES = {p.value: p for p in TaskPriority}


class WatchtowerApp:
    def __init__(self, web_mode: bool = False) -> None:
        self.web_mode = web_mode
        pygame.init()
        pygame.display.set_caption("Watchtower")
        self.screen_width = SCREEN_WIDTH
        self.screen_height = SCREEN_HEIGHT
        self.screen = pygame.display.set_mode((self.screen_width, self.screen_height), pygame.RESIZABLE)
        self.clock = pygame.time.Clock()
        self.font = pygame.font.SysFont("helvetica neue,arial", 16)
        self.small_font = pygame.font.SysFont("helvetica neue,arial", 13)
        self.title_font = pygame.font.SysFont("helvetica neue,arial", 24, bold=True)
        self.badge_font = pygame.font.SysFont("helvetica neue,arial", 14, bold=True)
        self.theme = DARK_THEME
        self.simulation = SimulationState()
        self.auth_config = AuthConfig.from_env()
        self.provider = AgentDataProvider(self.auth_config)
        self.poller = TelemetryPoller(self.provider, self.simulation.profiles)
        self.model_api = ModelApiClient()
        self.model_results: queue.Queue[tuple[str, str, int, object]] = queue.Queue()
        self.running_model_tasks: set[str] = set()
        self._dispatch_token: dict[str, int] = {}
        self._dispatch_seq = 0
        self.text_input = TextInput(pygame.Rect(16, 0, 200, 52), focused=True)
        self.add_agent_inputs = {
            "id": TextInput(pygame.Rect(0, 0, 10, 30), placeholder="id e.g. qwen"),
            "provider": TextInput(pygame.Rect(0, 0, 10, 30), placeholder="provider (openai/anthropic/gemini/local)"),
            "model": TextInput(pygame.Rect(0, 0, 10, 30), placeholder="model"),
            "name": TextInput(pygame.Rect(0, 0, 10, 30), placeholder="display name (optional)"),
        }
        self.focus: TextInput | None = self.text_input
        self.menu = Dropdown("Menu", pygame.Rect(16, 0, 120, 26))
        self.menu.items = [
            ("Compare input prompt", self._toolbar_compare),
            ("Clear finished tasks", self._toolbar_clear),
            ("Add agent...", self._open_add_agent),
            ("Settings...", self._open_settings),
            ("Save session", self._menu_save),
            ("Load session", self._menu_load),
            ("Toggle theme", self._toggle_theme),
            ("Help (F1)", self._open_help),
            ("Quit", self._quit),
        ]
        self.autosave_enabled = not web_mode and not os.getenv("WATCHTOWER_NO_AUTOSAVE")
        self.flash_message = "Ready - F1 help, F2 theme"
        self.selected_agent_id: str | None = None
        self.selected_task_id: str | None = None
        self.compare_group_id: str | None = None
        self.inspect_agent_id: str | None = None
        self.show_help = False
        self.show_settings = False
        self.show_add_agent = False
        self.dragging_task_id: str | None = None
        self.task_scroll = 0
        self.task_cursor = 0
        self.compare_scroll = 0
        self.effects: list[list[float]] = []
        self.metric_history: dict[str, deque[float]] = {}
        self._spark_timer = 0.0
        self._autosave_timer = 0.0
        self._completed_seen: set[str] = set()
        self._station_hits: list[tuple[pygame.Rect, str]] = []
        self._panel_task_hits: list[tuple[pygame.Rect, str]] = []
        self._bg_surface: pygame.Surface | None = None
        self._bg_cache_key: tuple[int, int, str] | None = None
        self.running = True
        if not web_mode and not os.getenv("WATCHTOWER_NO_AUTOSAVE"):
            self._restore_autosave()

    # The main prompt is backed by the TextInput widget.
    @property
    def input_text(self) -> str:
        return self.text_input.value

    @input_text.setter
    def input_text(self, value: str) -> None:
        self.text_input.set(value)

    def _blink(self) -> bool:
        return (self.simulation.elapsed_seconds % 1.0) < 0.5

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
                self._sample_metrics(dt)
                self._maybe_autosave(dt)
                self._draw(provider_snapshot)
                pygame.display.flip()
        finally:
            if self.autosave_enabled:
                self._autosave()
            self.poller.stop()

    async def run_async(self) -> None:
        """Browser/pygbag entry point.

        Single-threaded: no telemetry poller thread and no real model-call
        threads (both impossible under Emscripten). Telemetry is computed
        inline from the deterministic demo feed, and the loop yields to the
        browser every frame with ``await asyncio.sleep(0)``.
        """
        snapshot = self.provider.demo_snapshot(self.simulation.profiles)
        poll_timer = 0.0
        while self.running:
            dt = self.clock.tick(60) / 1000
            self._handle_events()
            poll_timer += dt
            if poll_timer >= 2.0:
                poll_timer = 0.0
                snapshot = self.provider.demo_snapshot(self.simulation.profiles)
            self.simulation.update(dt, snapshot.telemetry)
            self._sync_completion_effects()
            self._update_effects(dt)
            self._sample_metrics(dt)
            self._draw(snapshot)
            pygame.display.flip()
            await asyncio.sleep(0)
            pygame.quit()

    # ----- events ------------------------------------------------------------
    def _handle_events(self) -> None:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                self.running = False
            elif event.type == pygame.VIDEORESIZE:
                self._resize(event.w, event.h)
            elif event.type == pygame.MOUSEWHEEL:
                if self.compare_group_id:
                    self.compare_scroll = max(0, self.compare_scroll - event.y)
                else:
                    self.task_scroll = max(0, self.task_scroll - event.y)
            elif event.type == pygame.KEYDOWN:
                self._handle_keydown(event)
            elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                self._handle_mouse_down(event.pos)
            elif event.type == pygame.MOUSEBUTTONUP and event.button == 1:
                self._finish_drag(event.pos)

    def _handle_keydown(self, event: pygame.event.Event) -> None:
        if event.key == pygame.K_ESCAPE:
            if not self._close_overlay():
                self.running = False
        elif event.key == pygame.K_F1:
            self.show_help = not self.show_help
        elif event.key == pygame.K_F2:
            self._toggle_theme()
        elif self._is_agent_shortcut(event):
            self._select_agent_by_index(event.key - pygame.K_1)
        elif event.key == pygame.K_TAB and self.show_add_agent:
            self._cycle_add_agent_focus()
        elif event.key in (pygame.K_DOWN, pygame.K_UP) and not self.input_text and self.focus is self.text_input:
            self._move_task_cursor(1 if event.key == pygame.K_DOWN else -1)
        elif event.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
            self._handle_return()
        elif self.focus is not None:
            self.focus.handle_key(event)

    def _close_overlay(self) -> bool:
        if self.show_add_agent:
            self._close_add_agent()
        elif self.show_settings:
            self._close_settings()
        elif self.show_help:
            self.show_help = False
        elif self.selected_task_id:
            self.selected_task_id = None
        elif self.compare_group_id:
            self.compare_group_id = None
        elif self.inspect_agent_id:
            self.inspect_agent_id = None
        elif self.menu.open:
            self.menu.open = False
        else:
            return False
        return True

    def _handle_return(self) -> None:
        if self.show_add_agent:
            self._create_agent_from_dialog()
            return
        if self.show_settings:
            return
        # With an empty input box, Enter opens the keyboard-focused task.
        if not self.input_text.strip():
            tasks = self.simulation.snapshot().tasks
            if tasks:
                self.task_cursor = min(self.task_cursor, len(tasks) - 1)
                self.selected_task_id = tasks[self.task_cursor].id
                return
        self._submit_input()

    def _move_task_cursor(self, delta: int) -> None:
        tasks = self.simulation.snapshot().tasks
        if not tasks:
            return
        self.task_cursor = max(0, min(len(tasks) - 1, self.task_cursor + delta))
        if self.task_cursor < self.task_scroll:
            self.task_scroll = self.task_cursor
        elif self.task_cursor >= self.task_scroll + 4:
            self.task_scroll = self.task_cursor - 3

    def _handle_mouse_down(self, pos: tuple[int, int]) -> None:
        self._layout_widgets()
        if self.show_settings:
            self._settings_click(pos)
            return
        if self.show_add_agent:
            self._add_agent_click(pos)
            return
        if self.show_help:
            self.show_help = False
            return
        if self.selected_task_id:
            self._handle_detail_click(pos)
            return
        if self.compare_group_id:
            self._handle_compare_click(pos)
            return
        if self.inspect_agent_id:
            self._handle_inspect_click(pos)
            return
        # Toolbar menu: clicks on the button/items are consumed; a click
        # elsewhere closes the menu and still falls through to act on the target.
        if self.menu.open:
            if self.menu.handle_click(pos):
                return
        elif self.menu.rect.collidepoint(pos):
            self.menu.handle_click(pos)
            return
        for button in self._toolbar_buttons():
            if button.handle_click(pos):
                return
        todo_task_id = self._todo_task_at(pos)
        if todo_task_id:
            self.dragging_task_id = todo_task_id
            return
        if self._submit_rect().collidepoint(pos):
            self._submit_input()
            return
        if self.text_input.handle_click(pos):
            self._focus(self.text_input)
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
            self.inspect_agent_id = world_agent
            self.flash_message = f"Inspecting {self._agent_label(world_agent)}"

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
        self._invalidate_dispatch(task_id)
        self.flash_message = f"Cancelled {task_id}"

    def _retry_task(self, task_id: str) -> None:
        self.simulation.retry_task(task_id)
        self._invalidate_dispatch(task_id)
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
            self._invalidate_dispatches(self.simulation.remove_agent(agent_id))
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
            name = parts[3] if len(parts) > 3 else ""
            self._add_agent(parts[0], parts[1], parts[2], name)
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
        self._dispatch_token.clear()
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

    # ----- autosave + telemetry history -------------------------------------
    def _restore_autosave(self) -> None:
        if not AUTOSAVE_PATH.exists():
            return
        try:
            profiles, tasks = load_session(AUTOSAVE_PATH)
        except (OSError, ValueError):
            return
        if not profiles:
            return
        self.simulation = SimulationState(profiles=profiles)
        for task in tasks:
            if not task.is_finished:
                task.reset_for_retry()
            self.simulation.tasks[task.id] = task
        self.poller.set_profiles(profiles)
        self._completed_seen = {task.id for task in tasks if task.status is TaskStatus.COMPLETE}
        self.flash_message = f"Restored {len(tasks)} tasks"

    def _autosave(self) -> None:
        try:
            save_session(AUTOSAVE_PATH, self.simulation.profiles, list(self.simulation.tasks.values()))
        except OSError:
            pass

    def _maybe_autosave(self, dt: float) -> None:
        if not self.autosave_enabled:
            return
        self._autosave_timer += dt
        if self._autosave_timer >= AUTOSAVE_INTERVAL:
            self._autosave_timer = 0.0
            self._autosave()

    def _sample_metrics(self, dt: float) -> None:
        self._spark_timer += dt
        if self._spark_timer < SPARK_INTERVAL:
            return
        self._spark_timer = 0.0
        for agent in self.simulation.agents.values():
            history = self.metric_history.setdefault(agent.profile.id, deque(maxlen=SPARK_SAMPLES))
            history.append(agent.metrics.load)
        for stale in set(self.metric_history) - set(self.simulation.agents):
            self.metric_history.pop(stale, None)

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
            self._dispatch_seq += 1
            token = self._dispatch_seq
            self._dispatch_token[task.id] = token
            thread = threading.Thread(
                target=self._run_model_task,
                args=(task.id, agent.profile, task.prompt, token),
                name=f"watchtower-model-{task.id}",
                daemon=True,
            )
            thread.start()

    def _run_model_task(self, task_id: str, profile: AgentProfile, prompt: str, token: int) -> None:
        def on_delta(delta: str) -> None:
            self.model_results.put(("delta", task_id, token, delta))

        try:
            result = asyncio.run(self.model_api.run_task(profile, prompt, on_delta=on_delta))
            self.model_results.put(("done", task_id, token, result))
        except Exception as exc:
            self.model_results.put(("error", task_id, token, f"{type(exc).__name__}: {exc}"))

    def _invalidate_dispatch(self, task_id: str) -> None:
        """Forget any in-flight model call for a task so a late result is ignored."""
        self.running_model_tasks.discard(task_id)
        self._dispatch_token.pop(task_id, None)

    def _invalidate_dispatches(self, task_ids: list[str]) -> None:
        for task_id in task_ids:
            self._invalidate_dispatch(task_id)

    def _drain_model_results(self) -> None:
        while True:
            try:
                kind, task_id, token, payload = self.model_results.get_nowait()
            except queue.Empty:
                return
            is_current = self._dispatch_token.get(task_id) == token
            task = self.simulation.tasks.get(task_id)
            if kind == "delta":
                if task is not None and is_current:
                    task.append_partial(str(payload))
                continue
            # A "done"/"error" means this dispatch's thread finished. Ignore it if a
            # newer dispatch superseded it (agent removed, task retried/cancelled).
            if not is_current:
                continue
            self._invalidate_dispatch(task_id)
            if task is None:
                continue
            agent_id = task.assigned_agent_id
            if kind == "error":
                task.mark_model_error(str(payload)[:180])
                self.flash_message = f"{task.id} API error"
            else:
                result = payload
                assert isinstance(result, ModelCallResult)
                text = result.text[:4000]
                tokens = result.total_tokens or _estimate_tokens(task.prompt, text)
                task.mark_model_result(text, latency_ms=result.latency_ms, tokens=tokens)
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
        self._layout_widgets()
        if self.selected_task_id and self.selected_task_id not in self.simulation.tasks:
            self.selected_task_id = None
        if self.inspect_agent_id and self.inspect_agent_id not in self.simulation.agents:
            self.inspect_agent_id = None
        self._draw_app_background()
        snapshot = self.simulation.snapshot()
        self._draw_todo_panel(snapshot.tasks)
        self._draw_world()
        for agent in snapshot.agents:
            self._draw_agent(agent)
        self._draw_task_stations(snapshot.tasks)
        self._draw_effects()
        self._draw_panel(snapshot, provider_snapshot)
        self._draw_input()
        modal_open = self.show_settings or self.show_add_agent or self.show_help or self.selected_task_id or self.compare_group_id or self.inspect_agent_id
        if self.menu.open and not modal_open:
            self.menu.draw_items(self.screen, self.theme, self.small_font, pygame.mouse.get_pos())
        if self.selected_task_id:
            self._draw_detail(self.simulation.tasks[self.selected_task_id])
        if self.compare_group_id:
            self._draw_compare()
        if self.inspect_agent_id:
            self._draw_inspect()
        if self.show_help:
            self._draw_help()
        if self.show_settings:
            self._draw_settings()
        if self.show_add_agent:
            self._draw_add_agent()

    def _layout_widgets(self) -> None:
        input_y = self.screen_height - 86
        self.text_input.rect = pygame.Rect(16, input_y, self.screen_width - 150, 52)
        self.menu.rect = pygame.Rect(16, self.screen_height - 120, 120, 26)

    def _toolbar_buttons(self) -> list[Button]:
        toolbar_y = self.screen_height - 120
        x = 16 + self.menu.rect.width + 8
        specs = [
            ("Compare", self._toolbar_compare),
            ("Clear", self._toolbar_clear),
            ("Theme", self._toggle_theme),
            ("Settings", self._open_settings),
        ]
        buttons = []
        for label, callback in specs:
            width = max(72, self.small_font.size(label)[0] + 22)
            buttons.append(Button(label, pygame.Rect(x, toolbar_y, width, 26), callback, style="ghost"))
            x += width + 8
        return buttons

    # ----- toolbar / menu actions -------------------------------------------
    def _toolbar_compare(self) -> None:
        text = self.input_text.strip()
        if not text:
            self.flash_message = "Type a prompt, then Compare"
            return
        # Compare always fans out, so honour !priority and strip a leading
        # @all / @agent target rather than sending it as literal prompt text.
        if text.lower().startswith("@all ") or text.lower() == "@all":
            text = text[4:].strip()
        else:
            agent_id, stripped = self._parse_targeted_prompt(text)
            if agent_id:
                text = stripped
        priority, rest = self._parse_priority(text)
        if not rest:
            self.flash_message = "Type a prompt to compare"
            return
        self.input_text = ""
        tasks = self.simulation.submit_comparison(rest, priority=priority)
        self.flash_message = f"Comparing across {len(tasks)} agents"

    def _toolbar_clear(self) -> None:
        removed = self.simulation.clear_finished()
        self.task_scroll = 0
        self.flash_message = f"Cleared {removed} finished tasks"

    def _menu_save(self) -> None:
        saved = save_session(DEFAULT_SAVE_PATH, self.simulation.profiles, list(self.simulation.tasks.values()))
        self.flash_message = f"Saved to {saved.name}"

    def _menu_load(self) -> None:
        self._handle_load(DEFAULT_SAVE_PATH)

    def _open_help(self) -> None:
        self.menu.open = False
        self.show_help = True

    def _quit(self) -> None:
        self.running = False

    def _focus(self, widget: TextInput | None) -> None:
        self.text_input.focused = widget is self.text_input
        for field in self.add_agent_inputs.values():
            field.focused = widget is field
        self.focus = widget

    # ----- settings dialog ---------------------------------------------------
    def _open_settings(self) -> None:
        self.menu.open = False
        self.show_add_agent = False
        self.show_settings = True
        self._focus(None)

    def _close_settings(self) -> None:
        self.show_settings = False
        self._focus(self.text_input)

    def _settings_rect(self) -> pygame.Rect:
        rect = pygame.Rect(0, 0, min(480, self.screen_width - 80), min(340, self.screen_height - 120))
        rect.center = (self.screen_width // 2, self.screen_height // 2)
        return rect

    def _set_theme(self, light: bool) -> None:
        self.theme = LIGHT_THEME if light else DARK_THEME

    def _set_autosave(self, value: bool) -> None:
        self.autosave_enabled = value

    def _settings_widgets(self) -> tuple[list[Toggle], list[Button]]:
        rect = self._settings_rect()
        x = rect.x + 20
        toggles = [
            Toggle(pygame.Rect(x, rect.y + 60, 240, 22), "Light theme", self.theme is LIGHT_THEME, self._set_theme),
            Toggle(pygame.Rect(x, rect.y + 96, 240, 22), "Auto-save session", self.autosave_enabled, self._set_autosave),
        ]
        buttons: list[Button] = []
        bx, by = x, rect.y + 150
        for label, callback in (("Save now", self._menu_save), ("Load", self._menu_load), ("Clear finished", self._toolbar_clear), ("Add agent...", self._open_add_agent)):
            width = max(96, self.small_font.size(label)[0] + 22)
            if bx + width > rect.right - 20:
                bx, by = x, by + 38
            buttons.append(Button(label, pygame.Rect(bx, by, width, 30), callback, style="ghost"))
            bx += width + 8
        buttons.append(Button("Close", pygame.Rect(rect.right - 92, rect.bottom - 44, 72, 30), self._close_settings))
        return toggles, buttons

    def _draw_settings(self) -> None:
        theme = self.theme
        self._draw_backdrop()
        rect = self._settings_rect()
        self._draw_liquid_rect(rect, fill=_blend(theme.surface, theme.surface_alt, 0.24), border=theme.accent, radius=14, glow=theme.accent)
        self._text("Settings", rect.x + 20, rect.y + 16, self.title_font, theme.text)
        mouse = pygame.mouse.get_pos()
        toggles, buttons = self._settings_widgets()
        for toggle in toggles:
            toggle.draw(self.screen, theme, self.font, mouse)
        for button in buttons:
            button.draw(self.screen, theme, self.small_font, mouse)

    def _settings_click(self, pos: tuple[int, int]) -> None:
        if not self._settings_rect().collidepoint(pos):
            self._close_settings()
            return
        toggles, buttons = self._settings_widgets()
        for widget in (*toggles, *buttons):
            if widget.handle_click(pos):
                return

    # ----- add-agent dialog --------------------------------------------------
    def _open_add_agent(self) -> None:
        self.menu.open = False
        self.show_settings = False
        self.show_add_agent = True
        for field in self.add_agent_inputs.values():
            field.set("")
        self.add_agent_inputs["provider"].set("local")
        self._focus(self.add_agent_inputs["id"])

    def _close_add_agent(self) -> None:
        self.show_add_agent = False
        self._focus(self.text_input)

    def _add_agent_rect(self) -> pygame.Rect:
        rect = pygame.Rect(0, 0, min(480, self.screen_width - 80), min(340, self.screen_height - 120))
        rect.center = (self.screen_width // 2, self.screen_height // 2)
        return rect

    def _layout_add_agent(self) -> None:
        rect = self._add_agent_rect()
        y = rect.y + 56
        for key in ("id", "provider", "model", "name"):
            self.add_agent_inputs[key].rect = pygame.Rect(rect.x + 20, y, rect.width - 40, 30)
            y += 44

    def _add_agent_buttons(self) -> list[Button]:
        rect = self._add_agent_rect()
        return [
            Button("Create", pygame.Rect(rect.x + 20, rect.bottom - 44, 96, 30), self._create_agent_from_dialog),
            Button("Cancel", pygame.Rect(rect.x + 124, rect.bottom - 44, 96, 30), self._close_add_agent, style="ghost"),
        ]

    def _cycle_add_agent_focus(self) -> None:
        keys = list(self.add_agent_inputs)
        current = next((key for key in keys if self.add_agent_inputs[key].focused), None)
        nxt = keys[(keys.index(current) + 1) % len(keys)] if current else keys[0]
        self._focus(self.add_agent_inputs[nxt])

    def _draw_add_agent(self) -> None:
        theme = self.theme
        self._draw_backdrop()
        rect = self._add_agent_rect()
        self._draw_liquid_rect(rect, fill=_blend(theme.surface, theme.surface_alt, 0.24), border=theme.accent, radius=14, glow=theme.accent)
        self._text("Add agent  (Tab to move, Enter to create)", rect.x + 20, rect.y + 16, self.font, theme.text)
        self._layout_add_agent()
        blink = self._blink()
        for field in self.add_agent_inputs.values():
            field.draw(self.screen, theme, self.small_font, blink)
        mouse = pygame.mouse.get_pos()
        for button in self._add_agent_buttons():
            button.draw(self.screen, theme, self.small_font, mouse)

    def _add_agent_click(self, pos: tuple[int, int]) -> None:
        if not self._add_agent_rect().collidepoint(pos):
            self._close_add_agent()
            return
        self._layout_add_agent()
        for field in self.add_agent_inputs.values():
            if field.handle_click(pos):
                self._focus(field)
                return
        for button in self._add_agent_buttons():
            if button.handle_click(pos):
                return

    def _create_agent_from_dialog(self) -> None:
        values = {key: field.value for key, field in self.add_agent_inputs.items()}
        if self._add_agent(values["id"], values["provider"], values["model"], values["name"]):
            self._close_add_agent()

    def _add_agent(self, agent_id: str, provider: str, model: str, name: str) -> bool:
        agent_id = agent_id.strip().lower()
        provider = (provider or "local").strip().lower()
        model = model.strip()
        name = name.strip()
        if not agent_id:
            self.flash_message = "Agent needs an id"
            return False
        if provider not in {"openai", "anthropic", "gemini", "local"}:
            self.flash_message = f"Unknown provider: {provider}"
            return False
        if provider != "local" and not model:
            self.flash_message = f"{provider} agent needs a model"
            return False
        if agent_id in self.simulation.agents:
            self.flash_message = f"Agent {agent_id} exists"
            return False
        display = name or agent_id.title()
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
        return True

    def _draw_world(self) -> None:
        # Frame, grid, orbits, and title are baked into the cached background
        # (_draw_static_chrome); only the live flash message is drawn here.
        self._text(self.flash_message, WORLD_X + 20, 60, self.small_font, self.theme.muted)

    def _draw_agent(self, agent: AgentState) -> None:
        theme = self.theme
        x, y = self._agent_screen_position(agent)
        color = _hex_to_rgb(agent.profile.accent_color, theme.accent)
        bob = 0
        if agent.status is AgentStatus.WORKING:
            bob = int(round(2.5 * math.sin(self.simulation.elapsed_seconds * 6 + x)))
        cy = y + bob
        self.screen.blit(_agent_glow_surface(color), (x - 46, cy - 46))
        pygame.draw.circle(self.screen, _blend(color, theme.bg, 0.55), (x, cy), 31)
        if agent.profile.id == self.selected_agent_id:
            pygame.draw.circle(self.screen, _blend(theme.text, color, 0.18), (x, cy), 35, width=2)
        pygame.draw.circle(self.screen, color, (x, cy), 24)
        pygame.draw.circle(self.screen, _blend(theme.bg, theme.surface, 0.30), (x, cy), 17)
        self._draw_face(x, cy, agent.status, color)
        name = _render_text(self.small_font, agent.profile.display_name, theme.text)
        self.screen.blit(name, name.get_rect(center=(x, cy + 38)))
        action = _render_text(self.small_font, agent.action.value.replace("_", " "), theme.muted)
        self.screen.blit(action, action.get_rect(center=(x, cy + 54)))
        load_width = 42
        pygame.draw.rect(self.screen, _blend(theme.surface_alt, theme.bg, 0.18), (x - 21, cy - 38, load_width, 5), border_radius=3)
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
            self._draw_liquid_rect(rect, fill=_blend(theme.surface_alt, theme.bg, 0.05), border=_priority_color(task.priority, theme), radius=8, shadow=False)
            pygame.draw.rect(self.screen, theme.accent, (rect.x, rect.y, int(rect.width * task.progress), 4), border_radius=2)
            label = _render_text(self.small_font, task.id, theme.text)
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
        self._draw_liquid_rect(panel, fill=_blend(theme.surface, theme.bg, 0.06), border=theme.grid, radius=12)
        self._text("Models", PANEL_X + 18, 34, self.title_font, theme.text)
        y = 72
        for index, agent in enumerate(snapshot.agents):
            color = _hex_to_rgb(agent.profile.accent_color, theme.accent)
            row = self._agent_row_rect(index)
            if agent.profile.id == self.selected_agent_id:
                self._draw_liquid_rect(row, fill=_blend(theme.surface_alt, color, 0.08), border=color, radius=8, shadow=False)
            pygame.draw.circle(self.screen, color, (PANEL_X + 28, y + 9), 7)
            self._text(agent.profile.model_name[:20], PANEL_X + 44, y, self.font, theme.text)
            connection = "live key" if self.model_api.is_configured(agent.profile) else agent.profile.provider
            status = f"{agent.status.value} | {connection} | {agent.metrics.latency_ms:.0f} ms"
            self._text(status, PANEL_X + 44, y + 18, self.small_font, theme.muted)
            self._draw_sparkline(self.metric_history.get(agent.profile.id), pygame.Rect(PANEL_X + 196, y + 2, 46, 16), color)
            y += 48

        route = f"Route: {self._agent_label(self.selected_agent_id)}"
        self._text(route, PANEL_X + 18, y + 6, self.small_font, theme.muted)
        y += 36
        self._text("Tasks", PANEL_X + 18, y, self.title_font, theme.text)
        y += 38
        activity_y = panel.bottom - 92
        visible_tasks = max(1, min(4, (activity_y - 16 - y) // 58))
        self._draw_task_list(snapshot.tasks, y, visible_tasks)

        self._text("Activity", PANEL_X + 18, activity_y, self.title_font, theme.text)
        ey = activity_y + 30
        for event in snapshot.events[-2:][::-1]:
            message = f"{event.elapsed_seconds:05.1f}s {event.agent_id}: {event.message or event.action.value}"
            self._text(message[:38], PANEL_X + 18, ey, self.small_font, theme.muted)
            ey += 18

        auth = f"Auth: {provider_snapshot.auth_mode} | Feed: {provider_snapshot.source_label}"
        self._text(auth[:42], PANEL_X + 18, panel.bottom - 22, self.small_font, theme.muted)
        if provider_snapshot.last_error:
            self._text(provider_snapshot.last_error[:42], PANEL_X + 18, panel.bottom - 40, self.small_font, theme.danger)

    def _draw_task_list(self, tasks: list[SubmittedTask], y: int, visible_count: int = 4) -> None:
        theme = self.theme
        self._panel_task_hits = []
        max_scroll = max(0, len(tasks) - visible_count)
        self.task_scroll = min(self.task_scroll, max_scroll)
        self.task_cursor = min(self.task_cursor, max(0, len(tasks) - 1))
        window = tasks[self.task_scroll:self.task_scroll + visible_count]
        for offset, task in enumerate(window):
            absolute_index = self.task_scroll + offset
            rect = pygame.Rect(PANEL_X + 18, y, 224, 50)
            color = theme.success if task.status is TaskStatus.COMPLETE else _priority_color(task.priority, theme)
            if task.status is TaskStatus.FAILED:
                color = theme.danger
            self._draw_liquid_rect(rect, fill=_blend(theme.surface_alt, theme.bg, 0.08), border=theme.grid, radius=8, shadow=False)
            if absolute_index == self.task_cursor:
                pygame.draw.rect(self.screen, _blend(theme.text, color, 0.20), rect, width=1, border_radius=8)
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
        mouse = pygame.mouse.get_pos()
        dock = pygame.Rect(16, self.screen_height - 132, self.screen_width - 32, 104)
        self._draw_liquid_rect(dock, fill=_blend(theme.surface, theme.bg, 0.04), border=theme.grid, radius=14, glow=theme.accent)
        target = self._agent_label(self.selected_agent_id)
        self.text_input.placeholder = f"Task to {target}   @gpt ..   @all ..   !high ..   (F1 help)"
        self.text_input.draw(self.screen, theme, self.font, self._blink())
        submit = self._submit_rect()
        self._draw_liquid_rect(submit, fill=theme.accent, border=_blend(theme.accent, theme.text, 0.22), radius=10, shadow=False)
        self._text("Submit", submit.x + 26, submit.y + 17, self.font, theme.bg)
        # Toolbar (menu + quick actions) sits just above the input row.
        self.menu.draw_button(self.screen, theme, self.small_font, mouse)
        for button in self._toolbar_buttons():
            button.draw(self.screen, theme, self.small_font, mouse)

    def _draw_detail(self, task: SubmittedTask) -> None:
        theme = self.theme
        self._draw_backdrop()
        rect = self._detail_rect()
        self._draw_liquid_rect(rect, fill=_blend(theme.surface, theme.surface_alt, 0.20), border=theme.accent, radius=14, glow=theme.accent)
        pad = 18
        x = rect.x + pad
        y = rect.y + pad
        self._text(task.title[:48], x, y, self.title_font, theme.text)
        y += 32
        route = self._agent_label(task.assigned_agent_id or task.requested_agent_id)
        meta = f"{task.status.value} | {route} | {task.priority.value} | {task.model_latency_ms:.0f} ms"
        self._text(meta, x, y, self.small_font, theme.muted)
        y += 20
        self._text(self._token_cost_line(task), x, y, self.small_font, theme.muted)
        y += 24
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
            self._draw_action_button(brect, name, danger=danger)

    def _draw_help(self) -> None:
        theme = self.theme
        self._draw_backdrop()
        rect = pygame.Rect(0, 0, min(560, self.screen_width - 80), min(560, self.screen_height - 80))
        rect.center = (self.screen_width // 2, self.screen_height // 2)
        self._draw_liquid_rect(rect, fill=_blend(theme.surface, theme.surface_alt, 0.18), border=theme.grid, radius=14, glow=theme.accent)
        x, y = rect.x + 20, rect.y + 18
        self._text("Watchtower - keys & commands", x, y, self.title_font, theme.text)
        y += 38
        lines = [
            "Toolbar         Menu dropdown + Compare / Clear / Theme / Settings",
            "Settings        toggles (theme, auto-save) + Add agent dialog",
            "Enter           submit task / command (or create agent in dialog)",
            "Drag todo       drop a todo card onto an agent",
            "Click task      open its detail (prompt + response)",
            "Click agent     inspect it (metrics, task, activity)",
            "Up/Down + Enter navigate tasks and open the focused one",
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
        overlay.fill((*self.theme.overlay, 214))
        self.screen.blit(overlay, (0, 0))

    def _draw_app_background(self) -> None:
        cache_key = (self.screen_width, self.screen_height, self.theme.name)
        if self._bg_cache_key != cache_key or self._bg_surface is None:
            self._bg_surface = self._build_app_background_surface()
            self._bg_cache_key = cache_key
        self.screen.blit(self._bg_surface, (0, 0))

    def _build_app_background_surface(self) -> pygame.Surface:
        width = self.screen_width
        height = self.screen_height
        surface = pygame.Surface((width, height))
        top = _blend(self.theme.bg, self.theme.surface_alt, 0.18)
        bottom = self.theme.bg
        for y in range(height):
            color = _blend(top, bottom, y / max(1, height - 1))
            surface.fill(color, (0, y, width, 1))
        if self.theme is DARK_THEME:
            glow = pygame.Surface((width, height), pygame.SRCALPHA)
            pygame.draw.circle(glow, (*self.theme.accent, 26), (WORLD_X + WORLD_WIDTH // 2, 56), 320)
            pygame.draw.circle(glow, (*self.theme.surface_alt, 68), (width - 210, height - 120), 260)
            surface.blit(glow, (0, 0))
        self._draw_static_chrome(surface)
        return surface

    def _draw_static_chrome(self, surface: pygame.Surface) -> None:
        """Bake scene chrome that depends only on (size, theme) into the cached
        background: todo-panel frame + headings, world frame/grid/orbits/title.

        The Models panel frame and input dock stay live — they are drawn after
        agents/effects in _draw, so their frames must cover any glow overflow.
        Draw order here (todo frame, then world) matches the live order so the
        world shadow overlaps the todo panel's right edge identically.
        """
        theme = self.theme
        todo_panel = pygame.Rect(16, 16, LEFT_PANEL_WIDTH, WORLD_HEIGHT)
        self._draw_liquid_rect(todo_panel, fill=_blend(theme.surface, theme.bg, 0.06), border=theme.grid, radius=12, surface=surface)
        self._text("Todo", 34, 34, self.title_font, theme.text, surface=surface)
        self._text("Drag tasks onto agents", 34, 64, self.small_font, theme.muted, surface=surface)
        world = pygame.Rect(WORLD_X, WORLD_Y, WORLD_WIDTH, WORLD_HEIGHT)
        self._draw_liquid_rect(world, fill=_blend(theme.surface, theme.bg, 0.12), border=theme.grid, radius=12, glow=theme.accent, surface=surface)
        inner = world.inflate(-18, -18)
        pygame.draw.rect(surface, _blend(theme.bg, theme.surface_alt, 0.18), inner, border_radius=10)
        for x in range(64, WORLD_WIDTH, 64):
            pygame.draw.line(surface, _blend(theme.grid, theme.bg, 0.45), (WORLD_X + x, WORLD_Y + 10), (WORLD_X + x, WORLD_Y + WORLD_HEIGHT - 10), 1)
        for y in range(64, WORLD_HEIGHT, 64):
            pygame.draw.line(surface, _blend(theme.grid, theme.bg, 0.45), (WORLD_X + 10, WORLD_Y + y), (WORLD_X + WORLD_WIDTH - 10, WORLD_Y + y), 1)
        pygame.draw.circle(surface, _blend(theme.surface_alt, theme.accent, 0.18), (WORLD_X + WORLD_WIDTH // 2, WORLD_Y + WORLD_HEIGHT // 2), 190, width=1)
        pygame.draw.circle(surface, _blend(theme.surface_alt, theme.bg, 0.22), (WORLD_X + WORLD_WIDTH // 2, WORLD_Y + WORLD_HEIGHT // 2), 310, width=1)
        self._text("Watchtower", WORLD_X + 18, 30, self.title_font, theme.text, surface=surface)

    def _draw_liquid_rect(
        self,
        rect: pygame.Rect,
        fill: tuple[int, int, int] | None = None,
        border: tuple[int, int, int] | None = None,
        radius: int = 10,
        shadow: bool = True,
        glow: tuple[int, int, int] | None = None,
        surface: pygame.Surface | None = None,
    ) -> None:
        target = surface if surface is not None else self.screen
        fill = fill or self.theme.surface
        border = border or self.theme.grid
        if shadow:
            shadow_surface = rounded_alpha_surface((rect.width + 18, rect.height + 18), (9, 10, rect.width, rect.height), radius, (0, 0, 0, 110))
            target.blit(shadow_surface, (rect.x - 9, rect.y - 8))
        if glow:
            glow_surface = rounded_alpha_surface((rect.width + 16, rect.height + 16), (4, 4, rect.width + 8, rect.height + 8), radius + 5, (*glow, 30))
            target.blit(glow_surface, (rect.x - 8, rect.y - 8))
        pygame.draw.rect(target, fill, rect, border_radius=radius)
        sheen = _blend(fill, self.theme.text, 0.12)
        pygame.draw.line(target, sheen, (rect.x + radius, rect.y + 1), (rect.right - radius, rect.y + 1), 1)
        pygame.draw.rect(target, border, rect, width=1, border_radius=radius)

    def _draw_action_button(self, rect: pygame.Rect, label: str, danger: bool = False) -> None:
        fill = self.theme.danger if danger else self.theme.accent
        self._draw_liquid_rect(rect, fill=fill, border=_blend(fill, self.theme.text, 0.22), radius=8, shadow=False)
        self._text(label, rect.x + 10, rect.y + 7, self.small_font, self.theme.bg)

    def _draw_sparkline(self, history: deque[float] | None, rect: pygame.Rect, color: tuple[int, int, int]) -> None:
        if not history or len(history) < 2:
            return
        count = len(history)
        points = []
        for index, value in enumerate(history):
            px = rect.x + int(rect.width * index / (count - 1))
            py = rect.bottom - int(rect.height * max(0.0, min(1.0, value)))
            points.append((px, py))
        pygame.draw.lines(self.screen, color, False, points, 1)

    def _token_cost_line(self, task: SubmittedTask) -> str:
        agent = self.simulation.agents.get(task.assigned_agent_id or task.requested_agent_id or "")
        provider = agent.profile.provider if agent else "local"
        if task.actual_tokens:
            cost = task.actual_tokens / 1000 * PRICE_PER_1K.get(provider, 0.0)
            return f"{task.actual_tokens} tokens  ~${cost:.4f} est ({provider})"
        return f"~{task.estimated_tokens} tokens (estimate)"

    # ----- comparison overlay ------------------------------------------------
    def _group_tasks(self, group_id: str | None) -> list[SubmittedTask]:
        order = list(self.simulation.agents)
        tasks = [task for task in self.simulation.tasks.values() if task.group_id == group_id]

        def sort_key(task: SubmittedTask) -> int:
            agent_id = task.assigned_agent_id or task.requested_agent_id
            return order.index(agent_id) if agent_id in order else len(order)

        return sorted(tasks, key=sort_key)

    def _compare_rect(self) -> pygame.Rect:
        rect = pygame.Rect(0, 0, min(760, self.screen_width - 60), min(560, self.screen_height - 80))
        rect.center = (self.screen_width // 2, self.screen_height // 2)
        return rect

    def _overlay_close_rect(self, rect: pygame.Rect) -> pygame.Rect:
        return pygame.Rect(rect.right - 78, rect.y + 16, 60, 26)

    def _draw_compare(self) -> None:
        theme = self.theme
        self._draw_backdrop()
        rect = self._compare_rect()
        self._draw_liquid_rect(rect, fill=_blend(theme.surface, theme.surface_alt, 0.18), border=theme.accent, radius=14, glow=theme.accent)
        self._text("Comparison", rect.x + 18, rect.y + 16, self.title_font, theme.text)
        close = self._overlay_close_rect(rect)
        self._draw_action_button(close, "Close")
        tasks = self._group_tasks(self.compare_group_id)
        if not tasks:
            self._text("No tasks in this comparison", rect.x + 18, rect.y + 60, self.font, theme.muted)
            return
        # Fixed card size + paging so a large group never spills past the modal.
        cols = 2 if len(tasks) > 1 else 1
        gap = 12
        card_h = 130
        area_top = rect.y + 56
        area_bottom = rect.bottom - 28
        rows_visible = max(1, (area_bottom - area_top + gap) // (card_h + gap))
        per_page = rows_visible * cols
        total_rows = (len(tasks) + cols - 1) // cols
        max_scroll = max(0, total_rows - rows_visible)
        self.compare_scroll = min(self.compare_scroll, max_scroll)
        start = self.compare_scroll * cols
        col_w = (rect.width - 36 - (cols - 1) * gap) // cols
        for offset, task in enumerate(tasks[start:start + per_page]):
            cx = rect.x + 18 + (offset % cols) * (col_w + gap)
            cy = area_top + (offset // cols) * (card_h + gap)
            card = pygame.Rect(cx, cy, col_w, card_h)
            self._draw_liquid_rect(card, fill=_blend(theme.surface_alt, theme.bg, 0.08), border=theme.grid, radius=10, shadow=False)
            agent = self.simulation.agents.get(task.assigned_agent_id or task.requested_agent_id or "")
            color = _hex_to_rgb(agent.profile.accent_color, theme.accent) if agent else theme.accent
            self._text(self._agent_label(task.assigned_agent_id or task.requested_agent_id)[:18], cx + 10, cy + 6, self.font, color)
            self._text(f"{task.status.value} {task.progress * 100:>3.0f}%", cx + 10, cy + 26, self.small_font, theme.muted)
            body = task.model_response or task.model_partial or task.model_error or "(waiting)"
            ty = cy + 46
            max_lines = max(1, (card.bottom - ty - 6) // 16)
            for line in self._wrap(body, self.small_font, col_w - 20)[:max_lines]:
                self._text(line, cx + 10, ty, self.small_font, theme.text)
                ty += 16
        if max_scroll:
            footer = f"{len(tasks)} agents - scroll for more ({self.compare_scroll + 1}/{max_scroll + 1})"
            self._text(footer, rect.x + 18, rect.bottom - 22, self.small_font, theme.muted)

    def _handle_compare_click(self, pos: tuple[int, int]) -> None:
        rect = self._compare_rect()
        if self._overlay_close_rect(rect).collidepoint(pos) or not rect.collidepoint(pos):
            self.compare_group_id = None

    # ----- agent inspect overlay --------------------------------------------
    def _inspect_rect(self) -> pygame.Rect:
        rect = pygame.Rect(0, 0, min(520, self.screen_width - 80), min(440, self.screen_height - 120))
        rect.center = (self.screen_width // 2, self.screen_height // 2)
        return rect

    def _inspect_button_rects(self, rect: pygame.Rect) -> dict[str, pygame.Rect]:
        buttons: dict[str, pygame.Rect] = {}
        bx = rect.x + 18
        by = rect.bottom - 46
        for name in ("Route here", "Remove", "Close"):
            width = max(70, self.small_font.size(name)[0] + 20)
            buttons[name] = pygame.Rect(bx, by, width, 30)
            bx += width + 8
        return buttons

    def _draw_inspect(self) -> None:
        theme = self.theme
        agent = self.simulation.agents.get(self.inspect_agent_id or "")
        if agent is None:
            self.inspect_agent_id = None
            return
        self._draw_backdrop()
        rect = self._inspect_rect()
        self._draw_liquid_rect(rect, fill=_blend(theme.surface, theme.surface_alt, 0.18), border=theme.accent, radius=14, glow=theme.accent)
        color = _hex_to_rgb(agent.profile.accent_color, theme.accent)
        x, y = rect.x + 18, rect.y + 16
        self._text(agent.profile.display_name, x, y, self.title_font, color)
        y += 34
        metrics = agent.metrics
        current = self.simulation.tasks.get(agent.current_task_id or "")
        connection = "live key" if self.model_api.is_configured(agent.profile) else "demo"
        lines = [
            f"{agent.profile.model_name} | {agent.profile.provider} ({connection})",
            f"status {agent.status.value} | action {agent.action.value.replace('_', ' ')}",
            f"load {metrics.load * 100:.0f}% | latency {metrics.latency_ms:.0f} ms",
            f"{metrics.tokens_per_minute:.0f} tok/min | error {metrics.error_rate * 100:.1f}% | active {metrics.active_tasks}",
            f"current task: {current.title[:34] if current else 'idle'}",
        ]
        for line in lines:
            self._text(line, x, y, self.small_font, theme.text)
            y += 20
        self._draw_sparkline(self.metric_history.get(agent.profile.id), pygame.Rect(x, y + 2, rect.width - 36, 30), color)
        y += 40
        self._text("Recent activity", x, y, self.font, theme.accent)
        y += 22
        events = [event for event in self.simulation.snapshot().events if event.agent_id == agent.profile.id]
        for event in events[-4:][::-1]:
            message = f"{event.elapsed_seconds:05.1f}s {event.message or event.action.value}"
            self._text(message[:48], x, y, self.small_font, theme.muted)
            y += 18
        for name, brect in self._inspect_button_rects(rect).items():
            self._draw_action_button(brect, name, danger=name == "Remove")

    def _handle_inspect_click(self, pos: tuple[int, int]) -> None:
        rect = self._inspect_rect()
        if not rect.collidepoint(pos):
            self.inspect_agent_id = None
            return
        agent_id = self.inspect_agent_id
        for name, brect in self._inspect_button_rects(rect).items():
            if not brect.collidepoint(pos):
                continue
            if name == "Close":
                self.inspect_agent_id = None
            elif name == "Route here" and agent_id:
                self.selected_agent_id = agent_id
                self.flash_message = f"Routing to {self._agent_label(agent_id)}"
                self.inspect_agent_id = None
            elif name == "Remove" and agent_id:
                self._invalidate_dispatches(self.simulation.remove_agent(agent_id))
                self.poller.set_profiles(self.simulation.profiles)
                if self.selected_agent_id == agent_id:
                    self.selected_agent_id = None
                self.inspect_agent_id = None
                self.flash_message = f"Removed {agent_id}"
            return

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
        if task.group_id:
            names.append("Group")
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
            self._invalidate_dispatch(task.id)
            self.selected_task_id = None
        elif name == "Export":
            saved = export_task_text(f"{task.id}.md", task)
            self.flash_message = f"Exported {saved.name}"
        elif name == "Group":
            self.compare_group_id = task.group_id
            self.compare_scroll = 0
            self.selected_task_id = None

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

    def _text(
        self,
        text: str,
        x: int,
        y: int,
        font: pygame.font.Font,
        color: tuple[int, int, int],
        surface: pygame.Surface | None = None,
    ) -> None:
        target = surface if surface is not None else self.screen
        target.blit(_render_text(font, text, color), (x, y))

    def _wrap(self, text: str, font: pygame.font.Font, max_width: int) -> list[str]:
        # Memoized: overlays re-wrap the same (text, width) every frame, at one
        # font.size call per word (~500/frame for a 4000-char body). Callers
        # only slice/iterate the result — do not mutate the returned list.
        key = (font, text, max_width)
        cached = _WRAP_CACHE.get(key)
        if cached is not None:
            return cached
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
        if len(_WRAP_CACHE) >= _WRAP_CACHE_MAX:
            _WRAP_CACHE.clear()
        _WRAP_CACHE[key] = lines
        return lines

    def _agent_row_rect(self, index: int) -> pygame.Rect:
        return pygame.Rect(PANEL_X + 14, 66 + index * 48, 232, 42)

    def _draw_todo_panel(self, tasks: list[SubmittedTask]) -> None:
        # Panel frame and headings are baked into the cached background
        # (_draw_static_chrome); only task-dependent content is drawn here.
        theme = self.theme
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
        self._draw_liquid_rect(rect, fill=_blend(fill, theme.bg, 0.08), border=_priority_color(task.priority, theme), radius=10, shadow=not ghost)
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


_TEXT_CACHE: dict[tuple[pygame.font.Font, str, tuple[int, int, int]], pygame.Surface] = {}
_TEXT_CACHE_MAX = 512

_WRAP_CACHE: dict[tuple[pygame.font.Font, str, int], list[str]] = {}
_WRAP_CACHE_MAX = 128


def _render_text(font: pygame.font.Font, text: str, color: tuple[int, int, int]) -> pygame.Surface:
    """Cached font.render. Most drawn strings are unchanged frame-to-frame.

    Keyed on the font object itself (not id()) so entries keep their font alive
    and a freed font's id can never alias a new one. Churning strings (progress
    percentages, event timestamps) eventually overflow the bound; the cache is
    then cleared and rebuilt within a frame. Treat results as immutable.
    """
    key = (font, text, color)
    surface = _TEXT_CACHE.get(key)
    if surface is None:
        if len(_TEXT_CACHE) >= _TEXT_CACHE_MAX:
            _TEXT_CACHE.clear()
        surface = font.render(text, True, color)
        _TEXT_CACHE[key] = surface
    return surface


_AGENT_GLOW_CACHE: dict[tuple[int, int, int], pygame.Surface] = {}
_AGENT_GLOW_CACHE_MAX = 32


def _agent_glow_surface(color: tuple[int, int, int]) -> pygame.Surface:
    """Cached per-accent-color agent glow (two alpha circles on 92x92).

    The bitmap depends only on the resolved RGB, so each color rasterizes once
    instead of once per agent per frame. Treat the result as immutable.
    """
    surface = _AGENT_GLOW_CACHE.get(color)
    if surface is None:
        if len(_AGENT_GLOW_CACHE) >= _AGENT_GLOW_CACHE_MAX:
            _AGENT_GLOW_CACHE.clear()
        surface = pygame.Surface((92, 92), pygame.SRCALPHA)
        pygame.draw.circle(surface, (*color, 42), (46, 46), 38)
        pygame.draw.circle(surface, (*color, 18), (46, 46), 45)
        _AGENT_GLOW_CACHE[color] = surface
    return surface


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


def _estimate_tokens(prompt: str, text: str) -> int:
    return max(1, (len(prompt) + len(text)) // 4)


def _priority_color(priority: TaskPriority, theme: Theme) -> tuple[int, int, int]:
    return {
        TaskPriority.CRITICAL: theme.danger,
        TaskPriority.HIGH: theme.warning,
        TaskPriority.NORMAL: theme.accent,
        TaskPriority.LOW: theme.muted,
    }[priority]
