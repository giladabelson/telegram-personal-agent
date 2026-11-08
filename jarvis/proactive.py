"""Plain-code proactive messages, run from jarvisd's loop (R4).

Everything here is templates + Google API calls: no model tokens are spent on briefs, check-ins or nudges.
Email subjects/senders are shown verbatim to the user (data only; no model reads them here, so nothing to inject).
"""
import json
import re
from datetime import date, datetime, time, timedelta, timezone

from . import bus, config, daemon, gapi, hedate, tg

STATE = config.DATA / "proactive_state.json"
GOOGLE_LOGIN = config.HOME / "google" / "login_date"
OUTBOX = config.PROJECT / "memory" / "outbox.md"
LABELS = config.MAIL_LABELS
BRIEF_AT, CHECKINS = time(8, 0), [time(13, 0), time(18, 0)]
WORK_SESSIONS = [time(10, 0), time(16, 0)]  # Jarvis advances open tasks on its own (CLAUDE.md "Work sessions")
LATEST_BRIEF = time(12, 0)  # PC was off all morning: skip the brief rather than send it at 15:00


# ---------- small state helpers ----------
def _state():
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(s):
    STATE.write_text(json.dumps(s, ensure_ascii=False), encoding="utf-8")


def once(key, fn):
    """Run fn at most once per key. The key is saved only if fn succeeded."""
    s = _state()
    if s.get(key):
        return False
    fn()
    s = _state()
    s[key] = datetime.now(timezone.utc).isoformat()
    _save(s)
    return True


RLM = "‏"  # right-to-left mark: keeps a line RTL even when it starts with an English name


def bullet(text):
    return f"{RLM}• {text}"


def send(text, record=None, buttons=None):
    """Send to the user and keep a short outbox so the chat session can see what he is answering.
    record: what to keep in the outbox instead of text. The main session reads the outbox, so it must not
    contain outside text (mail subjects, other people's calendar titles) that could carry instructions."""
    tg.send_keyboard(text, buttons) if buttons else tg.send(text)
    lines = OUTBOX.read_text(encoding="utf-8").splitlines() if OUTBOX.exists() else ["# הודעות שנשלחו ביוזמת המערכת", ""]
    stamp = datetime.now(hedate.TZ).strftime("%d.%m %H:%M")
    lines.append(f"- {stamp}: " + (record or text).replace(RLM, "").replace("\n", " / ")[:300])
    OUTBOX.write_text("\n".join(lines[:2] + lines[2:][-40:]) + "\n", encoding="utf-8")


# ---------- data ----------
def _tasks_service():
    return gapi.service("tasks", "v1")


def open_tasks():
    items = _tasks_service().tasks().list(tasklist="@default", showCompleted=False, maxResults=100).execute()
    return [t for t in items.get("items", []) if t.get("title")]


def due_on(t):
    return date.fromisoformat(t["due"][:10]) if t.get("due") else None


def week_review(today):
    """Tasks completed in the last 7 days + the most-postponed open tasks."""
    since = (datetime.combine(today - timedelta(days=7), time()).replace(tzinfo=timezone.utc)).isoformat()
    s = _tasks_service()
    done = s.tasks().list(tasklist="@default", showCompleted=True, showHidden=True, completedMin=since,
                          maxResults=100).execute().get("items", [])
    done = [t for t in done if t.get("status") == "completed"]
    postponed = []
    for t in open_tasks():
        for line in (t.get("notes") or "").splitlines():
            if line.strip().startswith("נדחה:"):
                try:
                    postponed.append((int(line.split(":")[1]), t["title"]))
                except ValueError:
                    pass
    postponed.sort(reverse=True)
    return done, postponed[:3]


def mail_by_label(hours=24):
    g = gapi.service("gmail", "v1")
    ids = {l["name"]: l["id"] for l in g.users().labels().list(userId="me").execute()["labels"]}
    out = {}
    for name in LABELS:
        if name not in ids:
            continue
        r = g.users().messages().list(userId="me", labelIds=[ids[name]], q=f"is:unread newer_than:{max(1, hours // 24)}d",
                                      maxResults=5).execute()
        msgs = r.get("messages", [])
        if msgs:
            subs = [_subject(g, m["id"]) for m in msgs[:3]]
            out[name] = (r.get("resultSizeEstimate", len(msgs)), subs)
    return out


