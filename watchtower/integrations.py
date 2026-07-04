"""External service integrations for Watchtower (Todoist + GitHub).

Pure I/O layer in the spirit of model_api.py: raw httpx, no SDKs, no pygame,
no simulation imports. ui.py composes these into the app; tests exercise the
clients with mocked transports so no network is touched.

- Todoist: two-way — open tasks in a configured project become todo cards;
  completing the Watchtower task closes the Todoist task with the result as a
  comment.
- GitHub: open issues in configured repos become todo cards; completion posts
  the result back as an issue comment. Repos can be shallow-cloned locally so
  an agent prompt can carry repo context (file tree + README head).
"""
from __future__ import annotations

import os
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path

import httpx

TODOIST_API = "https://api.todoist.com/rest/v2"
GITHUB_API = "https://api.github.com"
REPOS_DIR = Path.home() / ".watchtower" / "repos"
_COMMENT_LIMIT = 12000


@dataclass(slots=True)
class ExternalTask:
    """A task pulled from an external service, ready to become a todo card."""

    source: str  # "todoist" | "github"
    external_id: str  # todoist task id, or "owner/repo#123"
    title: str
    body: str = ""
    url: str = ""

    @property
    def prompt(self) -> str:
        text = self.title if not self.body else f"{self.title}\n\n{self.body}"
        return f"[{self.source}] {text}"


@dataclass(slots=True)
class IntegrationConfig:
    """Tokens and scoping for the integration clients.

    Tokens live in memory/env only — never persisted to the autosave file,
    mirroring how ModelApiConfig treats provider keys.
    """

    todoist_token: str | None = None
    github_token: str | None = None
    todoist_project: str = "Inbox"
    github_repos: list[str] = field(default_factory=list)

    @classmethod
    def from_env(cls) -> "IntegrationConfig":
        return cls(
            todoist_token=os.getenv("TODOIST_API_TOKEN"),
            github_token=os.getenv("GITHUB_TOKEN"),
        )

    def set_key(self, provider: str, value: str) -> bool:
        cleaned = value.strip() or None
        if provider == "todoist":
            self.todoist_token = cleaned
            return True
        if provider == "github":
            self.github_token = cleaned
            return True
        return False


def completion_comment(title: str, response: str | None) -> str:
    body = (response or "").strip() or "(no model response recorded)"
    text = f"Completed by Watchtower: {title}\n\n{body}"
    return text[:_COMMENT_LIMIT]


class TodoistClient:
    """Minimal Todoist REST v2 client (tasks in, close+comment out)."""

    def __init__(self, config: IntegrationConfig, transport: httpx.BaseTransport | None = None) -> None:
        self.config = config
        self._transport = transport

    @property
    def enabled(self) -> bool:
        return bool(self.config.todoist_token)

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=TODOIST_API,
            headers={"Authorization": f"Bearer {self.config.todoist_token}"},
            transport=self._transport,
            timeout=15.0,
        )

    def _project_id(self, client: httpx.Client) -> str | None:
        if not self.config.todoist_project:
            return None
        response = client.get("/projects")
        response.raise_for_status()
        for project in response.json():
            if project.get("name") == self.config.todoist_project:
                return str(project.get("id"))
        return None

    def fetch_open_tasks(self) -> list[ExternalTask]:
        if not self.enabled:
            return []
        with self._client() as client:
            params: dict[str, str] = {}
            project_id = self._project_id(client)
            if project_id:
                params["project_id"] = project_id
            response = client.get("/tasks", params=params)
            response.raise_for_status()
            tasks = response.json()
        return [
            ExternalTask(
                source="todoist",
                external_id=str(task["id"]),
                title=task.get("content", "").strip() or "(untitled)",
                body=task.get("description", ""),
                url=task.get("url", ""),
            )
            for task in tasks
        ]

    def complete_task(self, external_id: str, comment: str | None = None) -> None:
        if not self.enabled:
            return
        with self._client() as client:
            if comment:
                response = client.post("/comments", json={"task_id": external_id, "content": comment[:_COMMENT_LIMIT]})
                response.raise_for_status()
            response = client.post(f"/tasks/{external_id}/close")
            response.raise_for_status()


