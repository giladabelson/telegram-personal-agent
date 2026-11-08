import asyncio
import importlib
import json
import os
import time

from jarvis import sessions


def _write(p, rows):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")


def test_redact():
    t = sessions.redact("mail a.b@x.com key=abc123 tok sk-ABCDEFGH12345 call 0501234567 ok", 200)
    assert "x.com" not in t and "abc123" not in t and "sk-ABC" not in t and "0501234567" not in t and "ok" in t
    assert len(sessions.redact("xy " * 200, 80)) == 80


def test_work_status(tmp_path, monkeypatch):
    code = tmp_path / "projects"
    _write(code / "p" / "a.jsonl", [
        {"type": "user", "cwd": "/home/example/myproj", "message": {"content": "fix the login, token=SECRET123"}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "old"}]}},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "TodoWrite", "input": {"todos": [
                {"content": "write tests", "status": "pending"}, {"content": "done one", "status": "completed"}]}},
            {"type": "text", "text": "Stopped before deploy, mail me@x.com"}]}},
    ])
    old = code / "p" / "old.jsonl"
    _write(old, [{"type": "user", "cwd": "C:/x", "message": {"content": "ancient"}}])
    os.utime(old, (time.time() - 30 * 86400,) * 2)
    monkeypatch.setattr(sessions, "CODE_DIR", code)
    monkeypatch.setattr(sessions, "COWORK_DIR", tmp_path / "none")
    r = sessions.work_status(7)
    assert "UNTRUSTED" in r["WARNING"] and len(r["sessions"]) == 1
    s = r["sessions"][0]
    assert s["kind"] == "Code" and s["project"] == "myproj"
    assert "SECRET123" not in s["title"] and "me@x.com" not in s["stopped_at"]
    assert s["stopped_at"].startswith("Stopped before deploy") and s["open_todos"] == ["write tests"]


def _tools(profile):
    if profile:
        os.environ["JARVIS_MCP_PROFILE"] = profile
    else:
        os.environ.pop("JARVIS_MCP_PROFILE", None)
    from jarvis import mcp_server
    importlib.reload(mcp_server)
    try:
        return {t.name for t in asyncio.run(mcp_server.mcp.list_tools())}
    finally:
        os.environ.pop("JARVIS_MCP_PROFILE", None)


def test_profiles():
    ext = _tools("external")
    assert ext == {"task_add", "task_list", "task_complete", "parse_when", "remind", "list_scheduled", "tell_jarvis"}
    full = _tools(None)
    assert {"work_status", "schedule_email", "schedule_whatsapp", "tell_jarvis"} <= full
