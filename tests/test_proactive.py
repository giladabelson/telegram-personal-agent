import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

os.environ["JARVIS_HOME"] = tempfile.mkdtemp()

from jarvis import hedate, proactive  # noqa: E402

TZ = hedate.TZ
import pytest  # noqa: E402

sent = []
kb = []


@pytest.fixture(autouse=True)
def _fakes(monkeypatch):
    monkeypatch.setattr(proactive.tg, "send", lambda t, chat_id=None: sent.append(t))
    monkeypatch.setattr(proactive.tg, "send_keyboard", lambda t, o, chat_id=None: (sent.append(t), kb.append(o)))
    monkeypatch.setattr(proactive, "STATE", Path(tempfile.mkdtemp()) / "state.json")
    monkeypatch.setattr(proactive.bus, "post", lambda t: posted.append(t))
    monkeypatch.setattr(proactive, "scan_candidates", lambda now: [])
    monkeypatch.setattr(proactive, "evening_summary", lambda now: None)


posted = []


proactive.OUTBOX = Path(tempfile.mkdtemp()) / "outbox.md"
proactive.open_tasks = lambda: [
    {"title": "להגיש תרגיל", "due": "2026-10-11T00:00:00.000Z"},
    {"title": "ישן", "due": "2026-10-09T00:00:00.000Z"},
    {"title": "מחר", "due": "2026-10-12T00:00:00.000Z"},
    {"title": "בלי תאריך"},
]
proactive.mail_by_label = lambda hours=24: {"לימודים-אוניברסיטה": (2, ["מבחן בקוונטים (מודל)"])}
proactive.new_mail = lambda: (2, ["מבחן בקוונטים, מאת מודל"])
proactive.today_events = lambda today: [("13:30", "[משימה] פרויקט בקורס"), (None, "יום הולדת לדנה")]
proactive.bills = lambda: ["חשבון חשמל לתשלום (חברת החשמל)"]
proactive.week_review = lambda today: ([{"title": "a"}, {"title": "b"}], [(3, "לסדר מלגה")])
proactive.important_mail_nudges = lambda: None
proactive.google_relogin_check = lambda now=None: None


def run_at(y, mo, d, h, mi):
    proactive.tick(datetime(y, mo, d, h, mi, tzinfo=TZ).astimezone(timezone.utc))


def test_brief_once_with_sunday_review():
    sent.clear()
    run_at(2026, 10, 11, 7, 59)
    assert not sent
    run_at(2026, 10, 11, 8, 1)
    run_at(2026, 10, 11, 9, 0)  # not again
    assert len(sent) == 1
    b = sent[0]
    assert b.startswith("בוקר טוב") and "להגיש תרגיל" in b and "באיחור: ישן" in b
    assert "• פרויקט בקורס, בשעה 13:30" in b and "ביומן:" in b and "יום הולדת לדנה" in b and "מבחן בקוונטים" in b
    assert "מחר" not in b.split("מיילים")[0].split("להיום:")[1]  # tomorrow's task isn't in today's list
    assert "יש 2 מיילים חדשים:" in b and "חשבון חשמל" in b
    out = proactive.OUTBOX.read_text(encoding="utf-8")
    assert "מבחן בקוונטים" not in out and "חשבון חשמל" not in out  # outside text never reaches the outbox
    assert "השבוע סגרת 2" in b and "לסדר מלגה, 3 פעמים" in b  # Sunday
    assert "בוקר טוב" in out
    assert "1. באיחור: ישן" in b and "סיימתי 1" in b and "טיוטה 3" in kb[-1] and "1. באיחור: ישן" in out  # numbered quick replies


def test_net_back_names_what_was_missed():
    sent.clear()
    since = datetime(2026, 10, 14, 7, 0, tzinfo=TZ).astimezone(timezone.utc)
    proactive.net_back(since, datetime(2026, 10, 14, 12, 30, tzinfo=TZ).astimezone(timezone.utc))
    assert "מ־07:00 עד 12:30" in sent[0] and "הודעת הבוקר" in sent[0] and "שעת העבודה של 10:00" in sent[0]
    sent.clear()
    proactive.net_back(since, since.replace(minute=5))  # 5 minutes: not worth a message
    assert not sent


def test_empty_checkin_is_not_sent(monkeypatch):
    sent.clear()
    monkeypatch.setattr(proactive, "open_tasks", lambda: [])
    run_at(2026, 10, 14, 13, 5)
    assert not sent


def test_no_late_brief_and_checkins():
    sent.clear()
    run_at(2026, 10, 12, 12, 30)  # PC woke after noon: no brief
    assert not sent
    run_at(2026, 10, 12, 13, 1)
    run_at(2026, 10, 12, 18, 5)
    assert len(sent) == 2 and sent[0].startswith("צהריים") and sent[1].startswith("ערב טוב")
    assert "סיכום שבוע" not in "".join(sent)


def test_work_sessions_once_each_and_retry_when_down(monkeypatch):
    posted.clear()
    run_at(2026, 10, 13, 9, 59)
    assert not posted

    def down(t):
        raise ConnectionError("session down")
    monkeypatch.setattr(proactive.bus, "post", down)
    with pytest.raises(ConnectionError):
        run_at(2026, 10, 13, 10, 0)  # Jarvis down: not marked done
    monkeypatch.setattr(proactive.bus, "post", lambda t: posted.append(t))
    run_at(2026, 10, 13, 10, 20)
    run_at(2026, 10, 13, 11, 0)
    run_at(2026, 10, 13, 16, 1)
    run_at(2026, 10, 13, 18, 1)  # window over
    assert posted == ["run work session"] * 2


def test_checkin_window_survives_dst_day():
    sent.clear()
    run_at(2026, 10, 25, 13, 0)  # DST ends that night/morning in Israel
    assert len(sent) == 1


def test_send_keyboard_payload(monkeypatch):
    from jarvis import tg
    monkeypatch.undo()  # drop the autouse fake of send_keyboard
    got = {}
    monkeypatch.setattr(tg, "_call", lambda m, d=None, **k: got.update(m=m, d=d))
    tg.send_keyboard("שלום", ["כן", "לא"], chat_id="1")
    assert got["m"] == "sendMessage"
    assert got["d"]["reply_markup"]["keyboard"] == [[{"text": "כן"}], [{"text": "לא"}]]
    assert got["d"]["reply_markup"]["one_time_keyboard"] is True
