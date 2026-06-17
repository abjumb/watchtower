import asyncio

import httpx

from watchtower import model_api
from watchtower.model_api import (
    ModelApiClient,
    ModelApiConfig,
    _anthropic_delta,
    _gemini_delta,
    _openai_chat_delta,
    _openai_responses_delta,
    _total_tokens,
)
from watchtower.models import AgentProfile, TaskPriority, TaskStatus
from watchtower.persistence import load_session, save_session
from watchtower.simulation import _API_PROGRESS_CEILING, SimulationState


def test_higher_priority_task_is_scheduled_first_under_contention() -> None:
    simulation = SimulationState()
    lows = [simulation.submit_task(f"low {i}", priority=TaskPriority.LOW) for i in range(5)]
    critical = simulation.submit_task("urgent", priority=TaskPriority.CRITICAL)

    simulation.update(0.1)

    assert critical.assigned_agent_id is not None
    waiting = [task for task in lows if task.status is TaskStatus.SUBMITTED]
    assert len(waiting) == 1


def test_cancel_task_frees_agent_and_marks_cancelled() -> None:
    simulation = SimulationState()
    task = simulation.submit_task("do it", requested_agent_id="gpt")
    simulation.update(0.1)
    assert simulation.agents["gpt"].current_task_id == task.id

    simulation.cancel_task(task.id)

    assert task.status is TaskStatus.CANCELLED
    assert simulation.agents["gpt"].current_task_id is None


def test_clear_finished_removes_terminal_tasks_only() -> None:
    simulation = SimulationState()
    done = simulation.submit_task("done")
    done.mark_model_result("ok")
    active = simulation.submit_task("busy", requested_agent_id="claude")
    simulation.update(0.1)

    removed = simulation.clear_finished()

    assert removed == 1
    assert done.id not in simulation.tasks
    assert active.id in simulation.tasks


def test_bounded_history_caps_finished_tasks() -> None:
    simulation = SimulationState()
    simulation.max_finished_tasks = 3
    for i in range(10):
        task = simulation.submit_task(f"t{i}")
        task.mark_model_result("x")
    simulation.update(0.1)

    finished = [task for task in simulation.tasks.values() if task.is_finished]
    assert len(finished) == 3


def test_compare_fans_prompt_to_every_agent() -> None:
    simulation = SimulationState()
    tasks = simulation.submit_comparison("compare this")

    assert len(tasks) == len(simulation.agents)
    assert len({task.group_id for task in tasks}) == 1
    assert {task.requested_agent_id for task in tasks} == set(simulation.agents)


def test_api_task_progress_holds_until_real_result() -> None:
    simulation = SimulationState()
    task = simulation.submit_task("call api", requested_agent_id="gpt")
    simulation.update(0.1)
    task.api_started = True

    for _ in range(200):
        simulation.update(0.2)

    assert task.status is not TaskStatus.COMPLETE
    assert task.progress <= _API_PROGRESS_CEILING + 1e-6

    task.mark_model_result("the answer", latency_ms=42)
    assert task.status is TaskStatus.COMPLETE
    assert task.progress == 1.0


def test_local_agent_without_api_still_completes_synthetically() -> None:
    simulation = SimulationState()
    task = simulation.submit_task("local work", requested_agent_id="llama")
    for _ in range(90):
        simulation.update(0.2)

    assert task.status is TaskStatus.COMPLETE


def test_add_and_remove_agent_updates_roster() -> None:
    simulation = SimulationState()
    before = len(simulation.agents)
    profile = AgentProfile("qwen", "Qwen", "Qwen Local", provider="local")

    simulation.add_agent(profile)
    assert len(simulation.agents) == before + 1
    assert "qwen" in simulation.agents

    task = simulation.submit_task("x", requested_agent_id="qwen")
    simulation.update(0.1)
    assert task.assigned_agent_id == "qwen"

    simulation.remove_agent("qwen")
    assert "qwen" not in simulation.agents
    assert task.status is TaskStatus.SUBMITTED
    assert task.assigned_agent_id is None


def test_retry_requeues_failed_task() -> None:
    simulation = SimulationState()
    task = simulation.submit_task("flaky", requested_agent_id="gpt")
    simulation.update(0.1)
    task.mark_model_error("boom")
    simulation._free_agent_for(task.id)

    simulation.retry_task(task.id)

    assert task.status is TaskStatus.SUBMITTED
    assert task.model_error == ""
    assert task.progress == 0.0


