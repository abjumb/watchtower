import os

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

import pygame

from watchtower.widgets import Button, Dropdown, TextInput, Toggle


def _key(key: int, unicode: str = "") -> pygame.event.Event:
    return pygame.event.Event(pygame.KEYDOWN, key=key, unicode=unicode, mod=0)


def test_text_input_typing_caret_and_submit() -> None:
    field = TextInput(pygame.Rect(0, 0, 200, 30), focused=True)
    for char in "hi":
        field.handle_key(_key(pygame.K_a, char))
    assert field.value == "hi"
    assert field.caret == 2
    field.handle_key(_key(pygame.K_LEFT))
    assert field.caret == 1
    field.handle_key(_key(pygame.K_a, "X"))
    assert field.value == "hXi"
    field.handle_key(_key(pygame.K_BACKSPACE))
    assert field.value == "hi"
    assert field.handle_key(_key(pygame.K_RETURN)) == "submit"


def test_button_click_respects_bounds_and_enabled() -> None:
    calls: list[int] = []
    button = Button("x", pygame.Rect(0, 0, 50, 20), lambda: calls.append(1))
    assert not button.handle_click((100, 100))
    assert button.handle_click((10, 10))
    assert calls == [1]
    button.enabled = False
    assert not button.handle_click((10, 10))


def test_toggle_flips_and_reports() -> None:
    seen: list[bool] = []
    toggle = Toggle(pygame.Rect(0, 0, 120, 22), "x", False, seen.append)
    assert toggle.handle_click((10, 10))
    assert toggle.value is True
    assert seen == [True]


def test_dropdown_opens_and_selects_item() -> None:
    chosen: list[str] = []
    menu = Dropdown(
        "Menu",
        pygame.Rect(0, 200, 120, 26),
        items=[("A", lambda: chosen.append("A")), ("B", lambda: chosen.append("B"))],
    )
    assert menu.handle_click((10, 210))  # click the button -> open
    assert menu.open
    rects = menu.item_rects()
    assert menu.handle_click(rects[1].center)  # pick the second item
    assert chosen == ["B"]
    assert not menu.open