def new_mail():
    """Unread primary mail from the last day, labelled or not: (count, first 3 subjects)."""
    g = gapi.service("gmail", "v1")
    r = g.users().messages().list(userId="me", q="is:unread category:primary newer_than:1d", maxResults=10).execute()
    msgs = r.get("messages", [])
    return len(msgs), [_subject(g, m["id"]) for m in msgs[:3]]


def today_events(today):
    """Today's calendar events (read-only), incl. the old system's "[משימה]" events: [(hh:mm or None, title)]."""
    start = datetime.combine(today, time(), hedate.TZ)
    r = gapi.service("calendar", "v3").events().list(
        calendarId="primary", timeMin=start.isoformat(), timeMax=(start + timedelta(days=1)).isoformat(),
        singleEvents=True, orderBy="startTime", maxResults=20).execute()
    out = []
    for e in r.get("items", []):
        dt = e["start"].get("dateTime")
        out.append((datetime.fromisoformat(dt).astimezone(hedate.TZ).strftime("%H:%M") if dt else None,
                    e.get("summary", "(בלי שם)")))
    return out


def _subject(g, mid):
    h = g.users().messages().get(userId="me", id=mid, format="metadata", metadataHeaders=["Subject", "From"]).execute()
    hd = {x["name"]: x["value"] for x in h["payload"].get("headers", [])}
    sender = hd.get("From", "").split("<")[0].strip().strip('"')[:25]
    # sender last, after a Hebrew word: Latin in parentheses at a line end breaks RTL in Telegram
    return redact(f"{hd.get('Subject', '(בלי נושא)')[:50]}, מאת {sender}")


def redact(text):
    """R11: never keep ID/card/account numbers, even from email subjects (e.g. 'ת.ז 3262…')."""
    return re.sub(r"\d[\d\- ]{4,}\d", "[הוסתר]", text)


def bills():
    g = gapi.service("gmail", "v1")
    # labelled receipts/bills, or bill phrases in primary/updates only (a broad search caught marketing and
    # Google security "account" mails in the dry run)
    q = ('newer_than:14d is:unread (category:primary OR category:updates) ("לתשלום" OR "מועד תשלום" OR '
         '"דרישת תשלום" OR "חשבון לתשלום" OR "payment due" OR "amount due")')
    msgs = g.users().messages().list(userId="me", q=q, maxResults=5).execute().get("messages", [])
    return list(dict.fromkeys(_subject(g, m["id"]) for m in msgs))


