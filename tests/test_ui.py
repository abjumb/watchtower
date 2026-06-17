import os

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

import contextlib

import pygame

from watchtower.models import TaskPriority, TaskStatus
from watchtower.ui import DARK_THEME, LIGHT_THEME, WatchtowerApp


@contextlib.contextmanager
def make_app():
    app = WatchtowerApp()
    try:
        yield app
    finally:
        app.poller.stop()
        pygame.quit()


def _render_frame(app) -> None:
    snapshot = app.poller.latest()
    app.simulation.update(0.1, snapshot.telemetry)
    app._drain_model_results()
    app._start_ready_model_calls()
    app._sync_completion_effects()
    app._update_effects(0.1)
    app._draw(snapshot)


def test_number_key_without_modifier_is_text_input() -> None:
    app = WatchtowerApp()
    try:
        event = pygame.event.Event(pygame.KEYDOWN, key=pygame.K_1, mod=0, unicode="1")

        assert not app._is_agent_shortcut(event)
    finally:
        app.poller.stop()
        pygame.quit()


def test_number_key_with_modifier_is_agent_shortcut() -> None:
    app = WatchtowerApp()
    try:
        event = pygame.event.Event(pygame.KEYDOWN, key=pygame.K_1, mod=pygame.KMOD_CTRL, unicode="1")

        assert app._is_agent_shortcut(event)
    finally:
        app.poller.stop()
        pygame.quit()


def test_submit_without_selected_agent_adds_todo() -> None:
    app = WatchtowerApp()
    try:
        app.input_text = "Refactor the tiny panel"

        app._submit_input()
        tasks = list(app.simulation.tasks.values())

        assert len(tasks) == 1
        assert tasks[0].status.value == "todo"
    finally:
        app.poller.stop()
        pygame.quit()


def test_dragging_todo_to_agent_assigns_task() -> None:
    app = WatchtowerApp()
    try:
        task = app.simulation.create_todo_task("Ship the left panel")
        app.dragging_task_id = task.id
        agent = app.simulation.agents["gpt"]

        app._finish_drag(app._agent_screen_position(agent))
        app.simulation.update(0.1)

        assert task.assigned_agent_id == "gpt"
    finally:
        app.poller.stop()
        pygame.quit()


def test_renders_a_frame_in_both_themes_with_overlays() -> None:
    with make_app() as app:
        app.simulation.submit_task("render me", requested_agent_id="gpt")
        _render_frame(app)
        app._toggle_theme()
        assert app.theme is LIGHT_THEME
        app.show_help = True
        app.selected_task_id = next(iter(app.simulation.tasks))
        _render_frame(app)  # detail + help overlays in light theme
        app._toggle_theme()
        assert app.theme is DARK_THEME


def test_compare_input_fans_out_to_every_agent() -> None:
    with make_app() as app:
        app.input_text = "@all summarize the repo"
        app._submit_input()
        tasks = list(app.simulation.tasks.values())
        assert len(tasks) == len(app.simulation.agents)
        assert len({task.group_id for task in tasks}) == 1


def test_priority_prefix_sets_task_priority() -> None:
    with make_app() as app:
        app.input_text = "!high refactor the panel"
        app._submit_input()
        task = next(iter(app.simulation.tasks.values()))
        assert task.priority is TaskPriority.HIGH
        assert task.title.startswith("refactor")


def test_key_command_sets_runtime_local_key() -> None:
    with make_app() as app:
        app.input_text = "/key local http://localhost:11434/v1"
        app._submit_input()
        assert app.model_api.config.has_key("local")


def test_agent_add_and_remove_via_command_syncs_poller() -> None:
    with make_app() as app:
        app.input_text = "/agent add qwen local qwen2 Qwen Coder"
        app._submit_input()
        assert "qwen" in app.simulation.agents
        assert any(p.id == "qwen" for p in app.poller._profiles)

        app.input_text = "/agent remove qwen"
        app._submit_input()
        assert "qwen" not in app.simulation.agents
        assert all(p.id != "qwen" for p in app.poller._profiles)


def test_clicking_panel_task_opens_detail_then_close() -> None:
    with make_app() as app:
        app.simulation.submit_task("inspect me", requested_agent_id="gpt")
        _render_frame(app)  # populates panel task hit-rects
        assert app._panel_task_hits
        rect, task_id = app._panel_task_hits[0]
        app._handle_mouse_down(rect.center)
        assert app.selected_task_id == task_id

        _render_frame(app)
        close_rect = app._detail_button_rects(app.simulation.tasks[task_id])["Close"]
        app._handle_mouse_down(close_rect.center)
        assert app.selected_task_id is None


def test_detail_cancel_action_cancels_task() -> None:
    with make_app() as app:
        task = app.simulation.submit_task("cancel me", requested_agent_id="gpt")
        app.simulation.update(0.1)
        app.selected_task_id = task.id
        _render_frame(app)
        app._detail_action("Cancel", task)
        assert task.status is TaskStatus.CANCELLED


def test_resize_clamps_to_minimum_and_moves_submit_button() -> None:
    with make_app() as app:
        app._resize(100, 100)  # below minimums
        from watchtower.ui import MIN_HEIGHT, MIN_WIDTH

        assert app.screen_width == MIN_WIDTH
        assert app.screen_height == MIN_HEIGHT
        app._resize(1600, 900)
        assert app.screen_width == 1600
        assert app._submit_rect().x == 1600 - 120
