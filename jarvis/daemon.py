"""jarvisd: the one long-running local service.

- keeps the Hebrew speech model loaded (/transcribe)
- owns the job schedule (reminders, approved scheduled sends) and fires them on time in plain code
- runs cheap "is anything due?" checks; only calls a model when something needs attention
HTTP on 127.0.0.1 only, every request needs the shared key from ~/.jarvis/daemon.key.
"""
import json
import logging
import subprocess
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import config, tg

log = logging.getLogger("jarvisd")
JOBS = config.DATA / "jobs.json"
_lock = threading.Lock()
_model = None
_model_err = None


# ---------- jobs ----------
def load_jobs():
    try:
        return json.loads(JOBS.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []


def save_jobs(jobs):
    tmp = JOBS.with_suffix(".tmp")
    tmp.write_text(json.dumps(jobs, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(JOBS)  # atomic: a crash never leaves half a file


def add_job(kind, at_iso, text, payload=None):
    at = datetime.fromisoformat(at_iso)
    if at.tzinfo is None:
        raise ValueError("at must include a timezone")
    if at < datetime.now(timezone.utc) - timedelta(minutes=2):
        raise ValueError("time is in the past")
    job = {"id": uuid.uuid4().hex[:8], "kind": kind, "at": at.astimezone(timezone.utc).isoformat(),
           "text": text, "payload": payload or {}, "status": "pending",
           "created": datetime.now(timezone.utc).isoformat()}
    with _lock:
        jobs = load_jobs()
        jobs.append(job)
        save_jobs(jobs)
    return job


def cancel_job(job_id):
    with _lock:
        jobs = load_jobs()
        for j in jobs:
            if j["id"] == job_id and j["status"] == "pending":
                j["status"] = "cancelled"
                save_jobs(jobs)
                return True
    return False


RUNNERS = {}


def runner(kind):
    def deco(fn):
        RUNNERS[kind] = fn
        return fn
    return deco


@runner("reminder")
def _run_reminder(job):
    tg.send(f"תזכורת ⏰\n{job['text']}")


def is_network_error(e):
    """DNS/connection/timeout failures: the request never reached the server, so retrying can't double-send."""
    import socket
    import urllib.error
    if isinstance(e, urllib.error.HTTPError):  # the server answered: not a network problem
        return False
    names = {c.__name__ for c in type(e).__mro__}
    return (isinstance(e, (urllib.error.URLError, ConnectionError, TimeoutError, socket.gaierror, socket.timeout))
            or bool(names & {"TransportError", "ServerNotFoundError", "ConnectionError", "NameResolutionError"}))


def is_bus_down(e):
    """The local Jarvis session wasn't reachable (bus.BusDown): nothing was delivered, safe to retry for a while."""
    return "BusDown" in {c.__name__ for c in type(e).__mro__}


_net = {"up": True, "checked": 0.0, "down_since": None}


def net_up():
    """Cheap DNS probe, cached 30 s. While it's down we skip all work instead of logging an error every pass."""
    import socket
    if time.time() - _net["checked"] > 30:
        _net["checked"] = time.time()
        try:
            socket.getaddrinfo("api.telegram.org", 443)
            _net["up"] = True
        except OSError:
            _net["up"] = False
    return _net["up"]


NET_BACK = []  # callbacks(down_since_utc) run once when the internet returns after an outage


def run_due(now=None):
    """Fire every pending job whose time has come. Missed jobs (PC was off) fire late, once, marked as late."""
    now = now or datetime.now(timezone.utc)
    with _lock:
        jobs = load_jobs()
        due = [j for j in jobs if j["status"] == "pending" and datetime.fromisoformat(j["at"]) <= now]
        for j in due:
            j["status"] = "running"  # mark first: a crash mid-send must not send twice
        if due:
            save_jobs(jobs)
    for j in due:
        age = (now - datetime.fromisoformat(j["at"])).total_seconds()
        try:
            res = RUNNERS[j["kind"]](j)
            status, err = ("dispatched" if res == "dispatched" else "done"), None  # dispatched: Jarvis finishes it
            if age > 600:
                try:
                    tg.send("התזכורת הזאת מגיעה באיחור, כי המחשב או האינטרנט היו מנותקים.")
                except Exception:
                    pass
        except Exception as e:  # report, never fail silently
            if (is_network_error(e) and age < 86400) or (is_bus_down(e) and age < 7200):
                # no internet: nothing was sent, so it's safe to try again on the next pass
                status, err = "pending", None
                log.warning("job %s waits (network or Jarvis session down): %s", j["id"], e)
                with _lock:
                    jobs = load_jobs()
                    for x in jobs:
                        if x["id"] == j["id"]:
                            x["status"] = "pending"
                    save_jobs(jobs)
                continue
            status, err = "failed", str(e)
            log.exception("job %s failed", j["id"])
            try:
                tg.send(f"לא הצלחתי לבצע את זה ⚠️\n{j['text']}\nלנסות שוב?")
            except Exception:
                pass
        with _lock:
            jobs = load_jobs()
            for x in jobs:
                if x["id"] == j["id"]:
                    x["status"], x["error"], x["ran"] = status, err, now.isoformat()
            save_jobs(jobs)
    return due


def job_done(job_id, ok, note=""):
    with _lock:
        jobs = load_jobs()
        j = next((x for x in jobs if x["id"] == job_id and x["status"] == "dispatched"), None)
        if not j:
            return False
        j["status"], j["error"] = ("done", None) if ok else ("failed", note)
        save_jobs(jobs)
    tg.send(f"בוצע ✅\n{j['text']}" if ok else f"לא הצלחתי לבצע את זה ⚠️\n{j['text']}\n{note}")
    return True


def stale_dispatched(now=None):
    """A dispatched job Jarvis never reported back on within 10 minutes: tell the user, don't retry."""
    now = now or datetime.now(timezone.utc)
    with _lock:
        jobs = load_jobs()
        stale = [j for j in jobs if j["status"] == "dispatched" and (now - datetime.fromisoformat(j["ran"])).total_seconds() > 600]
        for j in stale:
            j["status"] = "unknown"
        if stale:
            save_jobs(jobs)
    for j in stale:
        tg.send(f"לא בטוח שזה בוצע ⚠️\n{j['text']}\nתבדוק ותגיד לי אם לנסות שוב.")


def recover_running():
    """A job left 'running' by a crash is NOT retried automatically (could double-send); tell the user."""
    with _lock:
        jobs = load_jobs()
        stuck = [j for j in jobs if j["status"] == "running"]
        for j in stuck:
            j["status"] = "unknown"
        if stuck:
            save_jobs(jobs)
    for j in stuck:
        tg.send(f"לא בטוח אם זה בוצע לפני התקלה ⚠️\n{j['text']}\nתבדוק ותגיד לי אם לנסות שוב.")


# ---------- speech ----------
def load_model():
    global _model, _model_err
    try:
        from faster_whisper import WhisperModel
        name = config.WHISPER_MODEL
        _model = WhisperModel(name, device="cpu", compute_type="int8", cpu_threads=4)
        log.info("speech model loaded: %s", name)
    except Exception as e:
        _model_err = str(e)
        log.exception("speech model failed to load")


INBOX = config.TG_DIR / "inbox"
FAKE_INBOX = Path.home() / ".claude" / "channels" / "fakechat" / "inbox"  # local E2E test UI


def transcribe(path):
    p = Path(path).resolve()
    if not any(p.is_relative_to(r.resolve()) for r in (INBOX, FAKE_INBOX, config.DATA)) or not p.is_file():
        raise ValueError("only files from the Telegram inbox")
    if _model is None:
        raise RuntimeError(_model_err or "speech model still loading")
    import numpy as np
    t0 = time.time()
    # decode with ffmpeg ourselves: faster-whisper's PyAV path breaks across av versions
    pcm = subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-protocol_whitelist", "file", "-i", str(p), "-f", "s16le", "-ac", "1",
                          "-ar", "16000", "-"], capture_output=True, check=True, timeout=120).stdout
    audio = np.frombuffer(pcm, np.int16).astype(np.float32) / 32768.0
    dur = len(audio) / 16000
    text, engine = "", "faster-whisper"
    if dur <= SHORT_CLIP and CPP_CLI.exists():
        text, engine = _cpp_short(pcm, dur), "whisper.cpp"
    if not text.strip():
        segs, info = _model.transcribe(audio, language="he", vad_filter=True, beam_size=1,
                                       initial_prompt=config.INITIAL_PROMPT)
        text = " ".join(s.text.strip() for s in segs)
    if not text.strip():  # the prompt can occasionally make the model emit nothing; retry plain
        segs, info = _model.transcribe(audio, language="he", vad_filter=True, beam_size=1)
        text = " ".join(s.text.strip() for s in segs)
    return {"text": text, "seconds": round(time.time() - t0, 1), "audio_seconds": round(dur, 1), "engine": engine}


# Short notes: whisper.cpp with the encoder window shrunk to the clip length (-ac). Whisper otherwise always
# encodes a full 30 s window, so a 4 s note took ~20 s; with -ac it takes ~8 s on this laptop (measured).
CPP_DIR = config.HOME / "whispercpp"
CPP_CLI, CPP_MODEL = CPP_DIR / "bin" / "whisper-cli.exe", CPP_DIR / "ggml-model.bin"
SHORT_CLIP = 25  # seconds


def _cpp_short(pcm, dur):
    wav = config.DATA / f"stt_{uuid.uuid4().hex[:6]}.wav"
    try:
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "s16le", "-ar", "16000", "-ac", "1", "-i", "-", str(wav)],
                       input=pcm, check=True, timeout=60)
        ac = min(1500, int(dur / 30 * 1500) + 128)
        r = subprocess.run([str(CPP_CLI), "-m", str(CPP_MODEL), "-f", str(wav), "-l", "he", "-t", "4", "--no-timestamps",
                            "-ac", str(ac), "-bs", "1", "-bo", "1"], capture_output=True, timeout=120)
        return r.stdout.decode("utf-8", "replace").strip()
    except Exception:
        log.exception("whisper.cpp failed; falling back")
        return ""
    finally:
        wav.unlink(missing_ok=True)


