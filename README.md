# Watchtower

Lightweight top-down Pygame simulation for watching distinct AI model agents accept tasks and move through realtime actions.

## Run

Use Python 3.11 or 3.12. The existing Python 3.14 virtualenv may try to build Pygame from source and fail without SDL development headers.

```bash
python -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m watchtower
```

## Controls

- Type a task and press Enter or click Submit to add it to the left-side todo list.
- Drag a todo card onto an agent in the world to assign it.
- Click a model in the side panel or an agent in the world, or press Ctrl/Alt/Meta + 1-5, to route new tasks to that model.
- Click a task (panel card or world station) to open its detail view with the prompt, the model's full response, and Retry / Cancel / Export / Delete actions.
- Mouse wheel scrolls the task list. `F1` toggles the help overlay; `F2` toggles dark/light theme.
- Escape closes an open overlay, or quits.

### Targeting and priority

- `@gpt summarize these logs` — one-off target a specific agent.
- `@all <prompt>` (or `/compare <prompt>`) — fan the same prompt to every agent and compare answers.
- `!high <prompt>` — set priority (`low`, `normal`, `high`, `critical`); higher priority is scheduled first under contention.
- `/auto` returns to load-based routing.

### Commands

- `/theme [dark|light]` — switch theme (defaults to toggling).
- `/cancel <id>`, `/retry <id>`, `/clear` — cancel/retry one task, or clear all finished tasks.
- `/save [path]`, `/load [path]` — persist or restore the session (`watchtower_session.json` by default).
- `/export <id> [path]` — write a task's prompt + response to a Markdown file.
- `/key <provider> <value>` — set an API key at runtime (`/key local http://localhost:11434/v1` for a local endpoint).
- `/agent add <id> <provider> <model> [Name]`, `/agent remove <id>` — edit the agent roster live.
- `/endpoint <url>`, `/auth token <tok>`, `/auth login <user> <pass>` — reconfigure the telemetry feed.

## Model API Connections

Set provider keys in the environment before launching Watchtower:

```bash
export OPENAI_API_KEY="..."
export ANTHROPIC_API_KEY="..."
export GEMINI_API_KEY="..."
.venv/bin/python -m watchtower
```

The built-in agents map to:

- GPT -> OpenAI Responses API (`gpt-5.1-mini` by default)
- Claude -> Anthropic Messages API
- Gemini -> Gemini `generateContent` (`gemini-3.5-flash` by default)
- Llama and Mistral -> `local` provider; point them at an OpenAI-compatible endpoint to run real calls

Responses stream token-by-token when supported, and the agent's progress bar holds until the real
response lands (rather than completing on synthetic motion). Without a provider key, that agent still
animates in demo mode and task progress is simulated locally.

To drive the `local` agents (or any agent set to the `local` provider) against an OpenAI-compatible
server such as Ollama, LM Studio, or vLLM:

```bash
export WATCHTOWER_LOCAL_BASE_URL="http://localhost:11434/v1"   # or set at runtime with /key local <url>
```

## Telemetry Contract

When an endpoint is configured, Watchtower polls:

```text
GET /agents/{agent_id}/telemetry
```

Expected JSON can either be flat or nested under `metrics`:

```json
{
  "status": "idle",
  "message": "live",
  "metrics": {
    "load": 0.42,
    "latency_ms": 180,
    "tokens_per_minute": 900,
    "error_rate": 0.01,
    "active_tasks": 1
  }
}
```

Without endpoint/auth configuration, the app uses deterministic local demo telemetry.
