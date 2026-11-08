"""Deterministic Hebrew date/time parser (Asia/Jerusalem).

parse("מחר בשלוש", now) -> aware datetime, or None if no time expression found.
Relative offsets ("בעוד שעתיים") are real elapsed time (UTC arithmetic), so they
stay correct across DST changes; wall-clock phrases ("מחר בשלוש") are built in
local time and then localized.
"""
import re
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Jerusalem")

NUM = {
    "אחת": 1, "אחד": 1, "שתיים": 2, "שתים": 2, "שניים": 2, "שני": 2, "שלוש": 3, "שלושה": 3,
    "ארבע": 4, "ארבעה": 4, "חמש": 5, "חמישה": 5, "שש": 6, "שישה": 6, "שבע": 7, "שבעה": 7,
    "שמונה": 8, "תשע": 9, "תשעה": 9, "עשר": 10, "עשרה": 10,
    "אחת עשרה": 11, "אחד עשר": 11, "שתים עשרה": 12, "שתיים עשרה": 12, "שנים עשר": 12,
    "רבע": 15, "חצי": 30, "עשרים": 20, "שלושים": 30, "ארבעים": 40, "חמישים": 50,
}
# longest first so "אחת עשרה" wins over "אחת"
NUM_RE = "|".join(sorted(map(re.escape, NUM), key=len, reverse=True))
DAYS = {"ראשון": 6, "שני": 0, "שלישי": 1, "רביעי": 2, "חמישי": 3, "שישי": 4, "שבת": 5}
PARTS = {  # part of day -> (default hour, is_pm)
    "בבוקר": (9, False), "בצהריים": (12, True), "בצהרים": (12, True), "אחה\"צ": (16, True),
    "אחר הצהריים": (16, True), "אחרי הצהריים": (16, True), "בערב": (19, True), "בלילה": (21, True),
}
UNITS = {"דקה": "m", "דקות": "m", "שעה": "h", "שעות": "h", "יום": "d", "ימים": "d", "שבוע": "w", "שבועות": "w"}
DUAL = {"שעתיים": (2, "h"), "יומיים": (2, "d"), "שבועיים": (2, "w"), "חצי שעה": (30, "m"), "רבע שעה": (15, "m")}


def _num(s):
    s = s.strip()
    return int(s) if s.isdigit() else NUM.get(s)


def _clean(text):
    t = text.replace("־", " ").replace("-", " ").replace("״", '"').replace("׳", "'")
    t = re.sub(r"\bאחה\"?צ\b", 'אחה"צ', t)
    return re.sub(r"\s+", " ", t).strip()


def _relative(t, now):
    for word, (n, u) in DUAL.items():
        if re.search(rf"(בעוד|עוד) {word}", t):
            return _add(now, n, u)
    m = re.search(rf"(?:בעוד|עוד) (\d+|{NUM_RE}) ?({'|'.join(UNITS)})", t)
    if m and _num(m.group(1)):
        return _add(now, _num(m.group(1)), UNITS[m.group(2)])
    m = re.search(rf"(?:בעוד|עוד) ({'|'.join(UNITS)})\b", t)  # "בעוד שעה"
    if m:
        return _add(now, 1, UNITS[m.group(1)])
    return None


def _add(now, n, unit):
    if unit in "mh":
        return (now.astimezone(ZoneInfo("UTC")) + _delta(n, unit)).astimezone(TZ)
    return now + _delta(n, unit)


def _delta(n, unit):
    # minutes/hours are elapsed time; days/weeks keep the wall-clock time
    return {"m": timedelta(minutes=n), "h": timedelta(hours=n), "d": timedelta(days=n), "w": timedelta(weeks=n)}[unit]


