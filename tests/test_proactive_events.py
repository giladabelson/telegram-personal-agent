import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("JARVIS_HOME", tempfile.mkdtemp())

import pytest  # noqa: E402

from jarvis import hedate, proactive  # noqa: E402

TZ = hedate.TZ
posted, sent = [], []


@pytest.fixture(autouse=True)
def _fakes(monkeypatch):
    posted.clear()
    sent.clear()
    monkeypatch.setattr(proactive, "STATE", Path(tempfile.mkdtemp()) / "state.json")
    monkeypatch.setattr(proactive.bus, "post", lambda t: posted.append(t))
    monkeypatch.setattr(proactive, "send", lambda text, record=None, buttons=None: sent.append(text))
    monkeypatch.setattr(proactive, "_last_scan", [datetime.min.replace(tzinfo=timezone.utc)])
    monkeypatch.setattr(proactive, "open_tasks", lambda: [])
    monkeypatch.setattr(proactive, "today_events", lambda d: [])
    monkeypatch.setattr(proactive, "mail_by_label", lambda hours=24: {})
    monkeypatch.setattr(proactive, "new_mail", lambda: (0, []))
    monkeypatch.setattr(proactive, "bills", lambda: [])
    monkeypatch.setattr(proactive, "important_mail_nudges", lambda: None)
    monkeypatch.setattr(proactive, "google_relogin_check", lambda now=None: None)
    monkeypatch.setattr(proactive, "upcoming_send_nudges", lambda now: None)
    monkeypatch.setattr(proactive, "scan_candidates", lambda now: [])
    monkeypatch.setattr(proactive, "evening_summary", lambda now: sent.append("evening"))


def at(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=TZ).astimezone(timezone.utc)


def test_digest_only_sunday_evening_window_once():
    proactive.tick(at(2026, 10, 14, 20, 5))  # Wednesday
    proactive.tick(at(2026, 10, 18, 19, 59))  # Sunday, too early
    assert "run memory digest" not in posted
    proactive.tick(at(2026, 10, 18, 20, 5))
    proactive.tick(at(2026, 10, 18, 20, 30))
    assert posted.count("run memory digest") == 1
    proactive.tick(at(2026, 10, 18, 22, 1))  # window closed
    assert posted.count("run memory digest") == 1


def test_digest_retries_when_bus_down():
    def down(t):
        raise proactive.bus.BusDown("x")
    proactive.bus.post = down
    proactive.tick(at(2026, 10, 18, 20, 5))
    proactive.bus.post = lambda t: posted.append(t)
    proactive.tick(at(2026, 10, 18, 20, 10))
    assert posted.count("run memory digest") == 1


def test_evening_window_once():
    proactive.tick(at(2026, 10, 14, 20, 59))
    assert "evening" not in sent
    proactive.tick(at(2026, 10, 14, 21, 0))
    proactive.tick(at(2026, 10, 14, 21, 20))
    assert sent.count("evening") == 1


def test_build_evening():
    assert proactive.build_evening([], [], []) == ""
    t = proactive.build_evening(["א", "ב"], [{"kind": "reminder", "text": "ג"}], ["- 14.10: ד - בדקתי מחירים"])
    lines = t.splitlines()
    assert len(lines) == 5 and lines[-1] == "ועוד 1."
    assert "סגרתי: א" in lines[1]


def test_evening_opt_out(monkeypatch, tmp_path):
    f = tmp_path / "prefs.md"
    monkeypatch.setattr(proactive, "PREFS", f)
    assert not proactive.evening_off()
    f.write_text("- בלי סיכום ערב\n", encoding="utf-8")
    assert proactive.evening_off()


def test_scan_window_and_interval(monkeypatch):
    calls = []
    monkeypatch.setattr(proactive, "rule_scan", lambda now: calls.append(now))
    proactive.tick(at(2026, 10, 14, 6, 59))
    proactive.tick(at(2026, 10, 14, 7, 0))
    proactive.tick(at(2026, 10, 14, 7, 3))  # < 5 min later
    proactive.tick(at(2026, 10, 14, 7, 6))
    proactive.tick(at(2026, 10, 14, 23, 0))
    assert len(calls) == 2


def _cands(monkeypatch, items):
    monkeypatch.setattr(proactive, "scan_candidates", lambda now: list(items))


def test_scan_first_run_is_baseline_then_dedupe(monkeypatch):
    now = at(2026, 10, 14, 10, 0)
    _cands(monkeypatch, [("mail", "m1")])
    proactive.rule_scan(now)
    assert posted == []  # existing mail is not news
    _cands(monkeypatch, [("mail", "m1"), ("mail", "m2")])
    proactive.rule_scan(now)
    proactive.rule_scan(now)
    assert posted == ["run event mail m2"]


def test_scan_quiet_when_nothing(monkeypatch):
    proactive.rule_scan(at(2026, 10, 14, 10, 0))
    proactive.rule_scan(at(2026, 10, 14, 10, 5))
    assert posted == [] and sent == []


def test_scan_daily_cap(monkeypatch):
    now = at(2026, 10, 14, 10, 0)
    proactive.rule_scan(now)  # baseline
    _cands(monkeypatch, [("mail", f"m{i}") for i in range(10)])
    proactive.rule_scan(now)
    assert len(posted) == proactive.MAX_EVENTS_PER_DAY
    proactive.rule_scan(now)
    assert len(posted) == proactive.MAX_EVENTS_PER_DAY
    proactive.rule_scan(at(2026, 10, 15, 10, 0))  # next day: cap resets
    assert len(posted) == proactive.MAX_EVENTS_PER_DAY + 4


def test_scan_quiet_during_work_session(monkeypatch):
    proactive.rule_scan(at(2026, 10, 14, 9, 0))  # baseline
    proactive.once("work-2026-10-14-10", lambda: None)
    _cands(monkeypatch, [("mail", "m9")])
    proactive.rule_scan(datetime.now(timezone.utc))  # state stamp is "now": session counts as running
    assert posted == []


def test_scan_bus_down_not_marked_seen(monkeypatch):
    now = at(2026, 10, 14, 10, 0)
    proactive.rule_scan(now)
    _cands(monkeypatch, [("due", "ev1")])
    monkeypatch.setattr(proactive.bus, "post", lambda t: (_ for _ in ()).throw(proactive.bus.BusDown("x")))
    proactive.rule_scan(now)
    monkeypatch.setattr(proactive.bus, "post", lambda t: posted.append(t))
    proactive.rule_scan(now)
    assert posted == ["run event due ev1"]


def test_scan_rejects_odd_ids(monkeypatch):
    proactive.rule_scan(at(2026, 10, 14, 10, 0))
    _cands(monkeypatch, [("mail", "a b; rm")])
    proactive.rule_scan(at(2026, 10, 14, 10, 0))
    assert posted == []
