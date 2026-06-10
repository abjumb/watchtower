from __future__ import annotations

import base64
import os
from dataclasses import dataclass


@dataclass(slots=True)
class AuthConfig:
    api_base_url: str = ""
    oauth_token: str = ""
    username: str = ""
    password: str = ""

    @classmethod
    def from_env(cls) -> AuthConfig:
        return cls(
            api_base_url=os.getenv("WATCHTOWER_API_BASE_URL", "").strip(),
            oauth_token=os.getenv("WATCHTOWER_OAUTH_TOKEN", "").strip(),
            username=os.getenv("WATCHTOWER_USERNAME", "").strip(),
            password=os.getenv("WATCHTOWER_PASSWORD", "").strip(),
        )

    @property
    def mode(self) -> str:
        if self.oauth_token:
            return "oauth"
        if self.username and self.password:
            return "login"
        return "demo"

    @property
    def is_remote_enabled(self) -> bool:
        return bool(self.api_base_url and self.mode != "demo")

    def headers(self) -> dict[str, str]:
        if self.oauth_token:
            return {"Authorization": f"Bearer {self.oauth_token}"}
        if self.username and self.password:
            raw = f"{self.username}:{self.password}".encode("utf-8")
            encoded = base64.b64encode(raw).decode("ascii")
            return {"Authorization": f"Basic {encoded}"}
        return {}

    def with_token(self, token: str) -> AuthConfig:
        return AuthConfig(
            api_base_url=self.api_base_url,
            oauth_token=token.strip(),
        )

    def with_login(self, username: str, password: str) -> AuthConfig:
        return AuthConfig(
            api_base_url=self.api_base_url,
            username=username.strip(),
            password=password.strip(),
        )

    def with_endpoint(self, api_base_url: str) -> AuthConfig:
        return AuthConfig(
            api_base_url=api_base_url.strip().rstrip("/"),
            oauth_token=self.oauth_token,
            username=self.username,
            password=self.password,
        )
