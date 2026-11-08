"""Local event bus into the running Jarvis session, over the official fakechat channel (127.0.0.1 only).

Custom channels need --dangerously-load-development-channels plus an interactive dialog, which can't survive
unattended restarts; fakechat is on the official allowlist. Only three messages are meaningful (see CLAUDE.md):
  ping <nonce>      -> Jarvis answers "pong <nonce>" (watchdog health check, end to end through the model)
  run job <id>      -> Jarvis fetches the APPROVED job from jarvisd and does exactly that
  run work session  -> Jarvis advances open tasks on its own (no sends; same rules as always)
Known limit: the bus has no key, so any local process (or a web page posting to 127.0.0.1) can send these three.
None of them can make Jarvis send or change anything beyond what the user already approved.
Anything else arriving on this channel is ignored by Jarvis, so a local process can't use it to give orders.

python -m jarvis.bus ping   -> exit 0 if Jarvis answered within the timeout
"""
import asyncio
import glob
import json
import os
import re
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import aiohttp

URL = f"http://127.0.0.1:{os.environ.get('JARVIS_BUS_PORT', '8787')}"
RETRY_DELAYS = (1, 3)  # seconds between connection attempts; ~4 s total (the daemon loop retries every 20 s anyway), then BusDown


class BusDown(ConnectionError):
    """The bus port refuses/times out connections: the session (or its fakechat plugin) isn't up. Nothing was delivered."""


async def _send(text, expect=None, timeout=120):
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout + 30)) as s:
            ws = await s.ws_connect(URL + "/ws")
            try:
                form = aiohttp.FormData()
                form.add_field("id", "sys" + uuid.uuid4().hex[:8])
                form.add_field("text", text)
                await s.post(URL + "/upload", data=form)
                if not expect:
                    return True
                loop = asyncio.get_running_loop()
                end = loop.time() + timeout
                while loop.time() < end:
                    try:
                        m = await ws.receive(timeout=end - loop.time())
                    except asyncio.TimeoutError:
                        break
                    if m.type == aiohttp.WSMsgType.TEXT and expect in m.data:
                        return True
                    if m.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        break
                return False
            finally:
                await ws.close()
    except (aiohttp.ClientConnectorError, aiohttp.WSServerHandshakeError, ConnectionError, OSError) as e:
        raise BusDown(f"bus at {URL} unreachable: {type(e).__name__}") from None


def _retrying(make, delays):
    """Run make() (a coroutine factory); retry only BusDown, with backoff. Returns the result or raises BusDown."""
    for d in (*delays, None):
        try:
            return asyncio.run(make())
        except BusDown:
            if d is None:
                raise
            time.sleep(d)


def post(text, delays=None):
    """Deliver text to the session. Raises BusDown (a clean one-line error, no traceback needed) if it can't."""
    return _retrying(lambda: _send(text), RETRY_DELAYS if delays is None else delays)


def ping_status(timeout=120, delays=(2, 5)):
    """'ok' = pong received; 'hung' = bus reachable but the model didn't answer; 'down' = bus unreachable."""
    nonce = uuid.uuid4().hex[:6]
    try:
        ok = _retrying(lambda: _send(f"ping {nonce}", expect=f"pong {nonce}", timeout=timeout), delays)
    except BusDown:
        return "down"
    except Exception:
        return "hung"
    return "ok" if ok else "hung"


def ping(timeout=120):
    return ping_status(timeout) == "ok"


# ---- transcript analysis (used by watchdog.ps1: hang / idle detection and "never lose a message silently") ----
# Claude Code appends {"type":"queue-operation",...} lines whenever a channel message is enqueued, even when the
# session is stuck on a permission prompt. They are NOT activity. Only user/assistant lines carrying a message are.
HUNG_QUEUE_MIN = 5      # a message enqueued this long ago with no turn after it = hung
HUNG_TOOL_MIN = 10      # tool_use without tool_result this long (while something is queued) = hung
IDLE_MIN = 2
_ATTR = re.compile(r'(\w+)="([^"]*)"')
_IL = ZoneInfo("Asia/Jerusalem")


