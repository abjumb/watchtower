# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## Overview

Watchtower is a top-down Pygame simulation for watching distinct AI model agents
(GPT, Codex, Gemini, Llama, Mistral) accept tasks and move through realtime
actions. Operators submit prompts, route them to agents, and watch progress as
animated motion in a 2D world while live telemetry and (optionally) real model
API calls drive the state.

## Commands

Requires Python 3.11–3.13 (`requires-python = ">=3.11,<3.14"`). Python 3.14 will
try to build Pygame from source and fail without SDL headers.

```bash
python -m venv .venv
.venv/bin/python -m pip install -e .   # installs httpx, pygame, ipykernel
.venv/bin/python -m watchtower          # launch the app (also: `watchtower` script entrypoint)
```

Tests are pytest-style functions but `pytest` is **not** a declared dependency —
install it into the venv first:

```bash
.venv/bin/python -m pip install pytest
.venv/bin/python -m pytest                              # all tests
.venv/bin/python -m pytest tests/test_simulation.py     # one file
.venv/bin/python -m pytest tests/test_ui.py::test_dragging_todo_to_agent_assigns_task  # one test
```

UI tests run headless by setting `SDL_VIDEODRIVER=dummy` (see top of
`tests/test_ui.py`); any test that constructs `WatchtowerApp` must do this and
must call `app.poller.stop()` + `pygame.quit()` in a `finally` block, since the
constructor starts a background telemetry thread and initializes pygame.

## Architecture

The app is a single-process, 60 FPS pygame loop with background worker threads
feeding it via thread-safe handoffs. Data flows in one direction each frame:
threads → snapshot/queue → `SimulationState.update()` → draw.

### Layers (in `watchtower/`)

- **`models.py`** — Pure dataclasses and enums, the shared vocabulary. No I/O, no
  pygame. `AgentStatus`/`AgentAction`/`TaskStatus`/`TaskPriority` enums,
  `AgentProfile` (static identity incl. `provider`/`api_model`), `AgentState`
  (live position/status/metrics), `SubmittedTask` (carries its own state
  transitions via `assign_to`/`mark_progress`/`mark_model_result`/etc.), and
  `AgentMetrics`. `utcnow()` and `clamp()` live here.
- **`simulation.py`** — `SimulationState` is the authoritative game model. It owns
  agents and tasks, runs the per-frame `update(dt, telemetry)` that assigns
  waiting tasks (priority-ordered), advances agent motion (`_move_toward`,
  orbit/patrol math), and drives task progress. Roster is mutable at runtime via
  `add_agent`/`remove_agent`; tasks can be cancelled/retried/removed
  (`cancel_task`/`retry_task`/`remove_task`/`clear_finished`) and finished tasks are
  capped by `max_finished_tasks` (`_prune_finished`). `submit_comparison` fans one
  prompt to every agent (shared `group_id`). When `task.api_started` and not
  `api_completed`, synthetic progress holds at `_API_PROGRESS_CEILING` so the real
  model result is what completes the task. `default_profiles()` defines the five
  built-in agents and their provider/model mappings. `WORLD_WIDTH`/`WORLD_HEIGHT`
  are the simulation coordinate space (not screen pixels). `snapshot()` returns an
  immutable view for rendering.
- **`data_provider.py`** — Telemetry ingestion. `AgentDataProvider.fetch_all()` is
  async (httpx) and either hits `GET /agents/{id}/telemetry` on the configured
  endpoint or returns deterministic `_demo_telemetry` (sine-wave fake metrics).
  `TelemetryPoller` runs this on a daemon thread every ~2s and exposes the latest
  `ProviderSnapshot` behind a lock via `latest()`. Remote failures degrade
  gracefully to demo data rather than crashing.
