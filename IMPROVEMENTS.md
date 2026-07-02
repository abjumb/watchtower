# Watchtower Improvements Backlog

Work queue for the improvements loop. One item per commit on `claude/improvements`.
Items were identified by a multi-lens analysis pass and every claim below was
independently re-verified against HEAD `4e23a38` (perf numbers reproduced on this
machine). Perf items first (by measured impact), then visual items.

## How the loop uses this file

- The loop takes the **topmost unchecked item**, implements it as one commit, and
  runs the judge. It never flips an item to `done` — that is human-only.
- **Statuses:** `todo` → `in-progress` → `awaiting-review` | `blocked(<reason>)` → `done` (human flips).
- **Perf item done-criterion (machine):** full pytest suite green headless AND
  `benchmarks/benchmark.py` median improves by **>2%** vs the current baseline below
  (observed repeat noise is ~0.8%), plus the item's structural check. On acceptance,
  append the new number to the baseline history; it becomes the next item's baseline.
- **Visual item done-criterion:** pytest green AND before/after screenshots (BOTH
  themes) saved under `screenshots/item-<NN>/` AND status set to `awaiting-review`.
  The loop does not start the next **visual** item until this one is human-approved.
- **Damping:** retry cap 3 per item then `blocked(<reason>)` and move on; hard stop
  after 10 items or 2 consecutive blocks. PR only — never merge.

## Boundaries (apply to every item)

- No test deleted or weakened.
- `watchtower/models.py` and `watchtower/simulation.py` stay free of pygame and network I/O.
- Both dark and light themes must render correctly.
- No feature or visual effect removed to gain speed.
- Keyboard-first task flow must not slow down.

## Baseline

Command: `.venv/bin/python benchmarks/benchmark.py --json` (600 frames × 3 repeats, best median)
Tests: `SDL_VIDEODRIVER=dummy WATCHTOWER_NO_AUTOSAVE=1 .venv/bin/python -m pytest -q`

| Date | Commit | Median ms/frame | Notes |
|------|--------|-----------------|-------|
| 2026-07-01 | 4e23a38 (master) | 2.4616 | medians [2.4616, 2.4769, 2.4819]; 64 tests green |
| 2026-07-01 | claude/improvements P1 | 1.1219 | −54.4% vs 4e23a38; medians [1.2522, 1.1219, 1.1269]; 64 tests green; pixel-parity exact both themes |
| 2026-07-01 | claude/improvements P2 | 0.8709 | −22.4% vs P1 (−64.6% cumulative); medians [0.8709, 0.8848, 0.8776]; caches stable at 11+5 entries over 200 frames; pixel-parity exact both themes |
| 2026-07-01 | claude/improvements P3 | **0.7731** | −11.2% vs P2 (−68.6% cumulative); medians [0.7731, 0.7758, 0.7774]; 0 text-cache misses on identical redraw (42 entries); pixel-parity exact both themes |

## Loop state

- Items completed this run: 3 / 10
- Consecutive blocks: 0

---

## Items

### 1. [x] P1 · Bake static scene chrome into the cached background surface
**Status:** awaiting-review · **Type:** perf · **Impact:** high · **Risk:** medium · **Files:** `watchtower/ui.py`

> **Result (machine criteria met):** benchmark 2.4616 → 1.1219 ms/frame (−54.4%);
> `_draw_world` cProfile share 44% → 0.24%; deterministic frame md5 identical
> before/after in BOTH themes (refactor is provably render-neutral); 64 tests
> green. Scope note: Models-panel frame and input dock intentionally stay live —
> they are drawn after agents/effects and must cover glow overflow (z-order).

~70% of the base frame is pixel-identical chrome redrawn every frame. `_draw_world`
(ui.py:991-1004) rebuilds the world liquid rect (918×638 SRCALPHA shadow + glow via
`_draw_liquid_rect`), 23 grid lines each with a fresh `_blend`, 2 orbit circles, and
the "Watchtower" heading — measured 1.211 ms/frame in isolation; cProfile: 44% of
total frame time. The todo-panel frame (ui.py:1551-1554) and Models-panel frame
(ui.py:1071-1073) add more. All of it depends only on `(screen_width, screen_height,
theme)` — exactly the existing `_bg_surface`/`_bg_cache_key` key (ui.py:1236-1257)
whose full-screen blit already happens each frame, so baking chrome there adds zero
incremental cost. Keep live: `flash_message`, everything task/agent-dependent, and
the **input dock** (its soft shadow overlaps the world border bottom pixels —
z-order caveat; drawing it live avoids any visual diff). Theme toggle and resize
already invalidate via the existing cache key.

