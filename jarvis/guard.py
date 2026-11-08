"""PreToolUse hook: hard safety rules + narrow learned permissions (R5, R9, R10, R12).

Output contract: print a hookSpecificOutput JSON with permissionDecision allow/deny/ask, or print nothing
to fall through to the normal Claude Code permission rules. Fails CLOSED: any crash exits 2 (= block).
"""
import io
import json
import os
import re
import sys
from datetime import date
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
LEARNED = PROJECT / "permissions" / "learned.json"
OPUS_CAP = 5
NEVER = re.compile(r"pay|card|credit|cvv|password|passwd|credential|token|otp|captcha|bank|"
                   r"תשלום|אשראי|כרטיס|סיסמ|קוד אימות|בנק", re.I)
URL_OR_EMAIL = re.compile(r"https?://|www\.|[\w.+-]+@[\w-]+\.[\w.]+", re.I)


def _home():
    return Path(os.environ.get("JARVIS_HOME", Path.home() / ".jarvis"))


def _inside(p, *roots):
    try:
        rp = Path(p).expanduser().resolve()
    except Exception:
        return False
    return any(rp.is_relative_to(r.resolve()) for r in roots)


# Untrusted content is only read by agents that have no way to send anything (R12)
ONLY_FOR = {
    "WebFetch": {"web-reader", "opus-advisor", "sonnet-escalation"},
    **{f"mcp__google__{t}": {"email-reader", "email-labeler"} for t in (
        "search_gmail_messages", "get_gmail_message_content", "get_gmail_messages_content_batch",
        "get_gmail_thread_content", "get_gmail_threads_content_batch", "get_gmail_attachment_content")},
    **{f"mcp__google__{t}": {"email-labeler"} for t in (
        "modify_gmail_message_labels", "batch_modify_gmail_message_labels")},
    # event titles/descriptions can come from strangers' invites
    "mcp__google__get_events": {"calendar-reader"},
}


def _due_whatsapp(home):
    """True if jarvisd dispatched an approved WhatsApp job in the last 10 minutes."""
    from datetime import datetime, timezone
    try:
        jobs = json.loads((home / "data" / "jobs.json").read_text(encoding="utf-8"))
    except Exception:
        return False
    now = datetime.now(timezone.utc)
    return any(j["kind"] == "whatsapp" and j["status"] == "dispatched" and j.get("ran")
               and (now - datetime.fromisoformat(j["ran"])).total_seconds() < 600 for j in jobs)


def _event_has_guests(calendar_id, event_id):
    """True if the event has attendees or isn't the user's own, or if we can't tell (fail safe = ask)."""
    try:
        from . import gapi
        e = gapi.service("calendar", "v3").events().get(calendarId=calendar_id, eventId=event_id).execute()
        return bool(e.get("attendees")) or not e.get("organizer", {}).get("self", False)
    except Exception:
        return True


def _own_task_event(event_id):
    """True only if the event is a "[משימה]" event with no attendees that the user owns (organizer or creator is him).
    Any doubt or lookup failure = False (the caller then falls back to the stricter check)."""
    try:
        from . import gapi
        e = gapi.service("calendar", "v3").events().get(calendarId="primary", eventId=event_id).execute()
        mine = e.get("organizer", {}).get("self", False) or e.get("creator", {}).get("self", False)
        return "[משימה]" in e.get("summary", "") and not e.get("attendees") and bool(mine)
    except Exception:
        return False


# --- browser: approval is per site (registrable domain). Only the user's tap can add a site:
# the first navigate to a new host asks; the PostToolUse hook (record_browser) runs only after he tapped Allow.
_THREE_LABEL = {"co", "com", "org", "net", "ac", "gov", "edu", "muni"}
_HOSTING = {"github.io", "vercel.app", "netlify.app", "blogspot.com", "herokuapp.com", "pages.dev",
            "web.app", "firebaseapp.com", "wixsite.com", "weebly.com"}
_CHECKOUT = re.compile(r"checkout|billing|payment|/cart|סל.?קניות|תשלום", re.I)
_NAVIGATING = ("left_click", "double_click", "triple_click", "right_click", "left_click_drag", "key")