- **`model_api.py`** — Real LLM calls. `ModelApiClient.run_task(profile, prompt, on_delta=None)`
  dispatches on `profile.provider` to OpenAI Responses, Anthropic Messages, Gemini
  generateContent, or an OpenAI-compatible `local` endpoint (raw httpx, no SDKs).
  When `on_delta` is supplied it streams via SSE and emits token deltas, falling
  back to a blocking call if streaming fails before any delta (`_StreamUnavailable`);
  the per-provider delta extractors (`_openai_responses_delta`, `_anthropic_delta`,
  `_gemini_delta`, `_openai_chat_delta`) are pure and unit-tested. A `local` provider
  with no configured base URL still returns a stub string. `_extract_text` recursively
  flattens the differently-shaped provider response payloads. Keys come from
  `OPENAI_API_KEY`/`ANTHROPIC_API_KEY`/`GEMINI_API_KEY`/`WATCHTOWER_LOCAL_BASE_URL`
  and can be set at runtime via `ModelApiConfig.set_key`.
- **`persistence.py`** — Pure JSON (de)serialization. `save_session`/`load_session`
  round-trip profiles + tasks; `export_task_text` writes one task's prompt/response
  to Markdown. No pygame, no network.
- **`widgets.py`** — Tiny in-engine pygame widget toolkit (`Button`, `TextInput`,
  `Toggle`, `Dropdown`). Each widget owns its rect, hit-testing, and theme-aware
  drawing; `TextInput` is focusable with a caret. No game/sim coupling — `ui.py`
  positions widgets and wires callbacks.
- **`auth.py`** — `AuthConfig` for the *telemetry* endpoint (separate from model
  API keys). Supports OAuth bearer, basic login, or demo mode; `mode` and
  `is_remote_enabled` decide whether the provider goes remote.
- **`ui.py`** — `WatchtowerApp` owns the pygame window, event loop, all drawing,
  and the wiring between the above. This is the only module that mounts the parts
  together. It also hosts the GUI built from `widgets.py`: a bottom toolbar (a `Menu`
  `Dropdown` + Compare/Clear/Theme/Settings buttons), the focusable prompt `TextInput`
  (the `input_text` property proxies its value, so commands/tests still read/write a
  string), and modal Settings / Add-agent dialogs. `self.focus` tracks the active text
  field; keystrokes route to it unless they're global shortcuts. Colors are semantic
  tokens on a `Theme` (`DARK_THEME`/`LIGHT_THEME`, toggled with `F2`/`/theme` or the
  Settings toggle) — draw code reads `self.theme.<token>` rather than raw constants. Layout constants (`LEFT_PANEL_WIDTH`, `WORLD_X`, `PANEL_X`) live at the
  top; the window is resizable and `self.screen_width`/`self.screen_height` track the
  current size (clamped to `MIN_WIDTH`/`MIN_HEIGHT`). Overlays (`_draw_detail`,
  `_draw_compare`, `_draw_inspect`, `_draw_help`) and completion effects render on top
  of the base scene; only one is interactive at a time. Per-agent load history for the
  panel sparklines lives in `self.metric_history` (sampled every `SPARK_INTERVAL`), and
  the session auto-saves to `AUTOSAVE_PATH` (`~/.watchtower/autosave.json`) every
  `AUTOSAVE_INTERVAL` and on quit, restoring on launch unless `WATCHTOWER_NO_AUTOSAVE`
  is set. The detail overlay shows a token/cost readout (`PRICE_PER_1K`, deliberately
  coarse) using `ModelCallResult.total_tokens` when the provider reports usage
  (`model_api._total_tokens`) or a length-based estimate otherwise.
- **`app.py` / `__main__.py`** — Thin entrypoints; `main()` just runs
  `WatchtowerApp().run()`.

### Concurrency model

`ui.py` is the only place threads meet the game loop, and it never shares mutable
state across threads directly:
- Telemetry: the poller thread writes a `ProviderSnapshot`; the loop reads it via
  `poller.latest()` and passes `.telemetry` into `simulation.update()`.
- Model calls: `_start_ready_model_calls()` spawns a daemon thread per task that
  runs `model_api.run_task` (via `asyncio.run`) with an `on_delta` callback. Streamed
  deltas and the final result/error are pushed onto a `queue.Queue` as tagged tuples
  (`"delta"`/`"done"`/`"error"`, task_id, payload). `_drain_model_results()` consumes
  that queue on the main thread and applies it to the simulation. `task.api_started` +
  `running_model_tasks` guard against double-dispatch.

