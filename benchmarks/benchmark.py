"""Headless frame-time benchmark for Watchtower.

Times one full frame (simulation update + housekeeping + draw + flip) under
``SDL_VIDEODRIVER=dummy``, mirroring the single-threaded ``run_async`` frame
recipe so no poller or model-call threads add noise. This is the
reconciliation baseline for perf items in IMPROVEMENTS.md: a perf item is
only "done" when the median ms/frame reported here improves vs the baseline
recorded in that file.

Usage:
    .venv/bin/python benchmarks/benchmark.py [--frames N] [--repeats R] [--json]

Reports the best-of-repeats median (least noisy on a shared machine) plus
mean and p90 for context.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import time

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ["WATCHTOWER_NO_AUTOSAVE"] = "1"
# Never dispatch real model calls from a benchmark run.
for _key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "WATCHTOWER_LOCAL_BASE_URL"):
    os.environ.pop(_key, None)

import pygame

from watchtower.models import TaskPriority
from watchtower.ui import WatchtowerApp

DT = 1.0 / 60.0
PRIORITIES = [TaskPriority.LOW, TaskPriority.NORMAL, TaskPriority.HIGH, TaskPriority.CRITICAL]


def build_app() -> WatchtowerApp:
    """Construct the app with a representative busy scene.

    web_mode=True keeps the run single-threaded and hermetic: no autosave
    restore on construction and autosave stays disabled. The telemetry poller
    is never started; demo telemetry is computed inline like run_async does.
    """
    app = WatchtowerApp(web_mode=True)
    sim = app.simulation
    agent_ids = [profile.id for profile in sim.profiles]
    for i in range(12):
        sim.submit_task(
            f"benchmark workload item {i}: summarise subsystem {i % 5} and report anomalies",
            requested_agent_id=agent_ids[i % len(agent_ids)] if i % 3 == 0 else None,
            priority=PRIORITIES[i % len(PRIORITIES)],
        )
    for i in range(4):
        sim.create_todo_task(f"todo card {i}: follow-up review")
    return app


def run_once(frames: int, warmup: int) -> list[float]:
    app = build_app()
    try:
        snapshot = app.provider.demo_snapshot(app.simulation.profiles)
        poll_timer = 0.0
        samples: list[float] = []
        for i in range(warmup + frames):
            start = time.perf_counter()
            pygame.event.pump()
            poll_timer += DT
            if poll_timer >= 2.0:
                poll_timer = 0.0
                snapshot = app.provider.demo_snapshot(app.simulation.profiles)
            app.simulation.update(DT, snapshot.telemetry)
            app._sync_completion_effects()
            app._update_effects(DT)
            app._sample_metrics(DT)
            app._draw(snapshot)
            pygame.display.flip()
            if i >= warmup:
                samples.append((time.perf_counter() - start) * 1000.0)
        return samples
    finally:
        app.poller.stop()
        pygame.quit()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frames", type=int, default=600, help="timed frames per repeat")
    parser.add_argument("--warmup", type=int, default=60, help="untimed warmup frames per repeat")
    parser.add_argument("--repeats", type=int, default=3, help="independent app constructions")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON only")
    args = parser.parse_args()

    runs = [run_once(args.frames, args.warmup) for _ in range(args.repeats)]
    medians = [statistics.median(run) for run in runs]
    best = min(range(len(runs)), key=lambda i: medians[i])
    result = {
        "frames": args.frames,
        "repeats": args.repeats,
        "median_ms": round(medians[best], 4),
        "mean_ms": round(statistics.fmean(runs[best]), 4),
        "p90_ms": round(statistics.quantiles(runs[best], n=10)[8], 4),
        "all_medians_ms": [round(m, 4) for m in medians],
    }
    if args.json:
        print(json.dumps(result))
    else:
        print(f"median {result['median_ms']} ms/frame  (mean {result['mean_ms']}, p90 {result['p90_ms']})")
        print(f"medians across {args.repeats} repeats: {result['all_medians_ms']}")


if __name__ == "__main__":
    main()
