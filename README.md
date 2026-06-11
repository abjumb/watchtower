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

- Type a task and press Enter or click Submit.
- Click a model in the side panel, or press Ctrl/Alt/Meta + 1-5, to route new tasks to that model.
- Prefix a task with an agent id for one-off targeting, for example `@gpt summarize these logs`.
- `/auto` returns to load-based routing.
- `/endpoint https://your-observability-service.example`
- `/auth token YOUR_OAUTH_TOKEN`
- `/auth login USERNAME PASSWORD`
- Escape quits.

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
