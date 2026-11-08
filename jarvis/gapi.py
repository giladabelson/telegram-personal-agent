"""Google API clients for jarvisd, reusing the token saved by google_login (refreshed and re-saved automatically)."""
from auth.credential_store import LocalDirectoryCredentialStore
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

from . import config

_store = LocalDirectoryCredentialStore(str(config.HOME / "google"))


class GoogleAuthError(RuntimeError):
    """Token missing/expired/revoked: The user must re-run google_login."""


def creds():
    c = _store.get_credential(config.user_email())
    if c is None:
        raise GoogleAuthError("no Google token")
    if not c.valid:
        try:
            c.refresh(Request())
        except RefreshError as e:  # network errors (TransportError) are not a Google logout: let them retry
            raise GoogleAuthError(str(e)) from e
        _store.store_credential(config.user_email(), c)
    return c


def draft_snapshot(draft_id):
    """Everything that would actually go out: all recipients, subject, plain body, and a hash of the raw message.
    Used to check that what the user approved is exactly what gets sent (no hidden cc/bcc, no swapped body)."""
    import base64
    import hashlib
    from email import message_from_bytes, policy
    raw = base64.urlsafe_b64decode(service("gmail", "v1").users().drafts().get(
        userId="me", id=draft_id, format="raw").execute()["message"]["raw"])
    m = message_from_bytes(raw, policy=policy.default)
    part = m.get_body(preferencelist=("plain", "html"))
    return {"to": str(m.get("To", "")), "cc": str(m.get("Cc", "")), "bcc": str(m.get("Bcc", "")),
            "subject": str(m.get("Subject", "")), "body": part.get_content() if part else "",
            "attachments": sum(1 for _ in m.iter_attachments()), "sha": hashlib.sha256(raw).hexdigest()}


def service(name, version):
    return build(name, version, credentials=creds(), cache_discovery=False)
