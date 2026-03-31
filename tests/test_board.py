from __future__ import annotations

import io
import json
from pathlib import Path

from clawteam.board.collector import BoardCollector
from clawteam.board.server import BoardHandler
from clawteam.team.mailbox import MailboxManager
from clawteam.team.manager import TeamManager


class _FakeHTTPResponse:
    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return self._body


def _make_proxy_handler(path: str):
    handler = object.__new__(BoardHandler)
    handler.path = path
    handler.wfile = io.BytesIO()
    captured = {"status": None, "headers": [], "error": None}
    handler.send_response = lambda code: captured.__setitem__("status", code)
    handler.send_header = lambda name, value: captured["headers"].append((name, value))
    handler.end_headers = lambda: None
    handler.send_error = lambda code, message=None: captured.__setitem__("error", (code, message))
    handler._serve_static = lambda filename, content_type: None
    handler._serve_json = lambda data: None
    handler._serve_team = lambda team_name: None
    handler._serve_sse = lambda team_name: None
    return handler, captured


def test_collect_overview_does_not_call_collect_team(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("CLAWTEAM_DATA_DIR", str(tmp_path))
    TeamManager.create_team(
        name="demo",
        leader_name="leader",
        leader_id="leader001",
        description="demo team",
    )

    def fail_collect_team(self, team_name: str):
        raise AssertionError("collect_team should not be called for overview")

    monkeypatch.setattr(BoardCollector, "collect_team", fail_collect_team)

    teams = BoardCollector().collect_overview()

    assert teams == [
        {
            "name": "demo",
            "description": "demo team",
            "leader": "leader",
            "members": 1,
            "tasks": 0,
            "pendingMessages": 0,
        }
    ]


def test_collect_overview_sums_inbox_counts_for_all_members(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("CLAWTEAM_DATA_DIR", str(tmp_path))
    TeamManager.create_team(
        name="demo",
        leader_name="leader",
        leader_id="leader001",
        description="demo team",
    )
    TeamManager.add_member("demo", "worker", "worker001")
    MailboxManager("demo").send(from_agent="leader", to="worker", content="hello")

    def fail_collect_team(self, team_name: str):
        raise AssertionError("collect_team should not be called for overview")

    monkeypatch.setattr(BoardCollector, "collect_team", fail_collect_team)

    teams = BoardCollector().collect_overview()

    assert teams == [
        {
            "name": "demo",
            "description": "demo team",
            "leader": "leader",
            "members": 2,
            "tasks": 0,
            "pendingMessages": 1,
        }
    ]


def test_team_snapshot_cache_reuses_value_within_ttl():
    from clawteam.board.server import TeamSnapshotCache

    calls = {"count": 0}

    def loader():
        calls["count"] += 1
        return {"version": calls["count"]}

    cache = TeamSnapshotCache(ttl_seconds=60.0)

    first = cache.get("demo", loader)
    second = cache.get("demo", loader)

    assert first == {"version": 1}
    assert second == {"version": 1}
    assert calls["count"] == 1


def test_team_snapshot_cache_expires_after_ttl(monkeypatch):
    from clawteam.board.server import TeamSnapshotCache

    now = {"value": 100.0}
    monkeypatch.setattr("clawteam.board.server.time.monotonic", lambda: now["value"])

    calls = {"count": 0}

    def loader():
        calls["count"] += 1
        return {"version": calls["count"]}

    cache = TeamSnapshotCache(ttl_seconds=5.0)

    first = cache.get("demo", loader)
    now["value"] += 10.0
    second = cache.get("demo", loader)

    assert first == {"version": 1}
    assert second == {"version": 2}
    assert calls["count"] == 2


def test_collect_team_preserves_conflicts_field(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("CLAWTEAM_DATA_DIR", str(tmp_path))
    TeamManager.create_team(
        name="demo",
        leader_name="leader",
        leader_id="leader001",
        description="demo team",
    )

    data = BoardCollector().collect_team("demo")

    assert "conflicts" in data


def test_collect_team_exposes_member_inbox_identity(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("CLAWTEAM_DATA_DIR", str(tmp_path))
    TeamManager.create_team(
        name="demo",
        leader_name="leader",
        leader_id="leader001",
        description="demo team",
    )
    TeamManager.add_member("demo", "worker", "worker001", user="alice")

    data = BoardCollector().collect_team("demo")

    worker = next(member for member in data["members"] if member["name"] == "worker")
    assert worker["memberKey"] == "alice_worker"
    assert worker["inboxName"] == "alice_worker"


def test_collect_team_normalizes_message_participants(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("CLAWTEAM_DATA_DIR", str(tmp_path))
    TeamManager.create_team(
        name="demo",
        leader_name="leader",
        leader_id="leader001",
        description="demo team",
    )
    TeamManager.add_member("demo", "worker", "worker001", user="alice")
    mailbox = MailboxManager("demo")
    mailbox.send(from_agent="leader", to="worker", content="hello")
    mailbox.broadcast(from_agent="leader", content="broadcast")

    data = BoardCollector().collect_team("demo")

    direct = next(msg for msg in data["messages"] if msg.get("content") == "hello")
    assert direct["fromKey"] == "leader"
    assert direct["fromLabel"] == "leader"
    assert direct["toKey"] == "alice_worker"
    assert direct["toLabel"] == "worker"
    assert direct["isBroadcast"] is False

    broadcast = next(
        msg
        for msg in data["messages"]
        if msg.get("content") == "broadcast" and msg.get("to") == "alice_worker"
    )
    assert broadcast["fromKey"] == "leader"
    assert broadcast["toKey"] == "alice_worker"
    assert broadcast["toLabel"] == "worker"
    assert broadcast["isBroadcast"] is True


def test_collect_overview_preserves_broken_team_fallback(monkeypatch):
    def fake_discover():
        return [
            {
                "name": "good",
                "description": "good team",
                "memberCount": 1,
            },
            {
                "name": "broken",
                "description": "broken team",
                "memberCount": 7,
            },
        ]

    def fake_summary(self, team_name: str):
        if team_name == "broken":
            raise ValueError("boom")
        return {
            "name": "good",
            "description": "good team",
            "leader": "lead",
            "members": 1,
            "tasks": 3,
            "pendingMessages": 2,
        }

    monkeypatch.setattr(TeamManager, "discover_teams", staticmethod(fake_discover))
    monkeypatch.setattr(BoardCollector, "collect_team_summary", fake_summary)

    overview = BoardCollector().collect_overview()

    assert overview == [
        {
            "name": "good",
            "description": "good team",
            "leader": "lead",
            "members": 1,
            "tasks": 3,
            "pendingMessages": 2,
        },
        {
            "name": "broken",
            "description": "broken team",
            "leader": "",
            "members": 7,
            "tasks": 0,
            "pendingMessages": 0,
        },
    ]


def test_serve_team_reads_fresh_snapshot_without_cache(monkeypatch):
    calls = {"count": 0}
    served = {}

    class FakeCache:
        def get(self, team_name, loader):
            raise AssertionError("team cache should not be used for /api/team")

    handler = object.__new__(BoardHandler)
    handler.collector = type(
        "Collector",
        (),
        {
            "collect_team": staticmethod(
                lambda team_name: calls.__setitem__("count", calls["count"] + 1)
                or {"team": {"name": team_name}}
            )
        },
    )()
    handler.team_cache = FakeCache()
    handler._serve_json = lambda data: served.setdefault("data", data)

    handler._serve_team("demo")

    assert calls["count"] == 1
    assert served["data"] == {"team": {"name": "demo"}}


def test_serve_sse_uses_shared_team_snapshot_cache(monkeypatch):
    calls = {"count": 0}

    class FakeCache:
        def get(self, team_name, loader):
            calls["count"] += 1
            return loader()

    handler = object.__new__(BoardHandler)
    handler.collector = type(
        "Collector",
        (),
        {"collect_team": staticmethod(lambda team_name: {"team": {"name": team_name}})},
    )()
    handler.team_cache = FakeCache()
    handler.interval = 0.0
    handler.wfile = io.BytesIO()
    handler.send_response = lambda code: None
    handler.send_header = lambda name, value: None
    handler.end_headers = lambda: None
    monkeypatch.setattr(
        handler.wfile,
        "flush",
        lambda: (_ for _ in ()).throw(BrokenPipeError()),
    )

    handler._serve_sse("demo")

    assert calls["count"] == 1


def test_proxy_github_repo_path_fetches_readme_download_url(monkeypatch):
    handler, captured = _make_proxy_handler("/api/proxy?url=https://github.com/openai/openai-python")
    calls: list[tuple[str, str | None]] = []

    def fake_urlopen(req):
        calls.append((req.full_url, req.get_header("User-agent")))
        if req.full_url == "https://api.github.com/repos/openai/openai-python/readme":
            payload = json.dumps(
                {"download_url": "https://raw.githubusercontent.com/openai/openai-python/main/README.md"}
            ).encode()
            return _FakeHTTPResponse(payload)
        if req.full_url == "https://raw.githubusercontent.com/openai/openai-python/main/README.md":
            return _FakeHTTPResponse(b"# OpenAI Python")
        raise AssertionError(f"unexpected URL: {req.full_url}")

    monkeypatch.setattr("clawteam.board.server.urllib.request.urlopen", fake_urlopen)

    handler.do_GET()

    assert captured["error"] is None
    assert captured["status"] == 200
    assert handler.wfile.getvalue() == b"# OpenAI Python"
    assert calls == [
        ("https://api.github.com/repos/openai/openai-python/readme", "ClawTeam-Server"),
        ("https://raw.githubusercontent.com/openai/openai-python/main/README.md", "ClawTeam-Server"),
    ]


def test_proxy_github_blob_path_rewrites_to_raw(monkeypatch):
    handler, captured = _make_proxy_handler(
        "/api/proxy?url=https://github.com/openai/openai-python/blob/main/README.md"
    )
    calls: list[tuple[str, str | None]] = []

    def fake_urlopen(req):
        calls.append((req.full_url, req.get_header("User-agent")))
        return _FakeHTTPResponse(b"blob rewritten")

    monkeypatch.setattr("clawteam.board.server.urllib.request.urlopen", fake_urlopen)

    handler.do_GET()

    assert captured["error"] is None
    assert captured["status"] == 200
    assert handler.wfile.getvalue() == b"blob rewritten"
    assert calls == [
        ("https://raw.githubusercontent.com/openai/openai-python/main/README.md", "ClawTeam-Server")
    ]


def test_proxy_missing_url_query_param_returns_400(monkeypatch):
    handler, captured = _make_proxy_handler("/api/proxy")

    monkeypatch.setattr(
        "clawteam.board.server.urllib.request.urlopen",
        lambda req: (_ for _ in ()).throw(AssertionError("urlopen should not be called")),
    )

    handler.do_GET()

    assert captured["error"] == (400, "URL required")
    assert captured["status"] is None


def test_proxy_upstream_exception_returns_500(monkeypatch):
    handler, captured = _make_proxy_handler("/api/proxy?url=https://example.com/readme.txt")
    calls: list[tuple[str, str | None]] = []

    def fake_urlopen(req):
        calls.append((req.full_url, req.get_header("User-agent")))
        raise RuntimeError("upstream failure")

    monkeypatch.setattr("clawteam.board.server.urllib.request.urlopen", fake_urlopen)

    handler.do_GET()

    assert calls == [("https://example.com/readme.txt", "ClawTeam-Server")]
    assert captured["error"] == (500, "upstream failure")
