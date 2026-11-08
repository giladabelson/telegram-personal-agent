"""Outbound-only Telegram Bot API calls. Never calls getUpdates (the Channels plugin owns polling)."""
import json
import urllib.request
import uuid
from pathlib import Path

from . import config


def _call(method, data=None, files=None, timeout=30):
    url = f"https://api.telegram.org/bot{config.tg_token()}/{method}"
    if files:
        boundary = uuid.uuid4().hex
        body = b""
        for k, v in (data or {}).items():
            body += f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
        for k, p in files.items():
            body += (f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"; filename="{Path(p).name}"\r\n'
                     f"Content-Type: application/octet-stream\r\n\r\n").encode() + Path(p).read_bytes() + b"\r\n"
        body += f"--{boundary}--\r\n".encode()
        req = urllib.request.Request(url, body, {"Content-Type": f"multipart/form-data; boundary={boundary}"})
    else:
        req = urllib.request.Request(url, json.dumps(data or {}).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)["result"]


def send(text, chat_id=None):
    return _call("sendMessage", {"chat_id": chat_id or config.chat_id(), "text": text})


def send_keyboard(text, options, chat_id=None):
    """Message with a tap-to-answer reply keyboard (one button per row, max 8). A tap arrives as an ordinary text
    message from the user through the official plugin (which checks the sender); inline buttons can't work because
    the plugin ignores every callback_query that is not its own permission prompt, and a second getUpdates
    poller would conflict with it."""
    rows = [[{"text": str(o)[:60]}] for o in options[:8]]
    return _call("sendMessage", {"chat_id": chat_id or config.chat_id(), "text": text, "reply_markup": {
        "keyboard": rows, "one_time_keyboard": True, "resize_keyboard": True}})


def send_voice(ogg_path, chat_id=None):
    """OGG/Opus via sendVoice shows as a playable voice note (the plugin's reply tool would send a document)."""
    return _call("sendVoice", {"chat_id": chat_id or config.chat_id()}, {"voice": ogg_path}, timeout=60)


def ok():
    try:
        return bool(_call("getMe", timeout=10))
    except Exception:
        return False
