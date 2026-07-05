import os

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

import pytest

from watchtower.sprites import MODES, SPRITES, pose_grid, shade, sprite_size, sprite_surface


def test_modes_cover_all_five_agents_in_both_inks() -> None:
    for mode in ("paper", "night"):
        assert set(MODES[mode]["tints"]) == {"gpt", "claude", "gemini", "llama", "mistral"}
        assert len(MODES[mode]["ink"]) == 3


def test_shade_lightens_and_darkens() -> None:
    assert shade((100, 100, 100), 0.5) == (178, 178, 178)
    assert shade((100, 100, 100), -0.5) == (50, 50, 50)
    assert shade((10, 20, 30), 0.0) == (10, 20, 30)


def test_idle_pose_returns_base_grid_unchanged() -> None:
    assert pose_grid("gpt", "idle") == SPRITES["gpt"]
    assert pose_grid("gpt") == SPRITES["gpt"]


def test_unknown_sprite_raises() -> None:
    with pytest.raises(KeyError):
        pose_grid("dragon")


def test_sleep_pose_moves_eyes_down_and_narrows_mouth() -> None:
    base = SPRITES["gpt"]
    slept = pose_grid("gpt", "sleep")
    eye_row_base = next(i for i, row in enumerate(base) if "e" in row)
    assert "e" not in slept[eye_row_base]
    assert "e" in slept[eye_row_base + 1]
    mouth_row = next(i for i, row in enumerate(base) if "mmm" in row)
    assert slept[mouth_row].count("m") == 1  # three-wide mouth collapses to a dot


def test_work_pose_adds_brow_ink_above_eyes() -> None:
    base = SPRITES["claude"]
    working = pose_grid("claude", "work")
    eye_row = next(i for i, row in enumerate(base) if "e" in row)
    eye_cols = [x for x, c in enumerate(base[eye_row]) if c == "e"]
    for x in eye_cols:
        assert working[eye_row - 1][x] == "o"


def test_cheer_pose_opens_mouth_below_run() -> None:
    base = SPRITES["gpt"]
    cheering = pose_grid("gpt", "cheer")
    mouth_row = next(i for i, row in enumerate(base) if "mmm" in row)
    assert "m" in cheering[mouth_row + 1]
    assert "m" not in base[mouth_row + 1]


def test_llama_cheer_uses_snout_fallback() -> None:
    cheering = pose_grid("llama", "cheer")
    base = SPRITES["llama"]
    changed = [i for i, (a, b) in enumerate(zip(base, cheering)) if a != b]
    assert changed  # the fallback produced an open mouth somewhere on the snout


def test_walk_pose_spreads_feet() -> None:
    base = SPRITES["claude"]
    walking = pose_grid("claude", "walk")
    base_feet = [x for x, c in enumerate(base[-1]) if c == "o"]
    walk_feet = [x for x, c in enumerate(walking[-1]) if c == "o"]
    assert walk_feet == [base_feet[0] - 1, base_feet[1] + 1]


def test_sprite_surface_size_cache_and_ink_pixels() -> None:
    surface = sprite_surface("gpt", scale=3)
    width, height = sprite_size("gpt", scale=3)
    assert surface.get_size() == (width, height)
    assert sprite_surface("gpt", scale=3) is surface  # cache hit returns same object
    tinted = sprite_surface("gpt", tint=(1, 2, 3), scale=3)
    assert tinted is not surface
    ink = MODES["night"]["ink"]
    top_antenna_x = SPRITES["gpt"][1].index("o")
    pixel = surface.get_at((top_antenna_x * 3 + 1, 1 * 3 + 1))
    assert (pixel.r, pixel.g, pixel.b) == ink
