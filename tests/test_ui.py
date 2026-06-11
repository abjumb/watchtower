import os

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

import pygame

from watchtower.ui import WatchtowerApp


def test_number_key_without_modifier_is_text_input() -> None:
    app = WatchtowerApp()
    try:
        event = pygame.event.Event(pygame.KEYDOWN, key=pygame.K_1, mod=0, unicode="1")

        assert not app._is_agent_shortcut(event)
    finally:
        app.poller.stop()
        pygame.quit()


def test_number_key_with_modifier_is_agent_shortcut() -> None:
    app = WatchtowerApp()
    try:
        event = pygame.event.Event(pygame.KEYDOWN, key=pygame.K_1, mod=pygame.KMOD_CTRL, unicode="1")

        assert app._is_agent_shortcut(event)
    finally:
        app.poller.stop()
        pygame.quit()
