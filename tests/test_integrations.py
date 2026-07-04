import json
from pathlib import Path

import httpx

from watchtower.integrations import (
    ExternalTask,
    GitHubClient,
    IntegrationConfig,
    TodoistClient,
    completion_comment,
    repo_context,
)


def _transport(handler):
    return httpx.MockTransport(handler)


def test_config_set_key_and_from_env(monkeypatch) -> None:
    config = IntegrationConfig()
    assert config.set_key("todoist", " tok-a ") and config.todoist_token == "tok-a"
    assert config.set_key("github", "tok-b") and config.github_token == "tok-b"
    assert not config.set_key("gitlab", "x")
    assert config.set_key("todoist", "  ") and config.todoist_token is None

    monkeypatch.setenv("TODOIST_API_TOKEN", "env-t")
    monkeypatch.setenv("GITHUB_TOKEN", "env-g")
    env_config = IntegrationConfig.from_env()
    assert env_config.todoist_token == "env-t" and env_config.github_token == "env-g"


def test_disabled_clients_are_inert() -> None:
    config = IntegrationConfig()
    assert TodoistClient(config).fetch_open_tasks() == []
    assert GitHubClient(config).fetch_open_issues() == []
    TodoistClient(config).complete_task("1", "done")  # no token: must not raise


def test_todoist_fetch_filters_by_project() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.path == "/rest/v2/projects":
            return httpx.Response(200, json=[{"id": 9, "name": "Inbox"}, {"id": 7, "name": "Work"}])
        assert request.url.params["project_id"] == "9"
        return httpx.Response(200, json=[
            {"id": 101, "content": "water plants", "description": "the ferns", "url": "https://todoist/101"},
        ])

    client = TodoistClient(IntegrationConfig(todoist_token="tok"), transport=_transport(handler))
    tasks = client.fetch_open_tasks()
    assert tasks == [ExternalTask("todoist", "101", "water plants", "the ferns", "https://todoist/101")]
    assert tasks[0].prompt.startswith("[todoist] water plants")
    assert len(calls) == 2


def test_todoist_complete_comments_then_closes() -> None:
    recorded: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        recorded.append((request.method, request.url.path))
        if request.url.path == "/rest/v2/comments":
            assert json.loads(request.content) == {"task_id": "101", "content": "result text"}
        return httpx.Response(204)

    client = TodoistClient(IntegrationConfig(todoist_token="tok"), transport=_transport(handler))
    client.complete_task("101", "result text")
    assert recorded == [("POST", "/rest/v2/comments"), ("POST", "/rest/v2/tasks/101/close")]


def test_github_fetch_maps_issues_and_skips_prs() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/repos/acme/tool/issues"
        assert request.headers["Authorization"] == "Bearer tok"
        return httpx.Response(200, json=[
            {"number": 5, "title": "fix crash", "body": "boom", "html_url": "https://gh/5"},
            {"number": 6, "title": "a pr", "body": "", "html_url": "https://gh/6", "pull_request": {}},
        ])

    config = IntegrationConfig(github_token="tok", github_repos=["acme/tool"])
    issues = GitHubClient(config, transport=_transport(handler)).fetch_open_issues()
    assert issues == [ExternalTask("github", "acme/tool#5", "fix crash", "boom", "https://gh/5")]


def test_github_comment_posts_to_issue() -> None:
    recorded: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        recorded.append(f"{request.method} {request.url.path}")
        assert json.loads(request.content) == {"body": "the result"}
        return httpx.Response(201)

    config = IntegrationConfig(github_token="tok")
    GitHubClient(config, transport=_transport(handler)).comment_issue("acme/tool#5", "the result")
    assert recorded == ["POST /repos/acme/tool/issues/5/comments"]


def test_github_clone_runs_git_once_and_reuses(tmp_path: Path) -> None:
    commands: list[list[str]] = []

    def runner(cmd, capture_output, text):
        commands.append(cmd)
        Path(cmd[-1]).mkdir(parents=True)

        class Result:
            returncode = 0
            stderr = ""

        return Result()

    client = GitHubClient(IntegrationConfig(github_token="tok"))
    dest = client.clone_repo("acme/tool", dest_root=tmp_path, runner=runner)
    assert dest == tmp_path / "acme--tool"
    assert commands[0][:4] == ["git", "clone", "--depth", "1"]
    assert "tok" not in " ".join(commands[0])  # token must never reach the clone URL
    again = client.clone_repo("acme/tool", dest_root=tmp_path, runner=runner)
    assert again == dest and len(commands) == 1  # existing clone is reused


