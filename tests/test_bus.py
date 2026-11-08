import os
import socket
import tempfile
from datetime import datetime, timezone

os.environ["JARVIS_HOME"] = tempfile.mkdtemp()

import pytest  # noqa: E402

from jarvis import bus, daemon, hedate, proactive  # noqa: E402
from pathlib import Path  # noqa: E402


@pytest.fixture(autouse=True)
def _closed_port(monkeypatch):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()  # nothing listens here: simulates the bus being down
    monkeypatch.setattr(bus, "URL", f"http://127.0.0.1:{port}")
    monkeypatch.setattr(bus.time, "sleep", lambda s: None)
    monkeypatch.setattr(proactive, "STATE", Path(tempfile.mkdtemp()) / "state.json")


def test_post_raises_clean_busdown():
    with pytest.raises(bus.BusDown):
        bus.post("run work session")


def test_ping_status_down_is_not_hung():
    assert bus.ping_status(delays=()) == "down"
    assert bus.ping() is False


def test_retry_then_success(monkeypatch):
    calls = []

    async def flaky(*a, **k):
        calls.append(1)
        if len(calls) < 3:
            raise bus.BusDown("x")
        return True
    monkeypatch.setattr(bus, "_send", flaky)
    assert bus.post("hi") is True and len(calls) == 3


def test_guarded_logs_one_line_per_burst(caplog):
    daemon._burst.clear()

    def down():
        raise bus.BusDown("x")
    with caplog.at_level("INFO", logger="jarvisd"):
        for _ in range(50):
            daemon._guarded("tick", down)
        assert len([r for r in caplog.records if r.levelname == "WARNING"]) == 1
        assert not any(r.exc_info for r in caplog.records)
        daemon._guarded("tick", lambda: None)
    assert any("recovered" in r.getMessage() for r in caplog.records)


def at(h, m=0, day=14):
    return datetime(2026, 10, day, h, m, tzinfo=hedate.TZ).astimezone(timezone.utc)


def test_work_catchup_same_day_once(monkeypatch):
    posted = []
    monkeypatch.setattr(bus, "post", lambda t: posted.append(t))
    daemon.work_catchup(at(9))            # before first slot
    daemon.work_catchup(at(10, 30))       # tick's own window
    assert not posted
    daemon.work_catchup(at(13))           # 10:00 slot missed (PC woke at 13:00)
    daemon.work_catchup(at(13, 5))        # only once
    assert posted == ["run work session"]


def test_work_catchup_skips_when_done_late_evening_and_other_day(monkeypatch):
    posted = []
    monkeypatch.setattr(bus, "post", lambda t: posted.append(t))
    daemon.work_catchup(at(21, 30))
    assert not posted
    proactive._save({"work-2026-10-14-16": "x"})
    daemon.work_catchup(at(19))
    assert not posted
    daemon.work_catchup(at(13, day=15))   # yesterday's key doesn't count, but 13:00 on the 15th is a legit catch-up
    assert posted == ["run work session"]


def test_whatsapp_job_waits_for_bus(monkeypatch):
    daemon.save_jobs([])
    sent = []
    monkeypatch.setattr(daemon.tg, "send", lambda t, chat_id=None: sent.append(t))
    j = daemon.add_job("whatsapp", datetime.now(timezone.utc).isoformat(), "x", {})
    daemon.run_due()
    assert daemon.load_jobs()[0]["status"] == "pending" and not sent
    monkeypatch.setattr(bus, "post", lambda t: True)
    daemon.run_due()
    assert daemon.load_jobs()[0]["status"] == "dispatched"


# ---- transcript analysis: hang / idle detection and lost-message extraction ----
import json as _json  # noqa: E402


def _q(ts, mid, src="plugin:telegram:telegram", extra=""):
    c = f'<channel source="{src}" chat_id="1" message_id="{mid}" user="1" ts="{ts}"{extra}>\nhi\n</channel>'
    return _json.dumps({"type": "queue-operation", "operation": "enqueue", "timestamp": ts, "content": c})


def _turn(ts, role, content):
    return _json.dumps({"type": role, "timestamp": ts, "message": {"role": role, "content": content}})


NOW = datetime(2026, 10, 14, 9, 30, tzinfo=timezone.utc)   # 12:30 Israel