# ---------- messages ----------
def morning_brief(now):
    today = now.date()
    tasks = open_tasks()
    todays = [t for t in tasks if due_on(t) and due_on(t) <= today]
    lines = [("בוקר טוב " + config.USER_NAME).strip(), f"היום יום {hedate.HEB_DAYS[today.weekday()]}, {today.day}.{today.month}"]
    events = today_events(today)
    cal_tasks = [(h, s.replace("[משימה]", "").strip()) for h, s in events if "[משימה]" in s]
    meetings = [(h, s) for h, s in events if "[משימה]" not in s]
    record = list(lines)  # outbox copy: only the user's own tasks, no outside text
    if todays or cal_tasks:
        lines.append("משימות להיום:")
        items = [("באיחור: " if due_on(t) < today else "") + t["title"] for t in todays[:6]]
        items += [s + (f", בשעה {h}" if h else "") for h, s in cal_tasks[:8]]
        lines += [bullet(x) for x in items]
        record += ["משימות להיום:"] + [f"• {t['title']}" for t in todays[:6]] + [f"• {s}" for h, s in cal_tasks[:8]]
    else:
        lines.append("אין משימות להיום.")
    if meetings:
        lines.append("ביומן:")
        lines += [bullet(s + (f", בשעה {h}" if h else "")) for h, s in meetings[:3]]
        if len(meetings) > 3:
            lines.append(f"ועוד {len(meetings) - 3} אירועים.")
        record.append(f"ביומן: {len(meetings)} אירועים")
    undated = len([t for t in tasks if not due_on(t)])
    if undated:
        lines.append("ועוד משימה אחת בלי תאריך." if undated == 1 else f"ועוד {undated} משימות בלי תאריך.")
    # Up to 3 priorities with numbered quick replies. Tap buttons are a reply keyboard (its text comes back as a
    # normal message; inline buttons would be ignored by the Channels plugin). The list is also kept in the outbox.
    buttons = []
    prio = [("באיחור: " if due_on(t) < today else "") + t["title"] for t in sorted(todays, key=due_on)[:3]]
    prio += [s for h, s in cal_tasks[: 3 - len(prio)]]
    if prio:
        nums = [f"{i}. {x}" for i, x in enumerate(prio, 1)]
        lines += ["הכי חשוב היום:"] + nums + ["אפשר ללחוץ על כפתור, או לכתוב: סיימתי 1."]
        buttons = [f"{v} {i}" for i in range(1, len(prio) + 1) for v in ("סיימתי", "דחה", "טיוטה")]
        record[2:2] = ["הכי חשוב היום:"] + nums
    n, subs = new_mail()
    if n:
        lines.append("מייל חדש:" if n == 1 else ("יש 10 מיילים חדשים או יותר:" if n >= 10 else f"יש {n} מיילים חדשים:"))
        lines += [bullet(x) for x in subs]
        record.append(f"מיילים חדשים: {n}")
    b = bills()
    if b:
        lines.append("חשבוניות לתשלום:")
        lines += [bullet(x) for x in b[:3]]
        record.append(f"חשבוניות לתשלום: {len(b)}")
    if today.weekday() == 6:  # Sunday
        done, postponed = week_review(today)
        lines.append(f"השבוע סגרת {len(done)} משימות 💪" if done else "השבוע עוד לא סגרת משימות.")
        if postponed:
            lines.append("הכי נדחו:")
            lines += [bullet(f"{t}, {n} פעמים") for n, t in postponed]
        record.append(lines[-1] if not postponed else "הכי נדחו: " + ", ".join(t for n, t in postponed))
    send("\n".join(lines), record="\n".join(record), buttons=buttons)


def checkin(now):
    today = now.date()
    tasks = open_tasks()
    todays = [t for t in tasks if due_on(t) and due_on(t) <= today]
    tomorrow = [t for t in tasks if due_on(t) == today + timedelta(days=1)]
    greet = "צהריים טובים" if now.hour < 16 else "ערב טוב"
    lines = [f"{greet} 👋"]
    if todays:
        lines.append("עוד פתוח להיום:")
        lines += [bullet(t["title"]) for t in todays[:3]] + ([f"ועוד {len(todays) - 3}."] if len(todays) > 3 else [])
    if now.hour >= 16 and tomorrow:
        lines.append("מחר:")
        lines += [bullet(t["title"]) for t in tomorrow[:3]]
    if len(lines) == 1:
        return  # nothing open: a check-in with nothing in it is just noise
    lines.append("אפשר לכתוב לי: סיימתי, ואז שם המשימה.")
    send("\n".join(lines))


def important_mail_nudges():
    g = gapi.service("gmail", "v1")
    msgs = g.users().messages().list(userId="me", q="is:important is:unread category:primary newer_than:1d",
                                     maxResults=5).execute().get("messages", [])
    s = _state()
    if "nudged_mail" not in s:  # first run: don't flood him with mail he already has
        s["nudged_mail"] = [m["id"] for m in msgs]
        _save(s)
        return
    seen = set(s.get("nudged_mail", []))
    new = [m["id"] for m in msgs if m["id"] not in seen]
    sent_today = s.get("nudges", {}).get(date.today().isoformat(), 0)
    for mid in new[: max(0, 5 - sent_today)]:  # max 5 nudges a day
        send("מייל חשוב 📧\n" + RLM + _subject(g, mid), record="מייל חשוב 📧 (נושא לא נשמר כאן)")
        sent_today += 1
    s = _state()
    s["nudged_mail"] = list(seen | set(new))[-300:]
    s["nudges"] = {date.today().isoformat(): sent_today}
    _save(s)