def tts_ogg(text, voice="he-IL-HilaNeural"):
    if voice not in ("he-IL-HilaNeural", "he-IL-AvriNeural"):
        voice = "he-IL-HilaNeural"
    text = text[:1500]
    out = config.DATA / f"tts_{uuid.uuid4().hex[:6]}"
    src, ogg = out.with_suffix(".mp3"), out.with_suffix(".ogg")
    try:
        subprocess.run([str(Path(config.PROJECT, ".venv", "Scripts", "edge-tts.exe")), "--voice", voice,
                        "--text", text, "--write-media", str(src)], check=True, timeout=60, capture_output=True)
    except Exception:
        # edge-tts is blocked from some regions (rany2/edge-tts#490): fall back to the offline Windows voice
        log.warning("edge-tts failed, using Windows voice")
        txt, src = out.with_suffix(".txt"), out.with_suffix(".wav")
        txt.write_text(text, encoding="utf-8")
        subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                        str(config.PROJECT / "jarvis" / "tts_windows.ps1"), "-TextFile", str(txt), "-Out", str(src)],
                       check=True, timeout=60, capture_output=True)
        txt.unlink(missing_ok=True)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-c:a", "libopus", "-b:a", "32k", str(ogg)],
                   check=True, timeout=60)
    src.unlink(missing_ok=True)
    return ogg


