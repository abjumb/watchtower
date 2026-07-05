import os

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("WATCHTOWER_NO_AUTOSAVE", "1")

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
        assert app.theme is DARK_THEME  # paper (light) is the default now
        app.show_help = True
        app.selected_task_id = next(iter(app.simulation.tasks))
        _render_frame(app)  # detail + help overlays in dark theme
        app._toggle_theme()
        assert app.theme is LIGHT_THEME


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


def test_compare_overlay_opens_from_detail_and_renders() -> None:
    with make_app() as app:
        tasks = app.simulation.submit_comparison("which is best?")
        group_id = tasks[0].group_id
        app.selected_task_id = tasks[0].id
        app._detail_action("Group", tasks[0])
        assert app.compare_group_id == group_id
        assert app.selected_task_id is None
        grouped = app._group_tasks(group_id)
        assert len(grouped) == len(app.simulation.agents)
        _render_frame(app)  # compare overlay draws without error
        # clicking outside the overlay closes it
        app._handle_mouse_down((2, 2))
        assert app.compare_group_id is None


def test_clicking_world_agent_opens_inspect_overlay() -> None:
    with make_app() as app:
        agent = app.simulation.agents["gpt"]
        app._handle_mouse_down(app._agent_screen_position(agent))
        assert app.inspect_agent_id == "gpt"
        _render_frame(app)
        rect = app._inspect_rect()
        route_rect = app._inspect_button_rects(rect)["Route here"]
        app._handle_mouse_down(route_rect.center)
        assert app.selected_agent_id == "gpt"
        assert app.inspect_agent_id is None


def test_inspect_overlay_remove_button_drops_agent() -> None:
    with make_app() as app:
        app.inspect_agent_id = "mistral"
        _render_frame(app)
        rect = app._inspect_rect()
        remove_rect = app._inspect_button_rects(rect)["Remove"]
        app._handle_mouse_down(remove_rect.center)
        assert "mistral" not in app.simulation.agents
        assert all(p.id != "mistral" for p in app.poller._profiles)


def test_keyboard_navigation_opens_focused_task() -> None:
    with make_app() as app:
        app.simulation.create_todo_task("first")
        app.simulation.create_todo_task("second")
        app._move_task_cursor(1)
        assert app.task_cursor == 1
        app.input_text = ""
        app._handle_return()  # empty input -> open focused task
        tasks = app.simulation.snapshot().tasks
        assert app.selected_task_id == tasks[1].id


def test_metric_history_samples_over_time() -> None:
    with make_app() as app:
        app._sample_metrics(1.0)  # well past SPARK_INTERVAL
        app._sample_metrics(1.0)
        assert all(len(hist) >= 1 for hist in app.metric_history.values())
        assert set(app.metric_history) == set(app.simulation.agents)


def test_removing_agent_requeues_inflight_task_and_ignores_stale_result() -> None:
    from watchtower.model_api import ModelCallResult

    with make_app() as app:
        task = app.simulation.submit_task("work", requested_agent_id="gpt")
        app.simulation.update(0.1)  # assign to gpt
        # mimic a dispatched model call
        task.api_started = True
        app.running_model_tasks.add(task.id)
        app._dispatch_seq += 1
        old_token = app._dispatch_seq
        app._dispatch_token[task.id] = old_token

        requeued = app.simulation.remove_agent("gpt")
        app._invalidate_dispatches(requeued)

        assert task.id in requeued
        assert task.status is TaskStatus.SUBMITTED
        assert task.api_started is False
        assert task.id not in app.running_model_tasks
        assert task.id not in app._dispatch_token

        # the requeued task gets a new home instead of being skipped forever
        app.simulation.update(0.1)
        assert task.assigned_agent_id in app.simulation.agents

        # a late result from the removed agent's dispatch must be ignored
        app.model_results.put(("done", task.id, old_token, ModelCallResult("gpt", "stale", 1.0)))
        app._drain_model_results()
        assert task.model_response == ""
        assert task.status is not TaskStatus.COMPLETE


def test_matching_dispatch_result_completes_task() -> None:
    from watchtower.model_api import ModelCallResult

    with make_app() as app:
        task = app.simulation.submit_task("work", requested_agent_id="gpt")
        app.simulation.update(0.1)
        app._dispatch_seq += 1
        token = app._dispatch_seq
        app._dispatch_token[task.id] = token
        app.running_model_tasks.add(task.id)
        app.model_results.put(("done", task.id, token, ModelCallResult("gpt", "real answer", 5.0, total_tokens=12)))
        app._drain_model_results()
        assert task.status is TaskStatus.COMPLETE
        assert task.model_response == "real answer"
        assert task.actual_tokens == 12
        assert task.id not in app._dispatch_token


def test_arrow_keys_do_not_move_cursor_while_typing() -> None:
    with make_app() as app:
        app.simulation.create_todo_task("one")
        app.simulation.create_todo_task("two")
        app.input_text = "drafting a prompt"
        before = app.task_cursor
        app._handle_keydown(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_DOWN, mod=0, unicode=""))
        assert app.task_cursor == before  # gated while input has text
        app.input_text = ""
        app._handle_keydown(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_DOWN, mod=0, unicode=""))
        assert app.task_cursor == before + 1


