"""Pixel-art sprite system for the Fieldnotes × Overworld UI refresh.

Ported from the Claude Design bundle (watchtower-sprites.js) so the pygame app
renders the same characters as the prototype. Grids use one character per
pixel: o=outline/ink, e=eyes, m=mouth, b=body, l=light, d=dark, w=white,
s=screen, plus fixed prop colors (g h k x r c y u); "." is transparent.

Pose transforms (idle | walk | work | sleep | cheer) derive variants from the
base grid at runtime. Rendered surfaces are cached by every input that affects
the bitmap, mirroring the app's other render caches.
"""
from __future__ import annotations

import pygame

SPRITES: dict[str, tuple[str, ...]] = {
    "gpt": (
        "......l......",
        "......o......",
        "....ooooo....",
        "...obbbbbo...",
        "..obllbbbbo..",
        ".obbbbbbbbbo.",
        ".obebbbbbebo.",
        ".obbbbbbbbbo.",
        ".obbbmmmbbbo.",
        "..obbbbbbbo..",
        "...obbbbbo...",
        "....ooooo....",
        "....o...o....",
    ),
    "claude": (
        "..oo.....oo..",
        "..obo...obo..",
        ".ooboooooboo.",
        ".obbbbbbbbbo.",
        ".oblbbbbbbbo.",
        ".obebbbbbebo.",
        ".obbbbbbbbbo.",
        ".obbmmmmmbbo.",
        ".obbbbbbbbbo.",
        "..obbbbbbbo..",
        "...ooooooo...",
        "....o...o....",
    ),
    "gemini": (
        "...l.....l...",
        "...o.....o...",
        "..ooooooooo..",
        ".obbbbbbbbbo.",
        ".oblbbbbbbbo.",
        ".obebbbbbebo.",
        ".obbbbbbbbbo.",
        ".obbbmmmbbbo.",
        ".obbbbbbbbbo.",
        ".obbbbbbbbbo.",
        "..obbbbbbbo..",
        "...ooooooo...",
        "....o...o....",
    ),
    "llama": (
        "..oo.....oo..",
        "..obo...obo..",
        "..obo...obo..",
        ".ooboooooboo.",
        ".obbbbbbbbbo.",
        ".obebbbbbebo.",
        ".obbbbbbbbbo.",
        ".obblllllbbo.",
        ".obblmlmlbbo.",
        ".obblllllbbo.",
        "..obbbbbbbo..",
        "..obbbbbbbo..",
        "...ooooooo...",
        "....o...o....",
    ),
    "mistral": (
        "....oooooo....",
        "...obbbbbbo...",
        "..obblbbbbbo..",
        "..obbbbbbbbo..",
        "l.obebbbbebo..",
        "..obbbbbbbbo..",
        "llobbmmmmbbo..",
        "..obbbbbbbbo..",
        "l..obbbbbbo...",
        "....oooooo....",
    ),
    "desk": (
        "....oooooooo....",
        "...obssssssbo...",
        "...obssssssbo...",
        "...obssssssbo...",
        "....oooooooo....",
        ".......oo.......",
        "oooooooooooooooo",
        "obbbbbddddbbbbbo",
        "oooooooooooooooo",
        ".oo..........oo.",
        ".oo..........oo.",
        ".oo..........oo.",
    ),
    "plant": (
        "....ooo....",
        "...oghgo...",
        "..oghhhgo..",
        ".oghgghhgo.",
        ".ogghhgggo.",
        "..oghghgo..",
        "...ooooo...",
        "..okkkkko..",
        "..okkkkko..",
        "..oxxxxxo..",
        "...ooooo...",
    ),
    "shelf": (
        ".oooooooooooo.",
        ".obbbbbbbbbbo.",
        ".obrrucyygubo.",
        ".obrrucyygubo.",
        ".obbbbbbbbbbo.",
        ".obucgryrucbo.",
        ".obucgryrucbo.",
        ".obbbbbbbbbbo.",
        ".obyugcruygbo.",
        ".obyugcruygbo.",
        ".obbbbbbbbbbo.",
        ".oooooooooooo.",
        ".oo........oo.",
    ),
    "cooler": (
        "..ooooo..",
        ".occccco.",
        ".occwcco.",
        ".occccco.",
        "..ooooo..",
        ".owwwwwo.",
        ".owwcwwo.",
        ".owwwwwo.",
        ".owwwwwo.",
        "..ooooo..",
        "..o...o..",
    ),
    "tower": (
        ".....oooo.....",
        "....obbbbo....",
        "..oobbbbbboo..",
        ".obbbbbbbbbbo.",
        ".oooooooooooo.",
        "...oyyyyyyo...",
        "...oyyssyyo...",
        "...oyyyyyyo...",
        "...oooooooo...",
        "....oo..oo....",
        "....oo..oo....",
        "....oooooo....",
        "....oo..oo....",
        "....oo..oo....",
    ),
    "cat": (
        ".o..o.....",
        ".oo.oo....",
        ".obbbbo...",
        ".obxbxbo..",
        ".obbbbbbo.",
        "obbbbbbbbo",
        "oooooooooo",
    ),
    "coffee": (
        "..oooooo....",
        "..obbbbo....",
        "..obddbo....",
        "..obwwbo....",
        "..obbbbo....",
        "..oorroo.cc.",
        "oooooooooooo",
        "okkkkkkkkkko",
        "oxxxxxxxxxxo",
        "oooooooooooo",
        ".oo......oo.",
    ),
    "clock": (
        "..ooooo..",
        ".owwwwwo.",
        "owwwowwwo",
        "owwwowwwo",
        "owwwooowo",
        "owwwwwwwo",
        "owwwwwwwo",
        ".owwwwwo.",
        "..ooooo..",
    ),
}