def test_session_round_trips_through_disk(tmp_path) -> None:
    simulation = SimulationState()
    done = simulation.submit_task("remembered", requested_agent_id="claude")
    done.mark_model_result("kept text", latency_ms=120)
    path = tmp_path / "session.json"

    save_session(path, simulation.profiles, list(simulation.tasks.values()))
    profiles, tasks = load_session(path)

    assert {p.id for p in profiles} == set(simulation.agents)
    restored = next(task for task in tasks if task.id == done.id)
    assert restored.model_response == "kept text"
    assert restored.status is TaskStatus.COMPLETE
    assert restored.model_latency_ms == 120


def test_session_round_trips_token_usage(tmp_path) -> None:
    simulation = SimulationState()
    task = simulation.submit_task("count tokens", requested_agent_id="gpt")
    task.mark_model_result("answer", latency_ms=10, tokens=321)
    path = tmp_path / "s.json"

    save_session(path, simulation.profiles, list(simulation.tasks.values()))
    _, tasks = load_session(path)

    assert next(t for t in tasks if t.id == task.id).actual_tokens == 321


def test_total_tokens_handles_provider_usage_shapes() -> None:
    assert _total_tokens({"usage": {"total_tokens": 42}}) == 42
    assert _total_tokens({"usage": {"input_tokens": 10, "output_tokens": 5}}) == 15
    assert _total_tokens({"usage": {"prompt_tokens": 7, "completion_tokens": 3}}) == 10
    assert _total_tokens({"usageMetadata": {"totalTokenCount": 99}}) == 99
    assert _total_tokens({"nothing": True}) == 0


def test_total_tokens_tolerates_malformed_usage() -> None:
    # null / non-numeric usage values must not raise (would turn a good response into an error)
    assert _total_tokens({"usage": {"total_tokens": None}}) == 0
    assert _total_tokens({"usage": {"total_tokens": "unknown"}}) == 0
    assert _total_tokens({"usage": {"prompt_tokens": "x", "completion_tokens": None}}) == 0
    assert _total_tokens({"usage": "nope"}) == 0
    assert _total_tokens({"usageMetadata": {"totalTokenCount": "n/a"}}) == 0
    assert _total_tokens(None) == 0


def test_local_provider_key_detection_and_runtime_set() -> None:
    config = ModelApiConfig()
    assert not config.has_key("local")
    config.set_key("local", "http://localhost:11434/v1/")
    assert config.has_key("local")
    assert config.local_base_url == "http://localhost:11434/v1"


def test_streaming_delta_parsers() -> None:
    assert _openai_responses_delta({"type": "response.output_text.delta", "delta": "Hi"}) == "Hi"
    assert _openai_responses_delta({"type": "response.completed"}) == ""
    assert _anthropic_delta({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "yo"}}) == "yo"
    assert _anthropic_delta({"type": "message_start"}) == ""
    assert _gemini_delta({"candidates": [{"content": {"parts": [{"text": "g"}]}}]}) == "g"
    assert _openai_chat_delta({"choices": [{"delta": {"content": "tok"}}]}) == "tok"
    assert _openai_chat_delta({"choices": [{"delta": {}}]}) == ""


def test_run_task_falls_back_to_stub_for_unconfigured_local_agent() -> None:
    client = ModelApiClient(ModelApiConfig())
    profile = AgentProfile("llama", "Llama", "Meta Llama", provider="local")
    seen: list[str] = []

    result = asyncio.run(client.run_task(profile, "hello", on_delta=seen.append))

    assert "local demo agent" in result.text
    assert seen == []


def test_streaming_pipeline_parses_sse_and_emits_deltas(monkeypatch) -> None:
    body = (
        'data: {"choices":[{"delta":{"content":"Hel"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"lo"}}]}\n\n'
        "data: [DONE]\n\n"
    )
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text=body))
    original = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = transport
        return original(*args, **kwargs)

    monkeypatch.setattr(model_api.httpx, "AsyncClient", patched)

    client = ModelApiClient(ModelApiConfig(local_base_url="http://localhost:11434/v1"))
    profile = AgentProfile("llama", "Llama", "Meta Llama", provider="local", api_model="llama3")
    deltas: list[str] = []

    result = asyncio.run(client.run_task(profile, "hi", on_delta=deltas.append))

    assert deltas == ["Hel", "lo"]
    assert result.text == "Hello"