def upcoming_send_nudges(now_utc):
    """30 minutes before an approved scheduled send, tell the user so he can still cancel."""
    for j in daemon.load_jobs():
        if j["status"] != "pending" or j["kind"] not in ("email", "whatsapp"):
            continue
        left = (datetime.fromisoformat(j["at"]) - now_utc).total_seconds()
        if 0 < left <= 1800:
            once(f"presend-{j['id']}", lambda j=j: send(
                f"בעוד חצי שעה אשלח את זה:\n{RLM}{j['text']}\nכדי לבטל, כתוב: בטל שליחה"))


# ---------- approved scheduled sends ----------
@daemon.runner("email")
def _send_email(job):
    """The user approved this exact draft + time (permission prompt on schedule_email). Send it, then confirm."""
    p = job["payload"]
    g = gapi.service("gmail", "v1")
    if gapi.draft_snapshot(p["draft_id"])["sha"] != p.get("sha"):  # byte-for-byte what he approved
        raise RuntimeError("הטיוטה השתנתה מאז שאישרת, אז לא שלחתי")
    g.users().drafts().send(userId="me", body={"id": p["draft_id"]}).execute()
    send(f"שלחתי את המייל ✅\nאל: {p['to']}\nנושא: {p['subject']}")


@daemon.runner("whatsapp")
def _send_whatsapp(job):
    """Approved WhatsApp message: wake the Jarvis session over the local bus; it sends via Chrome and reports back."""
    from . import bus
    if not bus.post(f"run job {job['id']}"):
        raise RuntimeError("ג'רוויס לא זמין כרגע")
    return "dispatched"


# ---------- weekly memory digest, evening summary, cheap rule scan (no model unless something fires) ----------
DIGEST_AT, EVENING_AT = time(20, 0), time(21, 0)   # digest: Sundays; both have a 2 h catch-up window
PREFS = config.PROJECT / "memory" / "preferences.md"
ONGOING = config.PROJECT / "memory" / "ongoing.md"
OPEN_THREADS = config.PROJECT / "memory" / "open_threads.md"
MAX_EVENTS_PER_DAY = 6
WORK_QUIET_MIN = 30   # a work session started this recently is probably still running
UNI_QUERY = config.WATCH_QUERY
_ID = re.compile(r"^[\w.-]{1,80}$")


def _in_window(now, start, hours=2):
    return start <= now.time() < (datetime.combine(now.date(), start) + timedelta(hours=hours)).time()


def evening_off():
    try:
        return "בלי סיכום ערב" in PREFS.read_text(encoding="utf-8")
    except OSError:
        return False


def build_evening(done_titles, jobs_today, ongoing_lines):
    """Pure: the 'what I did today' message, or '' when nothing happened. Max 3 lines + 'ועוד N'.
    jobs_today: [{'kind','text'}] created today; ongoing_lines: today's lines from memory/ongoing.md."""
    items = [f"סגרתי: {t}" for t in done_titles]
    items += [f"קבעתי תזכורת: {j['text']}" for j in jobs_today if j["kind"] == "reminder"]
    items += [f"תזמנתי שליחה: {j['text']}" for j in jobs_today if j["kind"] in ("email", "whatsapp")]
    items += [l.lstrip("- ").split(":", 1)[-1].strip() for l in ongoing_lines]
    items = [i for i in items if i.strip()]
    if not items:
        return ""
    lines = ["מה עשיתי היום 🌙"] + [bullet(i[:80]) for i in items[:3]]
    if len(items) > 3:
        lines.append(f"ועוד {len(items) - 3}.")
    return "\n".join(lines)


def evening_summary(now):
    if evening_off():
        return
    day = now.date()
    since = datetime.combine(day, time(), hedate.TZ).astimezone(timezone.utc)
    done = _tasks_service().tasks().list(tasklist="@default", showCompleted=True, showHidden=True,
                                         completedMin=since.isoformat(), maxResults=100).execute().get("items", [])
    done = [t["title"] for t in done if t.get("status") == "completed" and t.get("title")]
    jobs = [j for j in daemon.load_jobs()
            if j["status"] != "cancelled" and datetime.fromisoformat(j["created"]) >= since]
    stamp = f"- {day.day}.{day.month}:"
    try:
        ongoing = [l for l in ONGOING.read_text(encoding="utf-8").splitlines() if l.startswith(stamp)]
    except OSError:
        ongoing = []
    text = build_evening(done, jobs, ongoing)
    if text:
        send(text, record="סיכום ערב: מה ג'רוויס עשה היום")