def test_queue_ops_are_not_activity_and_hang_is_detected():
    lines = [
        _turn("2026-10-14T08:22:00Z", "assistant", [{"type": "tool_use", "id": "t1", "name": "x", "input": {}}]),
        _q("2026-10-14T09:07:16Z", "135", extra=' attachment_kind="voice"'),
        _q("2026-10-14T09:08:08Z", "sys1", src="plugin:fakechat:fakechat"),
    ]
    r = bus.analyze_transcript(lines, NOW)
    assert r["hung"] and not r["idle"]
    assert r["last_turn"] == datetime(2026, 10, 14, 8, 22, tzinfo=timezone.utc)   # queue ops ignored
    assert [u["id"] for u in r["unprocessed"]] == ["135", "sys1"]
    assert bus.lost_notice(r["unprocessed"]) == "התאפסתי ולא טיפלתי בהודעה ששלחת ב־12:07 (הודעה קולית). אפשר לשלוח שוב?"


def test_recent_enqueue_is_not_yet_hung():
    lines = [_turn("2026-10-14T09:00:00Z", "assistant", [{"type": "text", "text": "a"}]), _q("2026-10-14T09:27:00Z", "7")]
    r = bus.analyze_transcript(lines, NOW)
    assert not r["hung"] and len(r["unprocessed"]) == 1 and not r["idle"]


def test_busy_session_with_turns_after_enqueue_is_not_hung():
    lines = [_q("2026-10-14T09:10:00Z", "8"),
             _turn("2026-10-14T09:10:01Z", "user", '<channel source="plugin:telegram:telegram" chat_id="1" message_id="8" user="1" ts="x">\nhi\n</channel>'),
             _turn("2026-10-14T09:28:00Z", "assistant", [{"type": "text", "text": "working"}])]
    r = bus.analyze_transcript(lines, NOW)
    assert not r["hung"] and not r["unprocessed"]


def test_idle_ignores_queue_ops_and_requires_nothing_waiting():
    done = [_turn("2026-10-14T09:20:00Z", "assistant", [{"type": "text", "text": "ok"}])]
    assert bus.analyze_transcript(done, NOW)["idle"]
    assert not bus.analyze_transcript(done + [_q("2026-10-14T09:29:00Z", "9")], NOW)["idle"]   # a queued message = not idle
    open_tool = [_turn("2026-10-14T09:20:00Z", "assistant", [{"type": "tool_use", "id": "t", "name": "x", "input": {}}])]
    assert not bus.analyze_transcript(open_tool, NOW)["idle"]
    closed = open_tool + [_turn("2026-10-14T09:21:00Z", "user", [{"type": "tool_result", "tool_use_id": "t", "content": "ok"}])]
    assert bus.analyze_transcript(closed, NOW)["idle"]


def test_open_tool_with_queued_message_is_hung():
    lines = [_turn("2026-10-14T09:05:00Z", "assistant", [{"type": "tool_use", "id": "t", "name": "x", "input": {}}]),
             _q("2026-10-14T09:26:00Z", "10"),
             _turn("2026-10-14T09:27:00Z", "assistant", [{"type": "text", "text": "late line"}])]
    r = bus.analyze_transcript(lines, NOW)
    assert r["hung"] and "tool_use" in r["reason"]


def test_lost_notice_lists_at_most_three_telegram_only_without_content():
    lines = [_q(f"2026-10-14T09:0{i}:00Z", str(i)) for i in range(1, 6)] + [_q("2026-10-14T09:08:00Z", "p", src="plugin:fakechat:fakechat")]
    n = bus.lost_notice(bus.analyze_transcript(lines, NOW)["unprocessed"])
    assert n.count("(טקסט)") == 3 and "ועוד 2" in n and "hi" not in n and "12:01" in n
    assert bus.lost_notice(bus.analyze_transcript([lines[-1]], NOW)["unprocessed"]) == ""


def test_transcript_status_cli_shape(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text("\n".join([_q("2026-10-14T09:00:00Z", "1")]), encoding="utf-8")
    st = bus.transcript_status(str(p), NOW)
    assert st["hung"] and st["unprocessed"] == 1 and st["notice"]
    _json.dumps(st)