def test_comparison_overlay_paginates_large_group() -> None:
    with make_app() as app:
        for i in range(8):  # grow the roster well past one screen of cards
            from watchtower.models import AgentProfile

            app.simulation.add_agent(AgentProfile(f"x{i}", f"X{i}", f"X{i} model", provider="local"))
        tasks = app.simulation.submit_comparison("compare everyone")
        app.compare_group_id = tasks[0].group_id
        _render_frame(app)  # must not raise even with many cards
        assert app.compare_scroll == 0
        app.compare_scroll = 999
        _render_frame(app)  # draw clamps an out-of-range scroll
        assert app.compare_scroll < len(app.simulation.agents)


def test_typing_keydown_updates_input_text() -> None:
    with make_app() as app:
        for char in "hi there":
            app._handle_keydown(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_a, unicode=char, mod=0))
        assert app.input_text == "hi there"


def test_menu_dropdown_opens_and_selects_settings() -> None:
    with make_app() as app:
        _render_frame(app)
        app._handle_mouse_down(app.menu.rect.center)
        assert app.menu.open
        labels = [label for label, _ in app.menu.items]
        idx = labels.index("Settings...")
        app._handle_mouse_down(app.menu.item_rects()[idx].center)
        assert app.show_settings


def test_toolbar_settings_button_opens_dialog() -> None:
    with make_app() as app:
        _render_frame(app)
        button = next(b for b in app._toolbar_buttons() if b.label == "Settings")
        app._handle_mouse_down(button.rect.center)
        assert app.show_settings


def test_settings_toggle_switches_theme() -> None:
    with make_app() as app:
        app._open_settings()
        _render_frame(app)
        toggles, _ = app._settings_widgets()
        app._settings_click(toggles[0].rect.center)  # "Light theme" off -> dark
        assert app.theme is DARK_THEME


def test_clicking_outside_settings_closes_it() -> None:
    with make_app() as app:
        app._open_settings()
        app._handle_mouse_down((2, 2))
        assert not app.show_settings


def test_add_agent_dialog_creates_agent() -> None:
    with make_app() as app:
        app._open_add_agent()
        app.add_agent_inputs["id"].set("qwen")
        app.add_agent_inputs["provider"].set("local")
        app.add_agent_inputs["model"].set("qwen2")
        app.add_agent_inputs["name"].set("Qwen Coder")
        app._create_agent_from_dialog()
        assert "qwen" in app.simulation.agents
        assert not app.show_add_agent
        assert any(p.id == "qwen" for p in app.poller._profiles)


def test_toolbar_compare_honours_priority_prefix() -> None:
    from watchtower.models import TaskPriority

    with make_app() as app:
        app.input_text = "!high brainstorm names"
        app._toolbar_compare()
        tasks = list(app.simulation.tasks.values())
        assert tasks, "compare should have created tasks"
        assert all(t.priority is TaskPriority.HIGH for t in tasks)
        assert all(t.prompt == "brainstorm names" for t in tasks)
        assert app.input_text == ""


def test_toolbar_compare_strips_target_prefix() -> None:
    with make_app() as app:
        app.input_text = "@gpt brainstorm names"
        app._toolbar_compare()
        tasks = list(app.simulation.tasks.values())
        assert len(tasks) == len(app.simulation.agents)
        # The @gpt target is stripped, not sent as literal prompt text.
        assert all(t.prompt == "brainstorm names" for t in tasks)


def test_add_agent_rejects_blank_model_for_real_provider() -> None:
    with make_app() as app:
        assert app._add_agent("ghost", "openai", "", "Ghost") is False
        assert "ghost" not in app.simulation.agents
        # local agents may omit a model (they fall back to a stub).
        assert app._add_agent("edge", "local", "", "Edge") is True
        assert "edge" in app.simulation.agents


def test_open_menu_click_through_acts_on_target() -> None:
    with make_app() as app:
        _render_frame(app)
        app.menu.open = True
        # A single click on a toolbar button should close the menu AND act.
        button = next(b for b in app._toolbar_buttons() if b.label == "Settings")
        app._handle_mouse_down(button.rect.center)
        assert not app.menu.open
        assert app.show_settings


def test_add_agent_dialog_tab_cycles_focus() -> None:
    with make_app() as app:
        app._open_add_agent()
        assert app.add_agent_inputs["id"].focused
        app._handle_keydown(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_TAB, unicode="\t", mod=0))
        assert app.add_agent_inputs["provider"].focused
        assert not app.add_agent_inputs["id"].focused


def test_autosave_round_trips_via_patched_path(tmp_path, monkeypatch) -> None:
    import watchtower.ui as ui_module

    monkeypatch.setattr(ui_module, "AUTOSAVE_PATH", tmp_path / "autosave.json")
    monkeypatch.delenv("WATCHTOWER_NO_AUTOSAVE", raising=False)
    with make_app() as app:
        app.simulation.submit_task("persist me", requested_agent_id="gpt")
        app._autosave()
        assert (tmp_path / "autosave.json").exists()

    # a fresh app should restore the autosaved task
    with make_app() as app2:
        assert any(task.prompt == "persist me" for task in app2.simulation.tasks.values())