Only agents whose provider has a live API key (or a configured `local` base URL) get
real model calls; everyone else animates with locally-simulated progress in
`_advance_agent`.

### Task lifecycle

`TODO` (in left panel, must be dragged onto an agent) → `SUBMITTED` (queued for
routing) → `ASSIGNED` → `IN_PROGRESS` → `COMPLETE`/`FAILED`; `cancel_task` moves a
task to `CANCELLED`, and `retry_task` returns a finished task to `SUBMITTED`. Routing
in `_assign_waiting_tasks`/`_candidate_agents` processes waiting tasks by
`(priority.rank, created_at)`: a `requested_agent_id` pins the task to one agent (and
waits if it's busy); otherwise the least-loaded free agent wins.

### Input commands (parsed in `ui.py:_submit_input` / `_handle_command`)

- `@<agent> <prompt>` — one-off target a specific agent; `@all <prompt>` / `/compare`
  fans the prompt to every agent.
- `!<priority> <prompt>` — `low`/`normal`/`high`/`critical` (default normal).
- `/auto` — clear selection, return to load-based routing.
- `/theme [dark|light]` (or `F2`), `F1` help overlay.
- `/cancel <id>`, `/retry <id>`, `/clear` — task lifecycle controls (also via the
  detail overlay buttons).
- `/save [path]`, `/load [path]`, `/export <id> [path]` — persistence (`persistence.py`).
- `/key <provider> <value>` — set a model API key/base URL at runtime.
- `/agent add <id> <provider> <model> [Name]`, `/agent remove <id>` — edit the roster
  (also calls `poller.set_profiles`).
- `/endpoint <url>`, `/auth token <tok>`, `/auth login <user> <pass>` — reconfigure
  the telemetry feed at runtime; these rebuild `AuthConfig` and call
  `poller.configure()`.
- Ctrl/Alt/Meta + 1–5 selects an agent (a bare number key is text input).

## Conventions

- All modules use `from __future__ import annotations` and modern typing
  (`X | None`, builtin generics).
- Dataclasses use `slots=True`. State transitions are methods on the dataclass
  (e.g. `SubmittedTask.mark_model_result`) rather than mutated externally.
- Keep `models.py` and `simulation.py` free of pygame and network I/O so the
  simulation stays unit-testable headlessly (the existing tests drive
  `SimulationState.update` directly with no display).
- Provider response parsing should go through `_extract_text` rather than indexing
  into provider-specific JSON shapes.

When changing agent roster, provider names, or default models, update
`default_profiles()` in `simulation.py` — `model_api.py` dispatches on the
`provider` string and `data_provider.py` keys telemetry by `profile.id`.

## Design intent

See `.design-context.md`: the target audience is casual "vibe coders"; the vibe is
chill, playful, game-like (not a technical command center). New UI must support
both dark (default) and light modes with shared semantic colors, and must not slow
down the keyboard-first task flow.

## gstack (REQUIRED — global install)

**Before doing ANY work, verify gstack is installed:**

```bash
test -d ~/.Codex/skills/gstack/bin && echo "GSTACK_OK" || echo "GSTACK_MISSING"
```

If GSTACK_MISSING: STOP. Do not proceed. Tell the user:

> gstack is required for all AI-assisted work in this repo.
> Install it:
> ```bash
> git clone --depth 1 https://github.com/garrytan/gstack.git ~/.Codex/skills/gstack
> cd ~/.Codex/skills/gstack && ./setup --team
> ```
> Then restart your AI coding tool.

Do not skip skills, ignore gstack errors, or work around missing gstack.

Using gstack skills: After install, skills like /qa, /ship, /review, /investigate,
and /browse are available. Use /browse for all web browsing.
Use ~/.Codex/skills/gstack/... for gstack file paths (the global path).
