"""Paths and settings. Secrets and runtime state live outside the project folder (R12)."""
import os
import secrets
from pathlib import Path

HOME = Path(os.environ.get("JARVIS_HOME", Path.home() / ".jarvis"))
CLAUDE_DIR = HOME / "claude"                      # CLAUDE_CONFIG_DIR of the Jarvis session
TG_DIR = CLAUDE_DIR / "channels" / "telegram"
DATA = HOME / "data"
LOGS = HOME / "logs"
PROJECT = Path(__file__).resolve().parent.parent
for d in (DATA, LOGS):
    d.mkdir(parents=True, exist_ok=True)

# Required, no defaults: the Telegram chat of the one user, and that user's Google account.
USER_CHAT_ID = os.environ.get("JARVIS_CHAT_ID", "")
USER_EMAIL = os.environ.get("JARVIS_USER_EMAIL", "")
# Google Tasks list id (default list) and the Gmail labels the morning brief summarises (comma separated).
TASKLIST_ID = os.environ.get("JARVIS_TASKLIST_ID", "@default")
MAIL_LABELS = [l.strip() for l in os.environ.get("JARVIS_MAIL_LABELS", "").split(",") if l.strip()]
# Gmail query for "important new mail" that may deserve a model turn (e.g. university or work senders).
WATCH_QUERY = os.environ.get("JARVIS_WATCH_QUERY", "label:important newer_than:1d")
USER_NAME = os.environ.get("JARVIS_USER_NAME", "")
WHISPER_MODEL = os.environ.get("JARVIS_WHISPER", "ivrit-ai/whisper-large-v3-turbo-ct2")
HOST, PORT = "127.0.0.1", int(os.environ.get("JARVIS_PORT", "8765"))

# A short natural sentence. A long comma-separated word list makes the ivrit-ai model return empty text (tested).
INITIAL_PROMPT = os.environ.get("JARVIS_WHISPER_PROMPT", "המשתמש מדבר עם העוזר האישי על משימות ותזכורות.")


def require(name, value):
    """Fail loudly instead of silently messaging or acting as a default account."""
    if not value:
        raise RuntimeError(f"{name} is not set (see .env.example)")
    return value


def chat_id():
    return require("JARVIS_CHAT_ID", USER_CHAT_ID)


def user_email():
    return require("JARVIS_USER_EMAIL", USER_EMAIL)


def tg_token():
    for line in (TG_DIR / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("TELEGRAM_BOT_TOKEN="):
            return line.split("=", 1)[1].strip()
    raise RuntimeError("no TELEGRAM_BOT_TOKEN")


def api_key():
    """Shared secret so only Jarvis's own MCP server can call the local daemon."""
    f = HOME / "daemon.key"
    if not f.exists():
        f.write_text(secrets.token_urlsafe(32), encoding="utf-8")
    return f.read_text(encoding="utf-8").strip()
