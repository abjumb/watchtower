"""A tiny in-engine widget toolkit drawn directly with pygame.

Keeps Watchtower's playful, hand-drawn look (no native toolkit) while giving it
real GUI affordances: clickable buttons, a focusable text field with a caret,
toggles, and dropdown menus. Widgets are theme-aware and own their hit-testing.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import pygame


def _mix(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    t = max(0.0, min(1.0, t))
    return tuple(int(x + (y - x) * t) for x, y in zip(a, b))


_ALPHA_CACHE: dict[tuple, pygame.Surface] = {}
_ALPHA_CACHE_MAX = 64


def rounded_alpha_surface(
    size: tuple[int, int],
    rect: tuple[int, int, int, int],
    radius: int,
    rgba: tuple[int, int, int, int],
) -> pygame.Surface:
    """Cached SRCALPHA surface with one rounded rect rasterized onto it.

    Shadow/glow bitmaps depend only on geometry + color, so render each distinct
    one once and blit it many times. Callers must treat the result as immutable.
    The distinct-key set is small and layout-stable; on overflow the cache is
    simply cleared and rebuilt within a frame.
    """
    key = (*size, *rect, radius, rgba)
    surface = _ALPHA_CACHE.get(key)
    if surface is None:
        if len(_ALPHA_CACHE) >= _ALPHA_CACHE_MAX:
            _ALPHA_CACHE.clear()
        surface = pygame.Surface(size, pygame.SRCALPHA)
        pygame.draw.rect(surface, rgba, pygame.Rect(rect), border_radius=radius)
        _ALPHA_CACHE[key] = surface
    return surface


_LABEL_CACHE: dict[tuple, pygame.Surface] = {}
_LABEL_CACHE_MAX = 256


def _render_label(font, text: str, color: tuple[int, int, int]) -> pygame.Surface:
    """Cached font.render for widget labels (constant strings, per-frame draws)."""
    key = (font, text, color)
    surface = _LABEL_CACHE.get(key)
    if surface is None:
        if len(_LABEL_CACHE) >= _LABEL_CACHE_MAX:
            _LABEL_CACHE.clear()
        surface = font.render(text, True, color)
        _LABEL_CACHE[key] = surface
    return surface


def _liquid_rect(screen, rect: pygame.Rect, fill, border, radius: int = 8, shadow: bool = True) -> None:
    if shadow:
        # Offset "stamp" shadow to match the paper-board design language.
        shadow_surface = rounded_alpha_surface((rect.width, rect.height), (0, 0, rect.width, rect.height), radius, (0, 0, 0, 55))
        screen.blit(shadow_surface, (rect.x + 2, rect.y + 2))
    pygame.draw.rect(screen, fill, rect, border_radius=radius)
    highlight = _mix(fill, (255, 255, 255), 0.12)
    pygame.draw.line(screen, highlight, (rect.x + radius, rect.y + 1), (rect.right - radius, rect.y + 1), 1)
    pygame.draw.rect(screen, border, rect, width=1, border_radius=radius)


@dataclass(slots=True)
class Button:
    label: str
    rect: pygame.Rect
    on_click: Callable[[], None]
    style: str = "primary"  # primary | ghost | danger
    enabled: bool = True

    def handle_click(self, pos: tuple[int, int]) -> bool:
        if self.enabled and self.rect.collidepoint(pos):
            self.on_click()
            return True
        return False

    def draw(self, screen, theme, font, mouse: tuple[int, int]) -> None:
        hover = self.rect.collidepoint(mouse)
        if not self.enabled:
            bg, fg, border = theme.surface_alt, theme.muted, theme.grid
        elif self.style == "ghost":
            bg = _mix(theme.surface_alt, theme.text, 0.10) if hover else _mix(theme.surface_alt, theme.surface, 0.35)
            fg = theme.text
            border = _mix(theme.grid, theme.text, 0.16) if hover else theme.grid
        else:
            base = theme.danger if self.style == "danger" else theme.accent
            bg = _mix(base, theme.text, 0.10) if hover else base
            fg = theme.bg
            border = _mix(base, theme.text, 0.25)
        _liquid_rect(screen, self.rect, bg, border, radius=8)
        label = _render_label(font, self.label, fg)
        screen.blit(label, label.get_rect(center=self.rect.center))


@dataclass(slots=True)
class TextInput:
    rect: pygame.Rect
    value: str = ""
    placeholder: str = ""
    focused: bool = False
    caret: int = 0
    max_len: int = 400
    # Draw cache: the caret-scroll loops cost O(len) font.size calls per frame,
    # so scroll/surface/caret-x are recomputed only when this key changes.
    _draw_cache_key: tuple | None = field(default=None, init=False, repr=False)
    _draw_surface: pygame.Surface | None = field(default=None, init=False, repr=False)
    _draw_caret_x: int = field(default=0, init=False, repr=False)

    def set(self, text: str) -> None:
        self.value = text[: self.max_len]
        self.caret = len(self.value)

    def handle_click(self, pos: tuple[int, int]) -> bool:
        if self.rect.collidepoint(pos):
            self.focused = True
            self.caret = len(self.value)
            return True
        return False

    def handle_key(self, event) -> str | None:
        """Edit the field. Returns "submit" when Enter is pressed, else None."""
        key = event.key
        if key in (pygame.K_RETURN, pygame.K_KP_ENTER):
            return "submit"
        if key == pygame.K_BACKSPACE:
            if self.caret > 0:
                self.value = self.value[: self.caret - 1] + self.value[self.caret :]
                self.caret -= 1
            return None
        if key == pygame.K_DELETE:
            if self.caret < len(self.value):
                self.value = self.value[: self.caret] + self.value[self.caret + 1 :]
            return None
        if key == pygame.K_LEFT:
            self.caret = max(0, self.caret - 1)
            return None
        if key == pygame.K_RIGHT:
            self.caret = min(len(self.value), self.caret + 1)
            return None
        if key == pygame.K_HOME:
            self.caret = 0
            return None
        if key == pygame.K_END:
            self.caret = len(self.value)
            return None
        char = event.unicode
        if char and char.isprintable() and len(self.value) < self.max_len:
            self.value = self.value[: self.caret] + char + self.value[self.caret :]
            self.caret += len(char)
        return None

    def draw(self, screen, theme, font, blink: bool) -> None:
        fill = _mix(theme.surface_alt, theme.bg, 0.22)
        border = theme.accent if self.focused else _mix(theme.grid, theme.text, 0.06)
        _liquid_rect(screen, self.rect, fill, border, radius=10)
        inner_w = self.rect.width - 20
        ty = self.rect.y + (self.rect.height - font.get_height()) // 2
        tx = self.rect.x + 10
        if not self.value:
            key = ("placeholder", font, self.placeholder, theme.name)
            if self._draw_cache_key != key:
                self._draw_surface = font.render(self.placeholder, True, theme.muted)
                self._draw_cache_key = key
            screen.blit(self._draw_surface, (tx, ty))
            if self.focused and blink:
                pygame.draw.line(screen, theme.text, (tx, ty + 2), (tx, ty + font.get_height() - 2), 1)
            return
        key = ("value", font, self.value, self.caret, inner_w, theme.name)
        if self._draw_cache_key != key:
            # Scroll the text so the caret stays visible inside the field.
            start = 0
            while start < self.caret and font.size(self.value[start : self.caret])[0] > inner_w:
                start += 1
            display = self.value[start:]
            while display and font.size(display)[0] > inner_w:
                display = display[:-1]
            self._draw_surface = font.render(display, True, theme.text)
            self._draw_caret_x = font.size(self.value[start : self.caret])[0]
            self._draw_cache_key = key
        screen.blit(self._draw_surface, (tx, ty))
        if self.focused and blink:
            caret_x = tx + self._draw_caret_x
            pygame.draw.line(screen, theme.text, (caret_x, ty + 2), (caret_x, ty + font.get_height() - 2), 1)


@dataclass(slots=True)
class Toggle:
    rect: pygame.Rect
    label: str
    value: bool
    on_change: Callable[[bool], None]

    def handle_click(self, pos: tuple[int, int]) -> bool:
        if self.rect.collidepoint(pos):
            self.value = not self.value
            self.on_change(self.value)
            return True
        return False

    def draw(self, screen, theme, font, mouse: tuple[int, int]) -> None:
        track = pygame.Rect(self.rect.x, self.rect.y + 2, 38, 18)
        on = self.value
        fill = theme.accent if on else _mix(theme.surface_alt, theme.bg, 0.18)
        _liquid_rect(screen, track, fill, theme.grid, radius=9, shadow=False)
        knob_x = track.right - 9 if on else track.x + 9
        knob = theme.bg if on else _mix(theme.text, theme.muted, 0.40)
        pygame.draw.circle(screen, knob, (knob_x, track.centery), 7)
        label = _render_label(font, self.label, theme.text)
        screen.blit(label, (track.right + 10, self.rect.y + (self.rect.height - font.get_height()) // 2))


@dataclass(slots=True)
class Dropdown:
    label: str
    rect: pygame.Rect
    items: list[tuple[str, Callable[[], None]]] = field(default_factory=list)
    open: bool = False
    item_height: int = 26
    menu_width: int = 190

    def item_rects(self) -> list[pygame.Rect]:
        # Opens upward (the toolbar sits near the bottom of the window).
        rects = []
        for index in range(len(self.items)):
            top = self.rect.y - 4 - (index + 1) * self.item_height
            rects.append(pygame.Rect(self.rect.x, top, self.menu_width, self.item_height))
        return rects

    def handle_click(self, pos: tuple[int, int]) -> bool:
        if self.rect.collidepoint(pos):
            self.open = not self.open
            return True
        if self.open:
            for rect, (_, action) in zip(self.item_rects(), self.items):
                if rect.collidepoint(pos):
                    self.open = False
                    action()
                    return True
            self.open = False
        return False

    def draw_button(self, screen, theme, font, mouse: tuple[int, int]) -> None:
        hover = self.rect.collidepoint(mouse) or self.open
        bg = _mix(theme.surface_alt, theme.text, 0.10) if hover else _mix(theme.surface_alt, theme.surface, 0.35)
        _liquid_rect(screen, self.rect, bg, theme.grid, radius=8)
        label = _render_label(font, f"☰ {self.label}", theme.text)
        screen.blit(label, label.get_rect(center=self.rect.center))

    def draw_items(self, screen, theme, font, mouse: tuple[int, int]) -> None:
        if not self.open:
            return
        for rect, (label, _) in zip(self.item_rects(), self.items):
            hover = rect.collidepoint(mouse)
            fill = _mix(theme.surface, theme.accent, 0.18) if hover else _mix(theme.surface, theme.bg, 0.10)
            _liquid_rect(screen, rect, fill, theme.grid, radius=6, shadow=False)
            text = _render_label(font, label, theme.text)
            screen.blit(text, (rect.x + 10, rect.y + (rect.height - font.get_height()) // 2))
