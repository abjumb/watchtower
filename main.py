"""Entry point for both desktop (CPython) and browser (pygbag/WASM) runs.

Desktop: ``python main.py`` (or the ``watchtower`` console script / ``python -m
watchtower``) runs the normal threaded app. In the browser, pygbag executes this
module under an Emscripten runtime where ``sys.platform == "emscripten"``; there
we run the single-threaded async loop in demo mode (no threads, no network, no
API keys).
"""
from __future__ import annotations

import sys


async def main() -> None:
    # pygbag looks for a top-level ``main`` coroutine to drive.
    from watchtower.ui import WatchtowerApp

    await WatchtowerApp(web_mode=True).run_async()


def _run_desktop() -> None:
    from watchtower.app import main as desktop_main

    desktop_main()


if __name__ == "__main__":
    if sys.platform == "emscripten":
        import asyncio

        asyncio.run(main())
    else:
        _run_desktop()
