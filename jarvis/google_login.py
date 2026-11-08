"""One-time Google consent. Saves the token where workspace-mcp (and jarvisd) read it: ~/.jarvis/google/.
Re-run this if Google ever says the token expired or was revoked."""
from google_auth_oauthlib.flow import InstalledAppFlow

from auth.credential_store import LocalDirectoryCredentialStore  # from workspace-mcp

from . import config

SCOPES = [
    "openid", "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/gmail.modify", "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.labels", "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.send",  # only jarvisd uses send, for sends the user approved
    "https://www.googleapis.com/auth/calendar", "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/tasks",
]
GOOGLE_DIR = config.HOME / "google"

if __name__ == "__main__":
    flow = InstalledAppFlow.from_client_secrets_file(str(config.HOME / "client_secret.json"), SCOPES)
    creds = flow.run_local_server(host="127.0.0.1", port=0, open_browser=True,
                                  success_message="Jarvis connected. You can close this tab.")
    LocalDirectoryCredentialStore(str(GOOGLE_DIR)).store_credential(config.user_email(), creds)
    from datetime import datetime, timezone
    (GOOGLE_DIR / "login_date").write_text(datetime.now(timezone.utc).isoformat())
    print("saved", sorted(creds.scopes or []))
