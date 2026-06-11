from __future__ import annotations

import asyncio
import queue
import threading

import pygame

from watchtower.auth import AuthConfig
from watchtower.data_provider import AgentDataProvider, TelemetryPoller
from watchtower.model_api import ModelApiClient, ModelCallResult
from watchtower.models import AgentState, SubmittedTask, TaskStatus
from watchtower.simulation import WORLD_HEIGHT, WORLD_WIDTH, SimulationState


LEFT_PANEL_WIDTH = 236
WORLD_X = LEFT_PANEL_WIDTH + 24
WORLD_Y = 16
PANEL_X = WORLD_X + WORLD_WIDTH + 16
SCREEN_WIDTH = PANEL_X + 260
SCREEN_HEIGHT = 760
BACKGROUND = (13, 17, 23)
SURFACE = (24, 31, 42)
SURFACE_2 = (33, 42, 55)
TEXT = (227, 232, 239)
MUTED = (146, 157, 171)
GRID = (38, 48, 63)
ACCENT = (95, 168, 255)


class WatchtowerApp:
    def __init__(self) -> None:
        pygame.init()
        pygame.display.set_caption("Watchtower")
        self.screen = pygame.display.set_mode((SCREEN_WIDTH, SCREEN_HEIGHT))
        self.clock = pygame.time.Clock()
        self.font = pygame.font.SysFont("arial", 16)
        self.small_font = pygame.font.SysFont("arial", 13)
        self.title_font = pygame.font.SysFont("arial", 24, bold=True)
        self.badge_font = pygame.font.SysFont("arial", 14, bold=True)
        self.simulation = SimulationState()
        self.auth_config = AuthConfig.from_env()
        self.provider = AgentDataProvider(self.auth_config)
        self.poller = TelemetryPoller(self.provider, self.simulation.profiles)
        self.model_api = ModelApiClient()
        self.model_results: queue.Queue[tuple[str, ModelCallResult | None, str]] = queue.Queue()
        self.running_model_tasks: set[str] = set()
        self.input_text = ""
        self.flash_message = "Ready"
        self.selected_agent_id: str | None = None
        self.dragging_task_id: str | None = None
        self.running = True

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
                self._draw(provider_snapshot)
                pygame.display.flip()
        finally:
            self.poller.stop()
            pygame.quit()

    def _handle_events(self) -> None:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                self.running = False
            elif event.type == pygame.KEYDOWN:
                if event.key == pygame.K_ESCAPE:
                    self.running = False
                elif event.key == pygame.K_RETURN:
                    self._submit_input()
                elif event.key == pygame.K_BACKSPACE:
                    self.input_text = self.input_text[:-1]
                elif self._is_agent_shortcut(event):
                    self._select_agent_by_index(event.key - pygame.K_1)
                elif event.unicode and len(self.input_text) < 180:
                    self.input_text += event.unicode
            elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                todo_task_id = self._todo_task_at(event.pos)
                if todo_task_id:
                    self.dragging_task_id = todo_task_id
                elif self._submit_rect().collidepoint(event.pos):
                    self._submit_input()
                else:
                    self._handle_selection_click(event.pos)
            elif event.type == pygame.MOUSEBUTTONUP and event.button == 1:
                self._finish_drag(event.pos)

    def _submit_input(self) -> None:
        text = self.input_text.strip()
        if not text:
            return
        self.input_text = ""
        if text.startswith("/auth token "):
            token = text.removeprefix("/auth token ").strip()
            self.auth_config = self.auth_config.with_token(token)
            self.poller.configure(self.auth_config)
            self.flash_message = "OAuth token loaded"
            return
        if text.startswith("/auth login "):
            parts = text.split(maxsplit=3)
            if len(parts) == 4:
                self.auth_config = self.auth_config.with_login(parts[2], parts[3])
                self.poller.configure(self.auth_config)
                self.flash_message = "Login credentials loaded"
            else:
                self.flash_message = "Use /auth login USER PASS"
            return
        if text.startswith("/endpoint "):
            endpoint = text.removeprefix("/endpoint ").strip()
            self.auth_config = self.auth_config.with_endpoint(endpoint)
            self.poller.configure(self.auth_config)
            self.flash_message = "Telemetry endpoint set"
            return
        if text == "/auto":
            self.selected_agent_id = None
            self.flash_message = "Task routing set to auto"
            return
        requested_agent_id, prompt = self._parse_targeted_prompt(text)
        target_agent_id = requested_agent_id or self.selected_agent_id
        if target_agent_id:
            task = self.simulation.submit_task(prompt, requested_agent_id=target_agent_id)
            self.flash_message = f"Task {task.id} submitted to {self._agent_label(target_agent_id)}"
        else:
            task = self.simulation.create_todo_task(prompt)
            self.flash_message = f"Todo {task.id} added"

    def _parse_targeted_prompt(self, text: str) -> tuple[str | None, str]:
        if not text.startswith("@"):
            return None, text
        target, _, prompt = text.partition(" ")
        agent_id = target[1:].strip().lower()
        if agent_id in self.simulation.agents and prompt.strip():
            return agent_id, prompt.strip()
        return None, text

    def _draw(self, provider_snapshot) -> None:
        self.screen.fill(BACKGROUND)
        self._draw_todo_panel()
        self._draw_world()
        snapshot = self.simulation.snapshot()
        for agent in snapshot.agents:
            self._draw_agent(agent)
        self._draw_task_stations(snapshot.tasks)
        self._draw_panel(snapshot.tasks, provider_snapshot)
        self._draw_input()

    def _draw_world(self) -> None:
        world = pygame.Rect(WORLD_X, WORLD_Y, WORLD_WIDTH, WORLD_HEIGHT)
        pygame.draw.rect(self.screen, (16, 22, 30), world, border_radius=8)
        pygame.draw.rect(self.screen, GRID, world, width=1, border_radius=8)
        for x in range(64, WORLD_WIDTH, 64):
            pygame.draw.line(self.screen, GRID, (WORLD_X + x, WORLD_Y), (WORLD_X + x, WORLD_Y + WORLD_HEIGHT), 1)
        for y in range(64, WORLD_HEIGHT, 64):
            pygame.draw.line(self.screen, GRID, (WORLD_X, WORLD_Y + y), (WORLD_X + WORLD_WIDTH, WORLD_Y + y), 1)
        title = self.title_font.render("Watchtower", True, TEXT)
        self.screen.blit(title, (WORLD_X + 18, 30))
        subtitle = self.small_font.render(self.flash_message, True, MUTED)
        self.screen.blit(subtitle, (WORLD_X + 20, 60))

    def _draw_agent(self, agent: AgentState) -> None:
        x, y = self._agent_screen_position(agent)
        color = _hex_to_rgb(agent.profile.accent_color)
        pygame.draw.circle(self.screen, _dim(color, 0.22), (x, y), 30)
        if agent.profile.id == self.selected_agent_id:
            pygame.draw.circle(self.screen, TEXT, (x, y), 34, width=2)
        pygame.draw.circle(self.screen, color, (x, y), 23)
        pygame.draw.circle(self.screen, BACKGROUND, (x, y), 17)
        label = self.badge_font.render(agent.profile.glyph, True, color)
        self.screen.blit(label, label.get_rect(center=(x, y)))
        name = self.small_font.render(agent.profile.display_name, True, TEXT)
        self.screen.blit(name, name.get_rect(center=(x, y + 38)))
        action = self.small_font.render(agent.action.value.replace("_", " "), True, MUTED)
        self.screen.blit(action, action.get_rect(center=(x, y + 54)))
        load_width = 42
        pygame.draw.rect(self.screen, SURFACE_2, (x - 21, y - 38, load_width, 5), border_radius=3)
        pygame.draw.rect(self.screen, color, (x - 21, y - 38, int(load_width * agent.metrics.load), 5), border_radius=3)

    def _draw_task_stations(self, tasks: list[SubmittedTask]) -> None:
        active = [task for task in tasks if task.status in {TaskStatus.ASSIGNED, TaskStatus.IN_PROGRESS}]
        for index, task in enumerate(active[:5]):
            rect = pygame.Rect(WORLD_X + 84 + index * 145, WORLD_Y + WORLD_HEIGHT - 40, 106, 30)
            pygame.draw.rect(self.screen, SURFACE_2, rect, border_radius=6)
            pygame.draw.rect(self.screen, ACCENT, rect, width=1, border_radius=6)
            pygame.draw.rect(self.screen, ACCENT, (rect.x, rect.y, int(rect.width * task.progress), 4), border_radius=2)
            label = self.small_font.render(task.id, True, TEXT)
            self.screen.blit(label, label.get_rect(center=rect.center))

    def _draw_panel(self, tasks: list[SubmittedTask], provider_snapshot) -> None:
        panel = pygame.Rect(PANEL_X, 16, SCREEN_WIDTH - PANEL_X - 16, WORLD_HEIGHT)
        pygame.draw.rect(self.screen, SURFACE, panel, border_radius=8)
        self._text("Models", PANEL_X + 18, 34, self.title_font, TEXT)
        y = 72
        snapshot = self.simulation.snapshot()
        for index, agent in enumerate(snapshot.agents):
            color = _hex_to_rgb(agent.profile.accent_color)
            row = self._agent_row_rect(index)
            if agent.profile.id == self.selected_agent_id:
                pygame.draw.rect(self.screen, SURFACE_2, row, border_radius=6)
                pygame.draw.rect(self.screen, color, row, width=1, border_radius=6)
            pygame.draw.circle(self.screen, color, (PANEL_X + 28, y + 9), 7)
            self._text(agent.profile.model_name, PANEL_X + 44, y, self.font, TEXT)
            connection = "live key" if self.model_api.is_configured(agent.profile) else agent.profile.provider
            status = f"{agent.status.value} | {connection} | {agent.metrics.latency_ms:.0f} ms"
            self._text(status, PANEL_X + 44, y + 18, self.small_font, MUTED)
            y += 48

        route = f"Route: {self._agent_label(self.selected_agent_id)}"
        self._text(route, PANEL_X + 18, y + 6, self.small_font, MUTED)
        y += 36
        self._text("Tasks", PANEL_X + 18, y, self.title_font, TEXT)
        y += 38
        for task in tasks[:3]:
            color = ACCENT if task.status is not TaskStatus.COMPLETE else (77, 201, 129)
            pygame.draw.rect(self.screen, SURFACE_2, (PANEL_X + 18, y, 224, 50), border_radius=6)
            pygame.draw.rect(self.screen, color, (PANEL_X + 18, y, 4, 50), border_radius=2)
            self._text(task.title, PANEL_X + 30, y + 7, self.small_font, TEXT)
            route_label = self._agent_label(task.assigned_agent_id or task.requested_agent_id)
            meta = f"{task.status.value} {task.progress * 100:>3.0f}% | {route_label}"
            if task.model_error:
                meta = f"api error | {route_label}"
            elif task.model_response:
                meta = f"done via api | {route_label}"
            self._text(meta, PANEL_X + 30, y + 27, self.small_font, MUTED)
            y += 58

        self._text("Activity", PANEL_X + 18, y + 4, self.title_font, TEXT)
        y += 38
        for event in snapshot.events[-4:][::-1]:
            message = f"{event.elapsed_seconds:05.1f}s {event.agent_id}: {event.message or event.action.value}"
            self._text(message[:38], PANEL_X + 18, y, self.small_font, MUTED)
            y += 18

        auth = f"Auth: {provider_snapshot.auth_mode} | Feed: {provider_snapshot.source_label}"
        self._text(auth[:42], PANEL_X + 18, WORLD_HEIGHT + 2, self.small_font, MUTED)
        if provider_snapshot.last_error:
            self._text(provider_snapshot.last_error[:42], PANEL_X + 18, WORLD_HEIGHT + 20, self.small_font, (236, 122, 122))

    def _draw_input(self) -> None:
        rect = pygame.Rect(16, SCREEN_HEIGHT - 86, SCREEN_WIDTH - 150, 52)
        pygame.draw.rect(self.screen, SURFACE, rect, border_radius=8)
        pygame.draw.rect(self.screen, ACCENT, rect, width=1, border_radius=8)
        target = self._agent_label(self.selected_agent_id)
        text = self.input_text or f"Submit task to {target}, @gpt prompt, /auto, /endpoint URL, /auth token TOKEN"
        color = TEXT if self.input_text else MUTED
        self._text(text[-120:], rect.x + 16, rect.y + 17, self.font, color)
        submit = self._submit_rect()
        pygame.draw.rect(self.screen, ACCENT, submit, border_radius=8)
        self._text("Submit", submit.x + 26, submit.y + 17, self.font, BACKGROUND)

    def _submit_rect(self) -> pygame.Rect:
        return pygame.Rect(SCREEN_WIDTH - 120, SCREEN_HEIGHT - 86, 104, 52)

    def _text(self, text: str, x: int, y: int, font: pygame.font.Font, color: tuple[int, int, int]) -> None:
        self.screen.blit(font.render(text, True, color), (x, y))

    def _agent_row_rect(self, index: int) -> pygame.Rect:
        return pygame.Rect(PANEL_X + 14, 66 + index * 48, 232, 42)

    def _draw_todo_panel(self) -> None:
        panel = pygame.Rect(16, 16, LEFT_PANEL_WIDTH, WORLD_HEIGHT)
        pygame.draw.rect(self.screen, SURFACE, panel, border_radius=8)
        self._text("Todo", 34, 34, self.title_font, TEXT)
        self._text("Drag tasks onto agents", 34, 64, self.small_font, MUTED)
        todo_tasks = [task for task in self.simulation.snapshot().tasks if task.status is TaskStatus.TODO]
        if not todo_tasks:
            self._text("Type a task below", 34, 104, self.font, MUTED)
            self._text("then drag it into play", 34, 126, self.font, MUTED)
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
        fill = SURFACE_2 if not ghost else (45, 58, 74)
        pygame.draw.rect(self.screen, fill, rect, border_radius=8)
        pygame.draw.rect(self.screen, ACCENT, rect, width=1, border_radius=8)
        self._text(task.title[:24], rect.x + 10, rect.y + 8, self.small_font, TEXT)
        self._text("grab and drop", rect.x + 10, rect.y + 27, self.small_font, MUTED)

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

    def _handle_selection_click(self, position: tuple[int, int]) -> None:
        for index, agent in enumerate(self.simulation.snapshot().agents):
            if self._agent_row_rect(index).collidepoint(position):
                self.selected_agent_id = agent.profile.id
                self.flash_message = f"Selected {agent.profile.display_name}"
                return

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

    def _start_ready_model_calls(self) -> None:
        for task in self.simulation.tasks.values():
            if task.status not in {TaskStatus.ASSIGNED, TaskStatus.IN_PROGRESS}:
                continue
            if task.api_started or task.id in self.running_model_tasks or not task.assigned_agent_id:
                continue
            agent = self.simulation.agents[task.assigned_agent_id]
            if not self.model_api.is_configured(agent.profile):
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

    def _run_model_task(self, task_id: str, profile, prompt: str) -> None:
        try:
            result = asyncio.run(self.model_api.run_task(profile, prompt))
            self.model_results.put((task_id, result, ""))
        except Exception as exc:
            self.model_results.put((task_id, None, f"{type(exc).__name__}: {exc}"))

    def _drain_model_results(self) -> None:
        while True:
            try:
                task_id, result, error = self.model_results.get_nowait()
            except queue.Empty:
                return
            self.running_model_tasks.discard(task_id)
            task = self.simulation.tasks.get(task_id)
            if not task:
                continue
            agent_id = task.assigned_agent_id
            if error:
                task.mark_model_error(error[:180])
                if agent_id:
                    self.simulation.agents[agent_id].current_task_id = None
                self.flash_message = f"{task.id} API error"
                continue
            if result:
                task.mark_model_result(result.text[:1000])
                if agent_id:
                    self.simulation.agents[agent_id].current_task_id = None
                self.flash_message = f"{task.id} completed by {self._agent_label(agent_id)}"


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    clean = value.strip().lstrip("#")
    if len(clean) != 6:
        return ACCENT
    return tuple(int(clean[index : index + 2], 16) for index in (0, 2, 4))


def _dim(color: tuple[int, int, int], factor: float) -> tuple[int, int, int]:
    return tuple(max(0, min(255, int(channel * factor))) for channel in color)