**Acceptance:** benchmark median improves >2% vs baseline (expect ~-30-40%); cProfile
`_draw_world` cumulative share drops to <5% (from 44%); F2 theme toggle and window
resize re-render chrome correctly in both themes; flash message still updates
immediately; tests green.

### 2. [x] P2 · Cache SRCALPHA shadow/glow surfaces instead of allocating per call
**Status:** awaiting-review · **Type:** perf · **Impact:** high · **Risk:** low · **Files:** `watchtower/ui.py`, `watchtower/widgets.py`

> **Result (machine criteria met):** benchmark 1.1219 → 0.8709 ms/frame (−22.4%;
> −64.6% cumulative vs master); caches stable at 11 rect + 5 glow entries across
> 200 frames (zero per-frame Surface allocations on hits); deterministic frame
> md5 identical to the original pre-P1 hashes in BOTH themes; 64 tests green.
> Shared factory `widgets.rounded_alpha_surface` used by ui and widgets.

`_draw_liquid_rect` (ui.py:1270-1277) allocates a fresh SRCALPHA surface, rasterizes
a rounded rect, and blits — twice (shadow + glow) — on every call (~16-17 calls/frame;
cProfile: ~64-67% of frame time cumulative). Same pattern in `widgets._liquid_rect`
(widgets.py:21-25, ~6×/frame) and the per-agent 92×92 glow (ui.py:1014-1017, 5
agents × 60fps). The bitmaps depend only on `(width, height, radius, rgba)` — add a
small bounded module-level surface cache (~64 entries; distinct sizes are few and
stable). Blits remain; only allocation+rasterization is eliminated. Measured
standalone: 2.703 → 1.975 ms/frame. Complementary to P1 (still covers todo cards,
text input, buttons, stations, agents, dialogs afterwards).

**Acceptance:** benchmark median improves >2% vs current baseline; on cache hits no
`pygame.Surface` constructions originate from `_draw_liquid_rect`/`_liquid_rect`/
`_draw_agent` (counter or profile); output pixel-identical in both themes; tests green.

### 3. [x] P3 · Memoized text-surface cache for per-frame `font.render` calls
**Status:** awaiting-review · **Type:** perf · **Impact:** high · **Risk:** low · **Files:** `watchtower/ui.py`

> **Result (machine criteria met):** benchmark 0.8709 → 0.7731 ms/frame (−11.2%;
> −68.6% cumulative vs master); 0 font.render calls on an identical redraw
> (cache stable at 42 entries — beats the ≤3 criterion); all four ui.py render
> sites (_text + agent name/action + station label) routed through the cache;
> frame md5 identical to pre-P1 baseline in BOTH themes; 64 tests green.