def _work_running(now_utc):
    s = _state()
    for k, v in s.items():
        if k.startswith("work-"):
            try:
                if 0 <= (now_utc - datetime.fromisoformat(v)).total_seconds() < WORK_QUIET_MIN * 60:
                    return True
            except (TypeError, ValueError):
                pass
    return False


def scan_candidates(now_utc):
    """Plain Gmail/Calendar checks. Returns [(kind, id)] of things that may deserve a model turn (ids only)."""
    g = gapi.service("gmail", "v1")
    out = [("mail", m["id"]) for m in g.users().messages().list(userId="me", q=UNI_QUERY, maxResults=5)
           .execute().get("messages", [])]
    me = config.user_email().lower()
    watch = {t["id"] for t in g.users().threads().list(userId="me", q="in:sent newer_than:14d", maxResults=15)
             .execute().get("threads", [])}
    try:
        watch |= set(re.findall(r"\b[0-9a-f]{16}\b", OPEN_THREADS.read_text(encoding="utf-8")))
    except OSError:
        pass
    counts = _state().get("thread_n", {})
    new_counts, replies = {}, []
    for tid in watch:
        try:
            msgs = g.users().threads().get(userId="me", id=tid, format="metadata", metadataHeaders=["From"]).execute()["messages"]
        except Exception:  # a deleted/unknown thread id must not stop the scan
            continue
        last_from = {h["name"].lower(): h["value"] for h in msgs[-1]["payload"].get("headers", [])}.get("from", "")
        new_counts[tid] = len(msgs)
        if tid in counts and len(msgs) > counts[tid] and me not in last_from.lower():
            replies.append(("reply", tid))
    s = _state()
    s["thread_n"] = new_counts
    _save(s)
    out += replies
    end = now_utc + timedelta(hours=2)
    ev = gapi.service("calendar", "v3").events().list(
        calendarId="primary", timeMin=now_utc.isoformat(), timeMax=end.isoformat(), singleEvents=True,
        orderBy="startTime", maxResults=10).execute().get("items", [])
    out += [("due", e["id"]) for e in ev if "[משימה]" in e.get("summary", "") and e.get("start", {}).get("dateTime")]
    return out


def rule_scan(now_utc):
    """Every 5 min, 07-23. Posts `run event <kind> <id>` (ids only) when a rule fires; deduped, 6 a day, quiet during work sessions."""
    day = now_utc.astimezone(hedate.TZ).date().isoformat()
    if _work_running(now_utc):
        return
    s = _state()
    first = "scan_seen" not in s
    seen = set(s.get("scan_seen", []))
    cands = [c for c in scan_candidates(now_utc) if _ID.match(c[1]) and f"{c[0]}:{c[1]}" not in seen]
    if first:  # first run: whatever already exists is not news
        s = _state()
        s["scan_seen"] = [f"{k}:{i}" for k, i in cands]
        _save(s)
        return
    for kind, i in cands:
        s = _state()
        n = s.get("scan_count", {}).get(day, 0)
        if n >= MAX_EVENTS_PER_DAY:
            return
        try:
            bus.post(f"run event {kind} {i}")
        except bus.BusDown:
            return  # not marked seen: retried on the next scan
        s = _state()
        s["scan_seen"] = (s.get("scan_seen", []) + [f"{kind}:{i}"])[-500:]
        s["scan_count"] = {day: n + 1}
        _save(s)


# ---------- the loop hook ----------
_last_mail_check = [datetime.min.replace(tzinfo=timezone.utc)]
_last_scan = [datetime.min.replace(tzinfo=timezone.utc)]