def _ts(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def _is_turn(d):
    return d.get("type") in ("user", "assistant") and isinstance(d.get("message"), dict) and d["message"].get("content")


def _kind(attrs):
    k = attrs.get("attachment_kind")
    if not k:
        return "טקסט"
    return {"voice": "הודעה קולית", "audio": "הודעה קולית", "photo": "תמונה"}.get(k, "קובץ")


def analyze_transcript(lines, now=None):
    """lines: iterable of raw JSONL strings. Returns a dict:
    last_turn (datetime|None: newest real user/assistant line), unprocessed (list of enqueued channel messages that no
    turn ever picked up: {ts, source, id, kind}), open_tool_min (age in minutes of the oldest tool_use without a
    tool_result, or None), hung (bool), idle (bool), reason."""
    now = now or datetime.now(timezone.utc)
    last_turn = None
    enq = []            # (ts, attrs, raw content)
    seen_text = []      # raw text of every non-queue line (a message id appearing there = it was delivered to the model)
    tool_open = {}      # tool_use id -> ts
    for raw in lines:
        try:
            d = json.loads(raw)
        except Exception:
            continue
        if not isinstance(d, dict):
            continue
        t = _ts(d.get("timestamp") or "")
        if d.get("type") == "queue-operation":
            if d.get("operation") == "enqueue" and t:
                m = re.match(r"\s*<channel\s+([^>]*)>", d.get("content") or "")
                enq.append((t, dict(_ATTR.findall(m.group(1))) if m else {}, d.get("content") or ""))
            continue
        seen_text.append((t, raw))
        if not _is_turn(d) or not t:
            continue
        if last_turn is None or t > last_turn:
            last_turn = t
        c = d["message"]["content"]
        if isinstance(c, list):
            for b in c:
                if not isinstance(b, dict):
                    continue
                if d["type"] == "assistant" and b.get("type") == "tool_use":
                    tool_open[b.get("id")] = t
                elif b.get("type") == "tool_result":
                    tool_open.pop(b.get("tool_use_id"), None)
    unprocessed = []
    for t, attrs, _content in enq:
        mid = attrs.get("message_id")
        done = any(mid and (lt is None or lt >= t) and f'message_id=\\"{mid}\\"' in raw for lt, raw in seen_text)
        if not done:
            unprocessed.append({"ts": t, "source": attrs.get("source", ""), "id": mid, "kind": _kind(attrs)})
    open_tool_min = None
    if tool_open:
        open_tool_min = (now - min(tool_open.values())).total_seconds() / 60
    hung, reason = False, ""
    for u in unprocessed:
        if (now - u["ts"]).total_seconds() / 60 > HUNG_QUEUE_MIN and (last_turn is None or last_turn < u["ts"]):
            hung, reason = True, f"message queued {u['ts'].isoformat()} with no turn after it"
            break
    if not hung and unprocessed and open_tool_min is not None and open_tool_min > HUNG_TOOL_MIN:
        hung, reason = True, f"tool_use without result for {open_tool_min:.0f} min while messages are queued"
    idle = (not unprocessed and open_tool_min is None
            and (last_turn is None or (now - last_turn).total_seconds() / 60 >= IDLE_MIN))
    return {"last_turn": last_turn, "unprocessed": unprocessed, "open_tool_min": open_tool_min,
            "hung": hung, "idle": idle, "reason": reason}


def lost_notice(unprocessed):
    """One Hebrew Telegram message about Telegram messages that were never handled (no content quoted), or ''."""
    tg_msgs = sorted((u for u in unprocessed if "telegram" in u["source"]), key=lambda u: u["ts"])
    if not tg_msgs:
        return ""
    shown = tg_msgs[:3]
    what = ", ".join(f"{u['ts'].astimezone(_IL).strftime('%H:%M')} ({u['kind']})" for u in shown)
    more = f" ועוד {len(tg_msgs) - 3}" if len(tg_msgs) > 3 else ""
    if len(shown) == 1:
        u = shown[0]
        return f"התאפסתי ולא טיפלתי בהודעה ששלחת ב־{u['ts'].astimezone(_IL).strftime('%H:%M')} ({u['kind']}). אפשר לשלוח שוב?"
    return f"התאפסתי ולא טיפלתי בהודעות ששלחת: {what}{more}. אפשר לשלוח שוב?"


def current_transcript(claude_dir=None, since=None):
    """Newest main-session transcript written after `since` (epoch seconds, the session start).
    A just-restarted session has no transcript yet; without `since` the old (maybe stuck) one would be judged."""
    d = claude_dir or os.path.join(os.path.expanduser("~"), ".jarvis", "claude")
    files = [f for f in glob.glob(os.path.join(d, "projects", "**", "*.jsonl"), recursive=True)
             if "subagents" not in f.replace("\\", "/").split("/")]
    if since is not None:
        files = [f for f in files if os.path.getmtime(f) >= since]
    return max(files, key=os.path.getmtime) if files else None


def transcript_status(path=None, now=None, since=None):
    path = path or current_transcript(since=since)
    if not path:
        return {"found": False, "hung": False, "idle": True, "unprocessed": 0, "notice": "", "reason": "", "last_turn_age_min": None}
    with open(path, encoding="utf-8", errors="replace") as f:
        r = analyze_transcript(f, now)
    now = now or datetime.now(timezone.utc)
    return {"found": True, "hung": r["hung"], "idle": r["idle"], "unprocessed": len(r["unprocessed"]),
            "notice": lost_notice(r["unprocessed"]), "reason": r["reason"],
            "last_turn_age_min": None if r["last_turn"] is None else round((now - r["last_turn"]).total_seconds() / 60, 1)}


if __name__ == "__main__" and sys.argv[1:] == ["ping"]:
    # exit 0 = answered, 1 = bus up but no answer (model hung), 2 = bus down (not a model problem)
    sys.exit({"ok": 0, "hung": 1, "down": 2}[ping_status()])

if __name__ == "__main__" and sys.argv[1:2] == ["transcript-status"]:
    since = float(sys.argv[2]) if len(sys.argv) > 2 else None   # session start, epoch seconds
    print(json.dumps(transcript_status(since=since)))   # ASCII-escaped JSON: safe for any console code page