def urlsplit_host(url):
    """Exact host of an http(s) URL (no scheme = https), or ''."""
    from urllib.parse import urlsplit
    u = str(url).strip()
    if not re.match(r"[a-z][a-z0-9+.-]*:", u, re.I):
        u = "https://" + u
    try:
        s = urlsplit(u)
        host = (s.hostname or "").lower().rstrip(".")
    except ValueError:
        return ""
    return host if s.scheme in ("http", "https") else ""


def _site_of(url):
    """Registrable domain of a URL ('www.zap.co.il' -> 'zap.co.il'), or '' if not a plain http(s) URL."""
    host = urlsplit_host(url)
    if not host:
        return ""
    labels = host.split(".")
    if len(labels) <= 2 or re.fullmatch(r"[\d.]+", host):
        return host
    if ".".join(labels[-2:]) in _HOSTING:
        return ".".join(labels[-3:])
    if len(labels[-1]) == 2 and labels[-2] in _THREE_LABEL:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def _jload(f, default):
    try:
        return json.loads(Path(f).read_text(encoding="utf-8"))
    except Exception:
        return default


def _jsave(f, obj):
    f = Path(f)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")


def _tab_urls(resp):
    """{tabId: url} from a tabs_context response (looks only at structured "url" keys, never page text)."""
    out = {}

    def walk(x, depth=0):
        if depth > 6:
            return
        if isinstance(x, str):
            try:
                walk(json.loads(x), depth + 1)
            except Exception:
                pass
        elif isinstance(x, list):
            for i in x:
                walk(i, depth + 1)
        elif isinstance(x, dict):
            if isinstance(x.get("url"), str):
                out[str(x.get("tabId", x.get("id", "")))] = x["url"]
            for v in x.values():
                if isinstance(v, (dict, list)) or (isinstance(v, str) and v[:1] in "[{"):
                    walk(v, depth + 1)
    walk(resp)
    return out


def record_browser(tool, inp, resp, home):
    """PostToolUse: runs only if the tool actually ran (= the user tapped Allow, or the guard allowed it)."""
    tabs_f, sites_f = home / "data" / "browser_tabs.json", home / "data" / "approved_sites.json"
    tabs = _jload(tabs_f, {})
    tab = str(inp.get("tabId", ""))
    if tool.endswith("__navigate"):
        url = str(inp.get("url", "")).strip()
        site = _site_of(url)
        if site and not url.lower() in ("back", "forward"):
            sites = _jload(sites_f, {})
            # WhatsApp has its own rule; checkout/credential-looking URLs ask every time and are never saved
            if urlsplit_host(url) != "web.whatsapp.com" and site not in sites and not (_CHECKOUT.search(url) or NEVER.search(url)):
                sites[site] = date.today().isoformat()
                _jsave(sites_f, sites)
            tabs[tab] = {"host": site, "fresh": True}
        else:
            tabs[tab] = {"host": "", "fresh": False}
    elif tool.endswith("__tabs_context_mcp"):
        for t, u in _tab_urls(resp).items():
            tabs[t] = {"host": _site_of(u), "fresh": True}
    elif tool.endswith("__tabs_create_mcp"):
        tabs[tab] = {"host": "", "fresh": False}
    elif tool.endswith("__computer") and (inp.get("action") in _NAVIGATING
                                          or (inp.get("action") == "type" and "\n" in str(inp.get("text", "")))):
        if tab in tabs:
            tabs[tab]["fresh"] = False  # a click may have left the site; tabs_context must confirm
    _jsave(tabs_f, tabs)


def _browser_site_check(tool, inp, home):
    """Decision for acting (click/type/form_input) inside the page: allowed only on an approved, confirmed site."""
    t = _jload(home / "data" / "browser_tabs.json", {}).get(str(inp.get("tabId", "")))
    if not t or not t.get("host"):
        return "ask", "מבקש אישור לפעול באתר לא מזוהה"
    if t["host"] not in _jload(home / "data" / "approved_sites.json", {}):
        return "ask", f"מבקש אישור לפעול באתר {t['host']}"
    if not t.get("fresh"):
        return "ask", f"מבקש אישור להמשיך באתר {t['host']} (לא בטוח שעדיין שם)"
    return "allow", f"האתר {t['host']} אושר."


_DESC_ONLY_OK = {"action", "event_id", "description", "send_updates", "calendar_id", "user_google_email"}