def test_repo_context_lists_files_and_readme(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print('hi')")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("secret")
    (tmp_path / "README.md").write_text("# Tool\nDoes things.")

    context = repo_context(tmp_path)
    assert "src/main.py" in context
    assert ".git" not in context
    assert "# Tool" in context


def test_completion_comment_shapes_and_truncates() -> None:
    assert completion_comment("t", None).endswith("(no model response recorded)")
    long = completion_comment("t", "x" * 20000)
    assert len(long) <= 12000


def test_ui_inbound_sync_and_commands(monkeypatch) -> None:
    import os

    os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
    os.environ["WATCHTOWER_NO_AUTOSAVE"] = "1"
    import pygame

    from watchtower.models import TaskStatus
    from watchtower.ui import WatchtowerApp

    app = WatchtowerApp(web_mode=True)
    try:
        external = ExternalTask("todoist", "42", "buy milk", "", "")
        monkeypatch.setattr(app.integration_poller, "latest", lambda: ([external], None))
        app._sync_external_tasks()
        app._sync_external_tasks()  # second pass must not duplicate the card
        todos = [t for t in app.simulation.tasks.values() if t.status is TaskStatus.TODO]
        assert len(todos) == 1
        assert todos[0].prompt.startswith("[todoist] buy milk")
        assert app.external_links[todos[0].id] == ("todoist", "42")

        assert app._handle_command("/key todoist tok-x")
        assert app.integration_config.todoist_token == "tok-x"
        assert app._handle_command("/todoist Work")
        assert app.integration_config.todoist_project == "Work"
        assert app._handle_command("/github repo add acme/tool")
        assert app.integration_config.github_repos == ["acme/tool"]
        assert app._handle_command("/github repo remove acme/tool")
        assert app.integration_config.github_repos == []
        assert app._handle_command("/key openai sk-test")  # model keys still route through
        assert app.flash_message == "openai key set"
    finally:
        app.poller.stop()
        app.integration_poller.stop()
        pygame.quit()


def test_ui_outbound_completion_and_repo_context(monkeypatch, tmp_path: Path) -> None:
    import os

    os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
    os.environ["WATCHTOWER_NO_AUTOSAVE"] = "1"
    import pygame

    from watchtower.ui import WatchtowerApp

    app = WatchtowerApp(web_mode=True)
    try:
        task = app.simulation.submit_task("fix crash", requested_agent_id="gpt")
        app.external_links[task.id] = ("github", "acme/tool#5")

        dispatched: list[tuple] = []
        monkeypatch.setattr(app, "_dispatch_external_result", lambda *args: dispatched.append(args))
        app._sync_external_completions()
        assert dispatched == []  # not complete yet
        task.assign_to("gpt")
        task.mark_progress(1.0)
        app._sync_external_completions()
        app._sync_external_completions()  # second pass must not re-push
        assert len(dispatched) == 1
        assert dispatched[0][:2] == ("github", "acme/tool#5")

        recorded: dict[str, tuple] = {}
        monkeypatch.setattr(app.todoist, "complete_task", lambda eid, c: recorded.setdefault("todoist", (eid, c)))
        monkeypatch.setattr(app.github, "comment_issue", lambda eid, b: recorded.setdefault("github", (eid, b)))
        app._push_external_result("todoist", "42", "title", "resp")
        app._push_external_result("github", "acme/tool#5", "title", None)
        assert recorded["todoist"][0] == "42" and "resp" in recorded["todoist"][1]
        assert recorded["github"][0] == "acme/tool#5"
        assert not app._integration_messages.empty()

        clone = tmp_path / "acme--tool"
        clone.mkdir()
        (clone / "main.py").write_text("x = 1")
        monkeypatch.setattr("watchtower.ui.REPOS_DIR", tmp_path)
        assert "main.py" in app._task_model_prompt(task)
        plain = app.simulation.submit_task("no link")
        assert app._task_model_prompt(plain) == plain.prompt
    finally:
        app.poller.stop()
        app.integration_poller.stop()
        pygame.quit()