Every drawn string re-rasterizes each frame: `_text` (ui.py:1525-1526) calls
`font.render` unconditionally, ~25-30×/frame with mostly unchanging inputs (static
headings, per-agent model/status lines, task rows, "Submit"), plus 3 direct renders
bypassing `_text` (ui.py:1024, 1026, 1058). Add a bounded memo cache keyed on
`(font id, text, color)` (and include theme or clear on toggle). Note: a global
render counter will also see widgets.py renders that this ui.py-scoped item does
not cover (that's P6) — scope the structural check to `_text` call sites.

**Acceptance:** benchmark median improves >2% vs current baseline; second
consecutive draw of an unchanged scene performs ≤3 `font.render` calls from `_text`
paths (vs ~40); frame pixel-identical in both themes; tests green.

### 4. [ ] P4 · Memoize `_wrap` and stop wrapping past the visible line budget
**Status:** todo · **Type:** perf · **Impact:** medium · **Risk:** low · **Files:** `watchtower/ui.py`

`_wrap` (ui.py:1528-1544) calls `font.size` once per word every frame while an
overlay is open: detail overlay wraps the full ≤4000-char body (~480-650 words →
~500 `font.size` calls/frame) then slices to ~15 visible lines, discarding most of
the work; compare overlay re-wraps each visible card per frame (ui.py:1363, up to
6 cards). Memoize wrapped output keyed on `(text, font, width)` and/or stop at the
line budget (`max_lines`) during wrapping.

**Acceptance:** benchmark unchanged-or-better on base scene AND with detail overlay
open on a 4000-char response, frame time within ~1 ms of base (vs ~+3.6 ms); second
consecutive overlay frame performs 0 `font.size` calls in the wrap path; wrapped
line output byte-identical for same inputs; tests green.

### 5. [ ] P5 · Cache TextInput scroll/render work (per-frame `font.size` loops)
**Status:** todo · **Type:** perf · **Impact:** medium · **Risk:** low · **Files:** `watchtower/widgets.py`, `watchtower/ui.py`

`TextInput.draw` (widgets.py:116-139) runs every frame for the prompt box: renders
the placeholder each frame (line 124; ui.py:1147 also rebuilds the placeholder
f-string per frame), and with text present runs a caret-scroll loop calling
`font.size` on a substring per iteration (line 131) plus a char-by-char trim loop
(line 134) — O(n) `font.size` calls each measuring O(n) substrings per frame — then
re-renders and re-measures (136, 138). Cache scroll offset + rendered surface keyed
on `(value, caret, width, theme, focused)`; recompute only on change (caret blink
must still repaint).

**Acceptance:** benchmark median improves or holds vs current baseline; with a
400-char value and caret at end, second consecutive frame performs 0 `font.size`
calls in TextInput.draw; display/caret placement identical for same state in both
themes; typing updates on next frame; tests green.

### 6. [ ] P6 · Stop rebuilding toolbar Buttons per frame; cache widget label surfaces
**Status:** todo · **Type:** perf · **Impact:** medium · **Risk:** low · **Files:** `watchtower/ui.py`, `watchtower/widgets.py`

`_draw_input` calls `_toolbar_buttons()` every frame (ui.py:1154) reconstructing 4
Button dataclasses + `font.size` per label (ui.py:762-776); `Button.draw`,
`Dropdown.draw_button` ("☰ Menu"), `Dropdown.draw_items`, and `Toggle.draw` all
`font.render` constant strings per frame (widgets.py:60, 202, 212, 164). Memoize
the toolbar button list keyed on `(screen_width, screen_height, theme)` and cache
label surfaces in the widgets keyed on `(text, color)`.

**Acceptance:** benchmark median improves or holds vs current baseline; idle app's
second consecutive frame shows 0 `font.size`/`font.render` calls attributable to
toolbar/menu widgets; buttons still fire at correct rects after resize; identical
output in both themes; tests green.

### 7. [ ] P7 · Skip scheduler rescan/re-sort when no agent is free
**Status:** todo · **Type:** perf · **Impact:** medium (scales with queue) · **Risk:** low · **Files:** `watchtower/simulation.py`

`_assign_waiting_tasks` (simulation.py:228-232) runs every frame: filters + sorts
all SUBMITTED tasks, then per waiting task `_candidate_agents` (244-254) rebuilds
and re-sorts the free-agent list. With all agents busy this is pure waste 60×/sec —
measured 170 µs/call at 5 busy agents + 490 queued tasks (~89% of `update()`); an
`any(agent free)` early-exit guard is ~0.6 µs. Pure-Python fix; simulation.py stays
pygame-free.

**Acceptance:** tests green unchanged (esp. `test_targeted_task_waits_for_busy_agent`,
`test_auto_routing_waits_when_all_agents_are_busy`); new pytest proves 0 sorts occur
in `_assign_waiting_tasks` when no agent is free under load; benchmark median
improves or holds; routing behavior identical.

### 8. [ ] V1 · Upgrade task-completion effect to an eased particle burst in agent color
**Status:** todo · **Type:** visual · **Impact:** high · **Risk:** low · **Files:** `watchtower/ui.py`

The completion payoff is a single 2px circle outline growing linearly (spawn
ui.py:705-711; draw 1062-1067) — reads as a debug marker, not a reward. Store the
agent's accent color at spawn (extend `_effect_origin` ui.py:713-717 to return it —
currently it discards the agent), draw an ease-out ring (`1-(1-t)^3`) plus 8-10
deterministic radiating dots fading out. Both themes via existing blend against
`theme.bg`.

**Acceptance:** pytest green (`_sync_completion_effects`/`_update_effects` covered);
effects list still empties after TTL; before/after screenshots mid-effect in both
themes under `screenshots/item-08/`; → `awaiting-review`; human approval.

### 9. [ ] V2 · Render-side motion easing + idle breathing for agents
**Status:** todo · **Type:** visual · **Impact:** high · **Risk:** medium · **Files:** `watchtower/ui.py`

Agents move at constant velocity and halt instantly (`_move_toward`
simulation.py:333-340); IDLE agents are static except orbit + blink. Fix entirely
render-side (simulation untouched, stays pygame-free): keep `self._render_pos`
exponentially smoothed toward sim position (`pos += (sim-pos)*min(1, dt*8)`) and add
a subtle breathing pulse to idle agents. Caveat from verification: the 92×92 glow
surface clips a +1.5px pulse — enlarge the surface a few px or pulse only the body.
Drag-drop hit rects must keep using sim positions (or drop tolerance) so
`test_dragging_todo_to_agent_assigns_task` stays honest.

