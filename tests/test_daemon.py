import os
import tempfile
from datetime import datetime, timedelta, timezone

os.environ["JARVIS_HOME"] = tempfile.mkdtemp()  # never touch the real schedule

from jarvis import daemon  # noqa: E402

import pytest  # noqa: E402

sent = []


@pytest.fixture(autouse=True)
def _fake_telegram(monkeypatch):
    monkeypatch.setattr(daemon.tg, "send", lambda text, chat_id=None: sent.append(text))


def test_reminder_fires_once_and_late_notice():
    sent.clear()
    daemon.save_jobs([])
    now = datetime.now(timezone.utc)
    daemon.add_job("reminder", (now + timedelta(minutes=1)).isoformat(), "לקנות חלב")
    future = daemon.add_job("reminder", (now + timedelta(hours=3)).isoformat(), "עתידי")
    later = now + timedelta(hours=1)  # PC was asleep for an hour
    daemon.run_due(later)
    daemon.run_due(later)  # second pass must not resend
    assert sent[0] == "תזכורת ⏰\nלקנות חלב"
    assert "באיחור" in sent[1] and len(sent) == 2
    assert [j["status"] for j in daemon.load_jobs()] == ["done", "pending"]
    assert daemon.cancel_job(future["id"]) and not daemon.cancel_job(future["id"])


def test_reminder_waits_for_network_instead_of_failing(monkeypatch):
    import urllib.error
    daemon.save_jobs([])
    now = datetime.now(timezone.utc)
    j = daemon.add_job("reminder", (now + timedelta(minutes=1)).isoformat(), "לקחת תרופה")

    def offline(text, chat_id=None):
        raise urllib.error.URLError("getaddrinfo failed")
    monkeypatch.setattr(daemon.tg, "send", offline)
    daemon.run_due(now + timedelta(minutes=2))
    assert next(x for x in daemon.load_jobs() if x["id"] == j["id"])["status"] == "pending"  # kept, not failed
    got = []
    monkeypatch.setattr(daemon.tg, "send", lambda t, chat_id=None: got.append(t))
    daemon.run_due(now + timedelta(minutes=40))  # internet back
    assert got[0] == "תזכורת ⏰\nלקחת תרופה" and "באיחור" in got[1]
    assert next(x for x in daemon.load_jobs() if x["id"] == j["id"])["status"] == "done"


def test_server_error_is_not_retried():
    import urllib.error
    assert not daemon.is_network_error(urllib.error.HTTPError("u", 400, "bad", {}, None))
    assert daemon.is_network_error(urllib.error.URLError("dns"))
    assert daemon.is_network_error(TimeoutError())


def test_failed_job_is_reported_not_silent():
    sent.clear()
    daemon.save_jobs([])
    daemon.RUNNERS["boom"] = lambda j: 1 / 0
    daemon.add_job("boom", datetime.now(timezone.utc).isoformat(), "משהו")
    daemon.run_due()
    assert daemon.load_jobs()[0]["status"] == "failed"
    assert sent and "לא הצלחתי" in sent[0]


def test_crash_mid_send_is_not_retried():
    sent.clear()
    job = daemon.add_job("reminder", datetime.now(timezone.utc).isoformat(), "x")
    jobs = daemon.load_jobs()
    for j in jobs:
        if j["id"] == job["id"]:
            j["status"] = "running"
    daemon.save_jobs(jobs)
    daemon.recover_running()
    assert any(j["status"] == "unknown" for j in daemon.load_jobs())
    assert "לא בטוח" in sent[0]


def test_naive_time_rejected():
    import pytest
    with pytest.raises(ValueError):
        daemon.add_job("reminder", "2026-10-12T15:00:00", "x")


def test_past_time_rejected():
    import pytest
    with pytest.raises(ValueError):
        daemon.add_job("reminder", (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(), "x")


def test_transcribe_refuses_paths_outside_inbox():
    import pytest
    with pytest.raises(ValueError):
        daemon.transcribe("C:/Windows/win.ini")
    with pytest.raises(ValueError):
        daemon.transcribe("http://evil.example/x")


def test_dispatched_job_lifecycle(monkeypatch):
    sent.clear()
    daemon.save_jobs([])
    posted = []
    daemon.RUNNERS["wa"] = lambda j: posted.append(j["id"]) or "dispatched"
    now = datetime.now(timezone.utc)
    j = daemon.add_job("wa", (now + timedelta(seconds=30)).isoformat(), "וואטסאפ לאמא: היי")
    daemon.run_due(now + timedelta(minutes=1))
    assert posted == [j["id"]] and daemon.load_jobs()[0]["status"] == "dispatched"
    assert daemon.job_done(j["id"], True) and "בוצע" in sent[-1]
    assert not daemon.job_done(j["id"], True)  # only once
    k = daemon.add_job("wa", (now + timedelta(seconds=30)).isoformat(), "שני")
    daemon.run_due(now + timedelta(minutes=1))
    daemon.stale_dispatched(now + timedelta(minutes=20))  # Jarvis never reported back
    assert [x["status"] for x in daemon.load_jobs() if x["id"] == k["id"]] == ["unknown"]
    assert "לא בטוח" in sent[-1]