# ---------- http ----------
class H(BaseHTTPRequestHandler):
    def _out(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _auth(self):
        if self.headers.get("X-Jarvis-Key") != config.api_key():
            self._out(403, {"error": "forbidden"})
            return False
        return True

    def do_GET(self):
        if self.path == "/health":  # no secrets here, no auth needed
            return self._out(200, {"ok": True, "model": _model is not None, "model_error": _model_err})
        if not self._auth():
            return
        if self.path == "/jobs":
            return self._out(200, [j for j in load_jobs() if j["status"] == "pending"])
        self._out(404, {"error": "not found"})

    def do_POST(self):
        if not self._auth():
            return
        n = int(self.headers.get("Content-Length", 0))
        if n > 65536:
            return self._out(413, {"error": "too large"})
        body = json.loads(self.rfile.read(n) or b"{}")
        try:
            if self.path == "/transcribe":
                return self._out(200, transcribe(body["path"]))
            if self.path == "/speak":
                return self._out(200, {"sent": bool(tg.send_voice(tts_ogg(body["text"], body.get("voice", "he-IL-HilaNeural"))))})
            if self.path == "/jobs":
                if body["kind"] not in RUNNERS:
                    raise ValueError(f"unknown kind {body['kind']}")
                return self._out(200, add_job(body["kind"], body["at"], body["text"], body.get("payload")))
            if self.path == "/cancel":
                return self._out(200, {"cancelled": cancel_job(body["id"])})
            if self.path == "/job_get":  # only a job that is due and dispatched right now can be read back
                j = next((x for x in load_jobs() if x["id"] == body["id"] and x["status"] == "dispatched"), None)
                return self._out(200 if j else 404, j or {"error": "no such dispatched job"})
            if self.path == "/job_done":
                return self._out(200, {"ok": job_done(body["id"], bool(body["ok"]), str(body.get("note", ""))[:300])})
        except Exception as e:
            return self._out(400, {"error": str(e)})
        self._out(404, {"error": "not found"})

    def log_message(self, *a):
        pass


def loop():
    while True:
        if not net_up():
            if not _net["down_since"]:
                _net["down_since"] = datetime.now(timezone.utc)
                log.warning("internet down: waiting (reminders keep their place in line)")
            time.sleep(20)
            continue
        if _net["down_since"]:
            since, _net["down_since"] = _net["down_since"], None
            log.info("internet back after %s", datetime.now(timezone.utc) - since)
            for cb in NET_BACK:
                try:
                    cb(since)
                except Exception:
                    log.exception("net-back callback failed")
        _guarded("run_due", run_due)
        for check in CHECKS:
            _guarded(check.__name__, check)  # one failing check must not starve the others
        time.sleep(20)


_burst = {}  # name -> (first_error_time, count): log one line per burst of identical failures, not one per pass


def _guarded(name, fn):
    try:
        fn()
    except Exception as e:
        if is_bus_down(e) or is_network_error(e):
            kind = "bus" if is_bus_down(e) else "net"
            if (name, kind) not in _burst:
                log.warning("%s: Jarvis session/bus unreachable, will keep retrying quietly" if kind == "bus"
                            else "%s: network error, will keep retrying quietly", name)
                _burst[(name, kind)] = time.time()
            return
        log.exception("%s failed", name)
        return
    for k in [k for k in _burst if k[0] == name]:
        log.info("%s: recovered after %.0f s", name, time.time() - _burst.pop(k))


def work_catchup(now_utc=None):
    """Work session missed because the PC was asleep/off (proactive.tick only covers a 2 h window after each slot).
    Run the latest missed slot once, same day only, if no work session ran today and it is before 21:00."""
    from . import bus, hedate, proactive
    now_utc = now_utc or datetime.now(timezone.utc)
    now = now_utc.astimezone(hedate.TZ)
    day = now.date().isoformat()
    if now.hour >= 21:
        return
    st = proactive._state()
    if any(st.get(f"work-{day}-{t.hour}") for t in proactive.WORK_SESSIONS):
        return
    past = [t for t in proactive.WORK_SESSIONS if now.time() >= (datetime.combine(now.date(), t) + timedelta(hours=2)).time()]
    if past and not any(t <= now.time() < (datetime.combine(now.date(), t) + timedelta(hours=2)).time()
                        for t in proactive.WORK_SESSIONS):
        t = past[-1]
        if proactive.once(f"work-{day}-{t.hour}", lambda: bus.post("run work session")):
            log.info("caught up the missed %s work session", t.strftime("%H:%M"))


CHECKS = [stale_dispatched, work_catchup]  # plain-code "is anything due?" checks; proactive.py adds more


def main():
    from logging.handlers import RotatingFileHandler
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[RotatingFileHandler(config.LOGS / "jarvisd.log", maxBytes=5_000_000,
                                                      backupCount=2, encoding="utf-8")])
    from . import proactive  # noqa: F401  registers runners/checks
    try:
        recover_running()
    except Exception:  # no internet at boot must not crash-loop the daemon
        log.exception("recover_running could not notify")
    threading.Thread(target=load_model, daemon=True).start()
    threading.Thread(target=loop, daemon=True).start()
    log.info("jarvisd listening on %s:%s; runners=%s checks=%s", config.HOST, config.PORT,
             sorted(RUNNERS), [c.__name__ for c in CHECKS])
    ThreadingHTTPServer((config.HOST, config.PORT), H).serve_forever()


if __name__ == "__main__":
    # Run main() from the imported module, not __main__: proactive.py registers its checks/runners on
    # jarvis.daemon, and a second copy of this file as __main__ would silently never see them.
    import jarvis.daemon
    jarvis.daemon.main()