def tick(now_utc=None):
    now_utc = now_utc or datetime.now(timezone.utc)
    now = now_utc.astimezone(hedate.TZ)
    day = now.date().isoformat()
    try:
        if BRIEF_AT <= now.time() < LATEST_BRIEF:
            once(f"brief-{day}", lambda: morning_brief(now))
        for t in CHECKINS:
            if t <= now.time() < (datetime.combine(now.date(), t) + timedelta(hours=2)).time():
                once(f"checkin-{day}-{t.hour}", lambda: checkin(now))
        for t in WORK_SESSIONS:
            if t <= now.time() < (datetime.combine(now.date(), t) + timedelta(hours=2)).time():
                # post() raises if the session is down, so `once` retries until Jarvis is back (within 2 h)
                try:
                    once(f"work-{day}-{t.hour}", lambda: bus.post("run work session"))
                except bus.BusDown:
                    pass  # session not up yet; the rest of tick still runs, retried next tick
        if (now_utc - _last_mail_check[0]).total_seconds() >= 300 and 7 <= now.hour < 23:
            _last_mail_check[0] = now_utc
            important_mail_nudges()
        upcoming_send_nudges(now_utc)
        if now.weekday() == 6 and _in_window(now, DIGEST_AT):
            try:
                once(f"digest-{day}", lambda: bus.post("run memory digest"))
            except bus.BusDown:
                pass  # retried next tick within the 2 h window
        if _in_window(now, EVENING_AT):
            once(f"evening-{day}", lambda: evening_summary(now))
        if (now_utc - _last_scan[0]).total_seconds() >= 300 and 7 <= now.hour < 23:
            _last_scan[0] = now_utc
            rule_scan(now_utc)
    except gapi.GoogleAuthError:
        once(f"google-down-{day}", lambda: tg.send(
            "גוגל ניתק אותי. אין לי גישה למשימות ולמייל.\nליד המחשב: פתח את הקיצור \"חבר מחדש את גוגל\" "
            "בשולחן העבודה.\nהתזכורות ממשיכות לעבוד."))
    google_relogin_check(now_utc)


def google_relogin_check(now=None):
    # The Google app stays in "Testing" (publishing needs a verified website), so Google drops the token after 7 days.
    now = now or datetime.now(timezone.utc)
    try:
        login = datetime.fromisoformat(GOOGLE_LOGIN.read_text().strip())
    except Exception:
        return
    if now - login > timedelta(days=6):
        once(f"relogin-{login.date()}", lambda: tg.send(
            "מחר גוגל מנתק אותי, זה קורה כל שבוע.\nליד המחשב: פתח את הקיצור "
            "\"חבר מחדש את גוגל\" בשולחן העבודה ולחץ Continue."))


def net_back(since_utc, now_utc=None):
    """One line after an outage of 15+ minutes, naming what was skipped because of it."""
    now_utc = now_utc or datetime.now(timezone.utc)
    if (now_utc - since_utc).total_seconds() < 900:
        return
    since, now = since_utc.astimezone(hedate.TZ), now_utc.astimezone(hedate.TZ)
    s, day = _state(), now.date().isoformat()
    windows = [(f"brief-{day}", LATEST_BRIEF, "הודעת הבוקר")]
    windows += [(f"checkin-{day}-{t.hour}", time(t.hour + 2), f"הבדיקה של {t.strftime('%H:%M')}") for t in CHECKINS]
    windows += [(f"work-{day}-{t.hour}", time(t.hour + 2), f"שעת העבודה של {t.strftime('%H:%M')}") for t in WORK_SESSIONS]
    missed = [name for key, end, name in windows
              if not s.get(key) and since.date() == now.date() and since.time() < end <= now.time()]
    start = since.strftime("%H:%M") if since.date() == now.date() else hedate.say(since, now)
    lines = [f"הייתי בלי אינטרנט מ־{start} עד {now.strftime('%H:%M')}."]
    if missed:
        lines.append("פספסתי בגלל זה: " + ", ".join(missed) + ".")
    if any(j["status"] == "pending" and datetime.fromisoformat(j["at"]) <= now_utc for j in daemon.load_jobs()):
        lines.append("תזכורות שהיו בזמן הזה מגיעות עכשיו.")
    send("\n".join(lines))


daemon.CHECKS.append(tick)
daemon.NET_BACK.append(net_back)