**Acceptance:** pytest green incl. drag test; two consecutive captures of an idle
agent differ (breathing); before/after screenshots both themes under
`screenshots/item-09/`; → `awaiting-review`; human approval.

### 10. [ ] V3 · Hover feedback for all clickable game elements
**Status:** todo · **Type:** visual · **Impact:** high · **Risk:** low · **Files:** `watchtower/ui.py`

Only widget-toolkit buttons have hover states (widgets.py:46-58). Agents, task
stations, panel task/agent rows, and todo cards are hover-dead. Hit rects already
exist (`_station_hits`/`_panel_task_hits`/`_todo_task_rect`/`_agent_row_rect`);
mouse pos is already fetched in `_draw_input`. Use ui.py's `_blend` (ui.py:1646;
widgets' `_mix` is widget-local) to lift fill/border toward `theme.text` on hover,
matching the widget recipe.

**Acceptance:** pytest green; base scene byte-identical when mouse is outside all
hit rects; screenshots of hovered vs non-hovered agent/task-row/todo-card in both
themes under `screenshots/item-10/`; → `awaiting-review`; human approval.

### 11. [ ] V4 · Fix agent accent-color contrast in light theme (1.4-3.2:1 today)
**Status:** todo · **Type:** visual · **Impact:** high · **Risk:** low · **Files:** `watchtower/ui.py`

Raw dark-theme accent hexes go straight into text/sparkline rendering: inspect
title (ui.py:1400-1402), compare-card header (1357-1358), sparklines (1085,
1288-1297, 1417). Measured on light panel fill: Mistral 1.76:1, GPT 2.45:1, runtime
palette down to 1.36:1. Add a theme-aware accent resolver; verification measured
that a ~0.45 blend toward `theme.text` (not 0.35) is needed to clear 3:1 worst-case.
Dark theme must remain visually unchanged.

**Acceptance:** pytest green; measured contrast ≥3:1 for every default + runtime
palette color on light surfaces; dark theme unchanged; screenshots of inspect,
compare, and panel sparklines in both themes under `screenshots/item-11/`;
→ `awaiting-review`; human approval.

### 12. [ ] V5 · Drop-target highlight while dragging a todo card
**Status:** todo · **Type:** visual · **Impact:** medium · **Risk:** low · **Files:** `watchtower/ui.py`

During a drag nothing shows which agent will receive the drop; a miss just flashes
"Drop the todo on an agent" (ui.py:1594). While `dragging_task_id` is set, resolve
the hovered agent (reuse the snapshot already captured at ui.py:732 — don't take a
fresh one) and draw a pulsing accent ring around it.

**Acceptance:** pytest green incl. drag-assignment test; no rendering change when
`dragging_task_id` is None; mid-drag screenshots in both themes under
`screenshots/item-12/`; → `awaiting-review`; human approval.

### 13. [ ] V6 · Replace tofu-box "☰" glyph on the Menu button with a drawn hamburger icon
**Status:** todo · **Type:** visual · **Impact:** medium · **Risk:** low · **Files:** `watchtower/widgets.py`

`Dropdown.draw_button` (widgets.py:202) renders "☰ Menu" but Helvetica Neue has no
U+2630 glyph — verified empirically: renders as the .notdef tofu box on the
always-visible toolbar in both themes. Drop the glyph from the label and draw three
short horizontal lines (theme-colored) as the icon.

**Acceptance:** pytest green; toolbar screenshot in both themes shows a crisp
3-line icon under `screenshots/item-13/`; → `awaiting-review`; human approval.

### 14. [ ] V7 · Flash message as a fading toast pill
**Status:** todo · **Type:** visual · **Impact:** medium · **Risk:** low · **Files:** `watchtower/ui.py`

`flash_message` is bare text at a fixed spot (ui.py:1004) that never expires —
"Cleared 0 finished tasks" sits forever, re-rendered every frame. Convert to a
property whose setter records `self._flash_at = simulation.elapsed_seconds` (52
assignment sites stay untouched), draw as a rounded pill (`_draw_liquid_rect`,
`surface_alt` fill + `grid` border) fading out after ~3.5s on a cached text surface
(key must include theme — `theme.muted` differs across modes).

**Acceptance:** pytest green (`flash_message` stays a read/write str property);
screenshots: pill visible right after a command, absent after fade, both themes,
under `screenshots/item-14/`; no per-frame `font.render` for an unchanged message;
→ `awaiting-review`; human approval.
