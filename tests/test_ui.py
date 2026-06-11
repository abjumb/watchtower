import os

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

import pygame

from watchtower.ui import WatchtowerApp


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