def _date(t, today):
    if "מחרתיים" in t:
        return today + timedelta(days=2)
    if "מחר" in t:
        return today + timedelta(days=1)
    if "היום" in t or "הערב" in t or "הלילה" in t:
        return today
    m = re.search(r"(?<!\d)(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?(?!\d)", t)
    if m:
        d, mo = int(m.group(1)), int(m.group(2))
        y = int(m.group(3)) if m.group(3) else today.year
        y += 2000 if y < 100 else 0
        try:
            cand = today.replace(year=y, month=mo, day=d)
        except ValueError:
            return None
        if not m.group(3) and cand < today:
            cand = cand.replace(year=y + 1)
        return cand
    m = re.search(r"(?:ביום|יום|ב)\s?(ראשון|שני|שלישי|רביעי|חמישי|שישי|שבת)(?: (?:הבא|הקרוב))?", t)
    if m:
        ahead = (DAYS[m.group(1)] - today.weekday()) % 7
        if ahead == 0:  # "ביום ראשון" said on Sunday = next week's
            ahead = 7
        if "שבוע הבא" in t and ahead < 7 and ahead <= (6 - today.weekday()) % 7:
            ahead += 7  # "ראשון בשבוע הבא" → week after
        return today + timedelta(days=ahead)
    if "שבוע הבא" in t:
        return today + timedelta(days=7)
    return None


def _clock(t):
    """Return (hour, minute, explicit_part) or None."""
    part = next((p for p in PARTS if p in t), None)
    m = re.search(r"(?<!\d)(\d{1,2}):(\d{2})(?!\d)", t)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
    else:
        m = re.search(rf"(?:^|\s)(?:ב|ב )?(\d{{1,2}}|{NUM_RE})(?: (?:ו)?(רבע|חצי|\d+ דקות|{NUM_RE} דקות))?(?=\s|$)", t)
        m2 = re.search(rf"רבע ל ?(\d{{1,2}}|{NUM_RE})", t)
        if m2 and _num(m2.group(1)):
            h, mi = (_num(m2.group(1)) - 1) % 24, 45
        elif m and _num(m.group(1)) is not None and _num(m.group(1)) <= 24 and _hour_context(t, m):
            h = _num(m.group(1))
            extra = (m.group(2) or "").replace(" דקות", "").strip()
            mi = _num(extra) if extra else 0
            mi = mi or 0
        elif "בחצות" in t:
            return 0, 0, "בחצות"
        elif part:
            return PARTS[part][0], 0, part
        else:
            return None
    if part and PARTS[part][1] and h < 12 and not (part == "בלילה" and h <= 4):
        h += 12
    elif not part and 1 <= h <= 6 and not m.group(0).count(":"):
        h += 12  # "בשלוש" without qualifier means 15:00
    return h % 24, mi, part


def _hour_context(t, m):
    """A bare number is a clock time only if introduced by ב / 'בשעה' or followed by a part of day."""
    before = t[: m.start(1)]
    return before.endswith("ב") or before.endswith("ב ") or before.endswith("בשעה ") or any(p in t for p in PARTS)


def parse(text, now=None):
    now = (now or datetime.now(TZ)).astimezone(TZ)
    t = _clean(text)
    rel = _relative(t, now)
    if rel:
        return rel.astimezone(TZ)
    today = now.date()
    day = _date(t, today)
    clk = _clock(t)
    if day is None and clk is None:
        return None
    if clk is None:
        clk = (9, 0, None)  # date without time → 09:00
    h, mi, _ = clk
    dt = datetime.combine(day or today, time(h, mi), tzinfo=TZ)
    # normalise nonexistent local times (spring-forward gap) through UTC
    dt = dt.astimezone(ZoneInfo("UTC")).astimezone(TZ)
    if day is None and dt <= now:
        dt = datetime.combine(today + timedelta(days=1), time(h, mi), tzinfo=TZ)
    return dt


HEB_DAYS = ["שני", "שלישי", "רביעי", "חמישי", "שישי", "שבת", "ראשון"]


def say(dt, now=None):
    """Hebrew restatement: 'מחר, יום שלישי 13.10 בשעה 15:00'."""
    now = (now or datetime.now(TZ)).astimezone(TZ)
    dt = dt.astimezone(TZ)
    diff = (dt.date() - now.date()).days
    rel = {0: "היום, ", 1: "מחר, ", 2: "מחרתיים, "}.get(diff, "")
    return f"{rel}יום {HEB_DAYS[dt.weekday()]} {dt.day}.{dt.month} בשעה {dt:%H:%M}"
