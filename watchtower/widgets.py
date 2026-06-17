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
            bg, fg = theme.surface_alt, theme.muted
        elif self.style == "ghost":
            bg = _mix(theme.surface_alt, theme.text, 0.10) if hover else theme.surface_alt
            fg = theme.text
        else:
            base = theme.danger if self.style == "danger" else theme.accent
            bg = _mix(base, theme.text, 0.12) if hover else base
            fg = theme.bg
        pygame.draw.rect(screen, bg, self.rect, border_radius=6)
        if self.style == "ghost":
            pygame.draw.rect(screen, theme.grid, self.rect, width=1, border_radius=6)
        label = font.render(self.label, True, fg)
        screen.blit(label, label.get_rect(center=self.rect.center))


@dataclass(slots=True)
class TextInput:
    rect: pygame.Rect
    value: str = ""
    placeholder: str = ""
    focused: bool = False
    caret: int = 0
    max_len: int = 400

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
        pygame.draw.rect(screen, theme.surface, self.rect, border_radius=8)
        border = theme.accent if self.focused else theme.grid
        pygame.draw.rect(screen, border, self.rect, width=1, border_radius=8)
        inner_w = self.rect.width - 20
        ty = self.rect.y + (self.rect.height - font.get_height()) // 2
        tx = self.rect.x + 10
        if not self.value:
            placeholder = font.render(self.placeholder, True, theme.muted)
            screen.blit(placeholder, (tx, ty))
            if self.focused and blink:
                pygame.draw.line(screen, theme.text, (tx, ty + 2), (tx, ty + font.get_height() - 2), 1)
            return
        # Scroll the text so the caret stays visible inside the field.
        start = 0
        while start < self.caret and font.size(self.value[start : self.caret])[0] > inner_w:
            start += 1
        display = self.value[start:]
        while display and font.size(display)[0] > inner_w:
            display = display[:-1]
        screen.blit(font.render(display, True, theme.text), (tx, ty))
        if self.focused and blink:
            caret_x = tx + font.size(self.value[start : self.caret])[0]
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
        pygame.draw.rect(screen, theme.accent if on else theme.surface_alt, track, border_radius=9)
        knob_x = track.right - 9 if on else track.x + 9
        pygame.draw.circle(screen, theme.bg if on else theme.muted, (knob_x, track.centery), 7)
        label = font.render(self.label, True, theme.text)
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
        bg = _mix(theme.surface_alt, theme.text, 0.10) if hover else theme.surface_alt
        pygame.draw.rect(screen, bg, self.rect, border_radius=6)
        pygame.draw.rect(screen, theme.grid, self.rect, width=1, border_radius=6)
        label = font.render(f"☰ {self.label}", True, theme.text)
        screen.blit(label, label.get_rect(center=self.rect.center))

    def draw_items(self, screen, theme, font, mouse: tuple[int, int]) -> None:
        if not self.open:
            return
        for rect, (label, _) in zip(self.item_rects(), self.items):
            hover = rect.collidepoint(mouse)
            pygame.draw.rect(screen, _mix(theme.surface, theme.accent, 0.18) if hover else theme.surface, rect, border_radius=4)
            pygame.draw.rect(screen, theme.grid, rect, width=1, border_radius=4)
            text = font.render(label, True, theme.text)
            screen.blit(text, (rect.x + 10, rect.y + (rect.height - font.get_height()) // 2))
