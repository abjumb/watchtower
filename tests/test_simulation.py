from watchtower.auth import AuthConfig
from watchtower.models import AgentMetrics, AgentTelemetry, TaskStatus
from watchtower.simulation import SimulationState


def test_submitted_task_is_assigned_and_completed() -> None:
    simulation = SimulationState()
    task = simulation.submit_task("Summarize the latest benchmark run")

    for _ in range(90):
        simulation.update(0.2)

    assert task.assigned_agent_id is not None
    assert task.status is TaskStatus.COMPLETE
    assert task.progress == 1.0


def test_task_can_target_specific_agent() -> None:
    simulation = SimulationState()
    task = simulation.submit_task("Write code", requested_agent_id="mistral")

    simulation.update(0.1)

    assert task.assigned_agent_id == "mistral"
    assert simulation.agents["mistral"].current_task_id == task.id


def test_todo_task_waits_until_dropped_on_agent() -> None:
    simulation = SimulationState()
    task = simulation.create_todo_task("Review the vibe")

    simulation.update(0.1)

    assert task.status is TaskStatus.TODO
    assert task.assigned_agent_id is None


def test_todo_task_can_be_dropped_on_agent() -> None:
    simulation = SimulationState()
    task = simulation.create_todo_task("Review the vibe")

    simulation.assign_todo_task(task.id, "claude")
    simulation.update(0.1)

    assert task.assigned_agent_id == "claude"
    assert task.status is TaskStatus.IN_PROGRESS


def test_targeted_task_waits_for_busy_agent() -> None:
    simulation = SimulationState()
    first = simulation.submit_task("First", requested_agent_id="gpt")
    second = simulation.submit_task("Second", requested_agent_id="gpt")

    simulation.update(0.1)

    assert first.assigned_agent_id == "gpt"
    assert second.status is TaskStatus.SUBMITTED


def test_auto_routing_waits_when_all_agents_are_busy() -> None:
    simulation = SimulationState()
    tasks = [simulation.submit_task(f"Task {index}") for index in range(len(simulation.agents) + 1)]

    simulation.update(0.1)

    assigned_tasks = [task for task in tasks if task.assigned_agent_id is not None]
    waiting_tasks = [task for task in tasks if task.status is TaskStatus.SUBMITTED]
    current_task_ids = {agent.current_task_id for agent in simulation.agents.values()}

    assert len(assigned_tasks) == len(simulation.agents)
    assert len(waiting_tasks) == 1
    assert {task.id for task in assigned_tasks} == current_task_ids


def test_remote_telemetry_updates_agent_metrics() -> None:
    simulation = SimulationState()
    profile = simulation.profiles[0]

    simulation.update(
        0.1,
        {
            profile.id: AgentTelemetry(
                agent_id=profile.id,
                metrics=AgentMetrics(load=0.9, latency_ms=123, tokens_per_minute=456),
            )
        },
    )

    assert simulation.agents[profile.id].metrics.load == 0.9
    assert simulation.agents[profile.id].metrics.latency_ms == 123


def test_auth_headers_support_oauth_and_login() -> None:
    assert AuthConfig(oauth_token="secret").headers()["Authorization"] == "Bearer secret"
    assert AuthConfig(username="u", password="p").headers()["Authorization"].startswith("Basic ")


def test_scheduler_skips_sorting_when_no_agent_is_free(monkeypatch) -> None:
    import watchtower.simulation as simulation_module

    simulation = SimulationState()
    for agent_id in list(simulation.agents):
        simulation.submit_task("busy work", requested_agent_id=agent_id)
    simulation.update(0.1)
    assert all(agent.current_task_id is not None for agent in simulation.agents.values())
    queued = [simulation.submit_task(f"queued {index}") for index in range(50)]

    calls = {"count": 0}
    real_sorted = sorted

    def counting_sorted(*args, **kwargs):
        calls["count"] += 1
        return real_sorted(*args, **kwargs)

    # Shadow the builtin inside the module so only simulation.py's sorts count,
    # and exercise the scheduler directly (update() also sorts in
    # _prune_finished, which is out of scope for this guard).
    monkeypatch.setattr(simulation_module, "sorted", counting_sorted, raising=False)

    simulation._assign_waiting_tasks()
    assert calls["count"] == 0  # all agents busy: no filter/sort work at all
    assert all(task.status is TaskStatus.SUBMITTED for task in queued)

    # Freeing an agent resumes normal routing on the next tick.
    freed = next(iter(simulation.agents.values()))
    simulation.tasks[freed.current_task_id].mark_progress(1.0)
    freed.current_task_id = None
    simulation.update(0.1)
    assert calls["count"] > 0
    assert freed.current_task_id is not None
    assert simulation.tasks[freed.current_task_id] in queued