_PAPER_INK = (42, 38, 32)
_NIGHT_INK = (13, 12, 28)

MODES: dict[str, dict] = {
    "paper": {
        "ink": _PAPER_INK,
        "tints": {
            "gpt": (62, 156, 112),
            "claude": (207, 111, 62),
            "gemini": (62, 115, 207),
            "llama": (134, 99, 214),
            "mistral": (223, 171, 53),
        },
    },
    "night": {
        "ink": _NIGHT_INK,
        "tints": {
            "gpt": (93, 211, 158),
            "claude": (232, 130, 90),
            "gemini": (98, 160, 255),
            "llama": (169, 133, 255),
            "mistral": (238, 195, 94),
        },
    },
}

_FIXED = {
    "w": (247, 247, 255),
    "g": (62, 156, 112),
    "h": (99, 189, 140),
    "k": (176, 105, 63),
    "x": (138, 79, 45),
    "r": (194, 95, 95),
    "c": (90, 164, 196),
    "y": (211, 165, 63),
    "u": (122, 107, 201),
}


def shade(color: tuple[int, int, int], amount: float) -> tuple[int, int, int]:
    """Lighten (amount>=0, toward white) or darken (toward black) a color."""
    target = 255 if amount >= 0 else 0
    strength = abs(amount)
    return tuple(int(round(channel + (target - channel) * strength)) for channel in color)


def _cells_of(grid: list[list[str]], char: str) -> list[tuple[int, int]]:
    return [(y, x) for y, row in enumerate(grid) for x, cell in enumerate(row) if cell == char]


def _mouth_runs(grid: list[list[str]], minimum: int) -> list[tuple[int, int, int]]:
    runs: list[tuple[int, int, int]] = []
    for y, row in enumerate(grid):
        start = -1
        for x in range(len(row) + 1):
            cell = row[x] if x < len(row) else None
            if cell == "m":
                if start < 0:
                    start = x
            elif start >= 0:
                if x - start >= minimum:
                    runs.append((y, start, x - 1))
                start = -1
    return runs


def pose_grid(key: str, pose: str = "idle") -> tuple[str, ...]:
    """A sprite grid in the given pose (idle | walk | work | sleep | cheer)."""
    base = SPRITES.get(key)
    if base is None:
        raise KeyError(f"Unknown sprite: {key!r}")
    if pose == "idle" or not pose:
        return base
    grid = [list(row) for row in base]
    if pose == "sleep":
        for y, x in _cells_of(grid, "e"):
            grid[y][x] = "b"
            if y + 1 < len(grid) and x < len(grid[y + 1]):
                grid[y + 1][x] = "e"
        for y, start, end in _mouth_runs(grid, 3):
            middle = (start + end) // 2
            for x in range(start, end + 1):
                if x != middle:
                    grid[y][x] = "b"
    elif pose == "work":
        for y, x in _cells_of(grid, "e"):
            if y > 0 and grid[y - 1][x] in ("b", "l"):
                grid[y - 1][x] = "o"
    elif pose == "cheer":
        runs = _mouth_runs(grid, 3)
        if runs:
            y, start, end = runs[0]
            if y + 1 < len(grid):
                for x in range(start + 1, end):
                    grid[y + 1][x] = "m"
        else:
            noses = _cells_of(grid, "m")
            if noses:
                y = noses[0][0] + 1
                if y < len(grid):
                    light_columns = [x for x, cell in enumerate(grid[y]) if cell == "l"]
                    if len(light_columns) >= 3:
                        middle = len(light_columns) // 2
                        for index in (middle - 1, middle, middle + 1):
                            if 0 <= index < len(light_columns):
                                grid[y][light_columns[index]] = "m"
    elif pose == "walk":
        last = grid[-1]
        feet = [x for x, cell in enumerate(last) if cell == "o"]
        if len(feet) == 2:
            last[feet[0]] = "."
            last[feet[1]] = "."
            if feet[0] - 1 >= 0:
                last[feet[0] - 1] = "o"
            if feet[1] + 1 < len(last):
                last[feet[1] + 1] = "o"
    return tuple("".join(row) for row in grid)


_SURFACE_CACHE: dict[tuple, pygame.Surface] = {}
_SURFACE_CACHE_MAX = 256


def sprite_size(key: str, scale: int = 4) -> tuple[int, int]:
    rows = SPRITES[key]
    return max(len(row) for row in rows) * scale, len(rows) * scale


def sprite_surface(
    key: str,
    pose: str = "idle",
    tint: tuple[int, int, int] = (143, 147, 255),
    ink: tuple[int, int, int] = _NIGHT_INK,
    screen: tuple[int, int, int] = (20, 19, 39),
    scale: int = 4,
) -> pygame.Surface:
    """Cached SRCALPHA surface for a sprite. Treat the result as immutable."""
    cache_key = (key, pose, tint, ink, screen, scale)
    surface = _SURFACE_CACHE.get(cache_key)
    if surface is not None:
        return surface
    rows = pose_grid(key, pose)
    columns = max(len(row) for row in rows)
    surface = pygame.Surface((columns * scale, len(rows) * scale), pygame.SRCALPHA)
    palette = {
        "o": ink,
        "e": ink,
        "m": ink,
        "b": tint,
        "l": shade(tint, 0.42),
        "d": shade(tint, -0.25),
        "s": screen,
        **_FIXED,
    }
    for y, row in enumerate(rows):
        for x, char in enumerate(row):
            color = palette.get(char)
            if color:
                surface.fill(color, (x * scale, y * scale, scale, scale))
    if len(_SURFACE_CACHE) >= _SURFACE_CACHE_MAX:
        _SURFACE_CACHE.clear()
    _SURFACE_CACHE[cache_key] = surface
    return surface