class GitHubClient:
    """Minimal GitHub REST client (issues in, comments out, shallow clones)."""

    def __init__(self, config: IntegrationConfig, transport: httpx.BaseTransport | None = None) -> None:
        self.config = config
        self._transport = transport

    @property
    def enabled(self) -> bool:
        return bool(self.config.github_token)

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=GITHUB_API,
            headers={
                "Authorization": f"Bearer {self.config.github_token}",
                "Accept": "application/vnd.github+json",
            },
            transport=self._transport,
            timeout=15.0,
        )

    def fetch_open_issues(self, per_repo: int = 20) -> list[ExternalTask]:
        if not self.enabled or not self.config.github_repos:
            return []
        found: list[ExternalTask] = []
        with self._client() as client:
            for repo in self.config.github_repos:
                response = client.get(f"/repos/{repo}/issues", params={"state": "open", "per_page": per_repo})
                response.raise_for_status()
                for issue in response.json():
                    if "pull_request" in issue:
                        continue
                    found.append(
                        ExternalTask(
                            source="github",
                            external_id=f"{repo}#{issue['number']}",
                            title=issue.get("title", "").strip() or "(untitled)",
                            body=issue.get("body") or "",
                            url=issue.get("html_url", ""),
                        )
                    )
        return found

    def comment_issue(self, external_id: str, body: str) -> None:
        if not self.enabled:
            return
        repo, _, number = external_id.partition("#")
        if not number:
            raise ValueError(f"Not a github issue id: {external_id!r}")
        with self._client() as client:
            response = client.post(f"/repos/{repo}/issues/{number}/comments", json={"body": body[:_COMMENT_LIMIT]})
            response.raise_for_status()

    def clone_repo(self, repo: str, dest_root: Path = REPOS_DIR, runner=subprocess.run) -> Path:
        """Shallow-clone a public repo for prompt context; reuse if present.

        The token is deliberately NOT embedded in the clone URL — it would be
        persisted into .git/config. Private-repo cloning is a later item.
        """
        dest = Path(dest_root) / repo.replace("/", "--")
        if dest.exists():
            return dest
        dest.parent.mkdir(parents=True, exist_ok=True)
        result = runner(
            ["git", "clone", "--depth", "1", f"https://github.com/{repo}.git", str(dest)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"git clone failed for {repo}: {result.stderr.strip()[:300]}")
        return dest


class IntegrationPoller:
    """Background fetcher for external tasks (TelemetryPoller pattern).

    A daemon thread polls both services every ``interval`` seconds — or sooner
    when poke()d after a config change — and swaps the latest snapshot behind
    a lock; the UI thread reads via latest(). Failures degrade to the previous
    snapshot with last_error set instead of crashing the thread.
    """

    def __init__(self, todoist: TodoistClient, github: GitHubClient, interval: float = 60.0) -> None:
        self.todoist = todoist
        self.github = github
        self.interval = interval
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None
        self._tasks: list[ExternalTask] = []
        self._error: str | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stopping.clear()
        self._thread = threading.Thread(target=self._run, name="integration-poller", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stopping.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=2.0)

    def poke(self) -> None:
        """Request an early poll (after a token/scope change)."""
        self._wake.set()

    def latest(self) -> tuple[list[ExternalTask], str | None]:
        with self._lock:
            return list(self._tasks), self._error

    def _run(self) -> None:
        while not self._stopping.is_set():
            self._cycle()
            self._wake.wait(self.interval)
            self._wake.clear()

    def _cycle(self) -> None:
        found: list[ExternalTask] = []
        error: str | None = None
        for fetch in (self.todoist.fetch_open_tasks, self.github.fetch_open_issues):
            try:
                found.extend(fetch())
            except Exception as exc:  # degrade gracefully; the UI surfaces this
                error = f"integration error: {exc}"[:120]
        with self._lock:
            if found or error is None:
                self._tasks = found
            self._error = error


def repo_context(dest: Path, max_entries: int = 200, readme_chars: int = 2000) -> str:
    """A compact prompt-context block for a cloned repo: file tree + README head."""
    dest = Path(dest)
    entries: list[str] = []
    for path in sorted(dest.rglob("*")):
        relative = path.relative_to(dest)
        if ".git" in relative.parts:
            continue
        if path.is_file():
            entries.append(str(relative))
        if len(entries) >= max_entries:
            entries.append("... (truncated)")
            break
    readme = ""
    for name in ("README.md", "README.rst", "README.txt", "README"):
        candidate = dest / name
        if candidate.is_file():
            readme = candidate.read_text(errors="replace")[:readme_chars]
            break
    parts = [f"Repository files ({dest.name}):", *entries]
    if readme:
        parts += ["", "README (head):", readme]
    return "\n".join(parts)