def decide(tool, inp, home=None, agent_type=None):
    """Return (decision, reason) or None to fall through."""
    home = home or _home()
    if tool.startswith("mcp__claude-in-chrome__"):
        # web pages are untrusted: only the browser-worker agent (no mail, no Telegram) touches Chrome
        if agent_type != "browser-worker":
            return "deny", "Browser tools are only for the browser-worker agent; delegate to it."
        if tool.endswith("__computer") and inp.get("action") in ("wait", "screenshot", "zoom", "scroll", "scroll_to"):
            return "allow", "Looking is safe; clicks and typing still ask the user."
        if tool.endswith("__browser_batch"):
            return "deny", "Use single actions so each click can be approved."
        if tool.endswith(("__file_upload", "__upload_image")):
            return "ask", "מבקש אישור להעלות קובץ לאתר"
        if tool.endswith("__javascript_tool"):
            return "ask", "מבקש אישור להריץ קוד בתוך האתר"
        if tool.endswith("__navigate"):
            url = str(inp.get("url", "")).strip()
            if url.lower() in ("back", "forward"):
                return _browser_site_check(tool, inp, home)
            site = _site_of(url)
            if urlsplit_host(url) == "web.whatsapp.com":
                return "allow", "WhatsApp Web."
            if not site:
                return "ask", "מבקש אישור לפתוח כתובת לא רגילה בדפדפן"
            if _CHECKOUT.search(url) or NEVER.search(url):
                return "ask", f"מבקש אישור להיכנס לעמוד תשלום או התחברות ב־{site}"
            # The user chose full auto mode (14.10): other sites go to the auto-mode classifier, no prompt
            return None
        if (tool.endswith("__computer") and inp.get("action") == "key"
                and str(inp.get("text", "")).lower() in ("enter", "return") and _due_whatsapp(home)):
            return "allow", "Enter for an approved, due WhatsApp job."
        if tool.endswith(("__computer", "__form_input")):
            # an "ask" settings rule would override a hook "allow", so the guard decides every action
            if NEVER.search(json.dumps(inp, ensure_ascii=False)):
                return "ask", "מבקש אישור: נראה שזה קשור לתשלום, סיסמה או פרטים רגישים"
            return _browser_site_check(tool, inp, home)
    if tool in ONLY_FOR and agent_type not in ONLY_FOR[tool]:
        return "deny", f"{tool} is only for the {', '.join(sorted(ONLY_FOR[tool]))} agent(s); delegate to it."
    inbox, data = home / "claude" / "channels" / "telegram" / "inbox", home / "data"
    fake = Path.home() / ".claude" / "channels" / "fakechat" / "inbox"  # local E2E test UI
    blob = json.dumps(inp, ensure_ascii=False)

    # --- Google: never delete tasks, never archive mail, never invite people, never edit his events
    if tool == "mcp__google__manage_task" and inp.get("action") == "delete":
        return "deny", "Tasks are never deleted; mark completed instead."
    if tool in ("mcp__google__modify_gmail_message_labels", "mcp__google__batch_modify_gmail_message_labels"):
        removed = inp.get("remove_label_ids") or inp.get("remove_labels") or []
        if removed or "TRASH" in blob.upper():
            return "deny", "Labels can only be added: never archive, mark read or trash mail."
    if tool == "mcp__google__manage_event":
        action = str(inp.get("action", "create")).strip().lower()
        if (inp.get("attendees") or inp.get("attachments") or inp.get("send_updates", "all") != "none"
                or action == "rsvp"):
            return "deny", "Calendar events must have no attendees and send_updates='none' (invites send email)."
        if inp.get("calendar_id", "primary") != "primary":
            return "deny", "Only the user's main calendar (calendar_id='primary')."
        risky = [k for k in ("add_google_meet", "conference_data", "conference_provider", "conference_uri",
                             "conference_passcode", "conference_id", "guests_can_modify", "guests_can_invite_others",
                             "guests_can_see_other_guests") if inp.get(k)]
        if risky or str(inp.get("visibility") or "default").lower() not in ("default", "private"):
            return "deny", "No meeting links, guest options or public events."
        if action == "update":
            # The user approved: his own events (nobody else on them) may be edited; others would get an update
            # Description-only notes on his own "[משימה]" events never need a question (work sessions run unattended)
            if set(inp) <= _DESC_ONLY_OK and _own_task_event(inp.get("event_id", "")):
                return "allow", "Adding notes to the user's own task event."
            if _event_has_guests("primary", inp.get("event_id", "")):
                title = inp.get("summary") or inp.get("title") or ""
                return "ask", "מבקש אישור לשנות אירוע שיש בו עוד אנשים" + (f": {title}" if title else "")
            return "allow", "Editing the user's own calendar event."
        if action != "create":
            # The user: no approval for his own schedule; ask only when other people are on the event.
            # Google keeps deleted events in the trash for 30 days.
            if action == "delete" and not _event_has_guests("primary", inp.get("event_id", "")):
                return "allow", "Deleting the user's own event (nobody else on it)."
            return "ask", "מבקש אישור למחוק אירוע שיש בו עוד אנשים"
        return "allow", "New event without guests."
    if tool == "mcp__google__draft_gmail_message":
        for a in inp.get("attachments") or []:
            if not isinstance(a, dict) or a.get("url") or not _inside(a.get("path", ""), inbox, data):
                return "deny", "Draft attachments only from the Telegram inbox (no URLs, no other files)."

    # --- files leaving the machine or being changed
    if tool == "mcp__plugin_telegram_telegram__reply":
        for f in inp.get("files") or []:
            if not _inside(f, inbox, data, PROJECT / "out"):
                return "deny", "Only files from the inbox/data/out folders may be sent."
    if tool == "mcp__jarvis__transcribe_he" and not _inside(inp.get("path", ""), inbox, data, fake):
        return "deny", "Only voice files from the Telegram inbox can be transcribed."
    if tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        path = inp.get("file_path", "")
        if _inside(path, data):  # approved sites are proven only by the user's tap, never written by the model
            return "deny", "Files in the Jarvis data folder (approved_sites.json) can't be edited by the model."
        if _inside(path, PROJECT / "memory"):
            text = " ".join(str(inp.get(k, "")) for k in ("content", "new_string"))
            text += " ".join(str(e.get("new_string", "")) for e in inp.get("edits") or [] if isinstance(e, dict))
            if URL_OR_EMAIL.search(text) or NEVER.search(text):
                return "ask", "מבקש אישור לשמור בזיכרון משהו שיש בו קישור, כתובת או פרט רגיש"

    if tool in ("Agent", "Task") and str(inp.get("subagent_type", "")).startswith("dev-"):
        return "deny", "dev-* agents are for building Jarvis, not for running it."

    # --- Opus is capped at 5 calls/day (agent type or model override)
    if tool in ("Agent", "Task") and ("opus" in str(inp.get("subagent_type", "")) or "opus" in str(inp.get("model", ""))):
        f = home / "data" / "opus_count.json"
        try:
            c = json.loads(f.read_text())
        except Exception:
            c = {}
        today = date.today().isoformat()
        if c.get(today, 0) >= OPUS_CAP:
            return "deny", f"Opus daily cap ({OPUS_CAP}) reached; answer without Opus and tell the user."
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({today: c.get(today, 0) + 1}))

    # --- narrow rules the user approved ("להוסיף לרשימת המותר?")
    if NEVER.search(blob) or inp.get("attachments") or inp.get("attendees"):
        return None  # money/credential-looking or extra-payload actions always take the normal (asking) path
    try:
        rules = json.loads(LEARNED.read_text(encoding="utf-8"))
    except Exception:
        rules = []
    for r in rules:
        if r.get("tool") != tool or NEVER.search(json.dumps(r, ensure_ascii=False)):
            continue
        m = r.get("match") or {}
        if m and all(str(inp.get(k, "")).strip().lower() == str(v).strip().lower() for k, v in m.items()):
            return "allow", f"learned rule: {r.get('why', '')}"
    return None


def main():
    ev = json.load(io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8"))  # Windows stdin is cp1255 otherwise
    if ev.get("hook_event_name") == "PostToolUse":
        if str(ev.get("tool_name", "")).startswith("mcp__claude-in-chrome__"):
            record_browser(ev["tool_name"], ev.get("tool_input") or {}, ev.get("tool_response"), _home())
        return
    d = decide(ev.get("tool_name", ""), ev.get("tool_input") or {}, agent_type=ev.get("agent_type"))
    if d:
        sys.stdout.reconfigure(encoding="utf-8")
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                                 "permissionDecision": d[0], "permissionDecisionReason": d[1]}}))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # fail closed
        print(f"guard error: {e}", file=sys.stderr)
        sys.exit(2)
