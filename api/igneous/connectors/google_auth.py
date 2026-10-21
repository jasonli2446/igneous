"""Google OAuth for the Gmail / Drive / Calendar / Contacts connectors.

`igneous connect google` runs the installed-app loopback flow once per account and stores the refresh token
in Postgres, **encrypted at rest** (CO-5) with a key in data/google/token.key (git-ignored, mode 600). The
OAuth client JSON lives in data/google/client_secret.json, also git-ignored.

The Google Cloud project is in Testing mode (FD-1), so refresh tokens expire after 7 days. When that happens
the connectors raise ReauthRequired and you re-run `igneous connect google`.
"""
import json
from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from sqlalchemy.orm import Session

from igneous.config import settings
from igneous.connectors import secrets

SCOPES = [  # read-only everywhere (CO-2)
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/contacts.readonly",
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
]


class ReauthRequired(RuntimeError):
    pass


def _dir() -> Path:
    d = Path(settings.data_dir) / "google"
    d.mkdir(parents=True, exist_ok=True)
    return d


def client_secret_path() -> Path:
    p = _dir() / "client_secret.json"
    if not p.exists():
        raise FileNotFoundError(f"put the OAuth desktop client JSON at {p} (see README: Google)")
    return p


def save(s: Session, account: str, creds: Credentials) -> None:
    secrets.save(s, "google", account, creds.to_json())


def accounts(s: Session, provider: str = "google") -> list[str]:
    return secrets.accounts(s, provider)


def load(s: Session, account: str | None = None) -> tuple[str, Credentials]:
    """Credentials for `account` (or the only connected account), refreshed and re-saved if needed."""
    try:
        saved_account, saved = secrets.load(s, "google", account)
    except RuntimeError as e:
        raise ReauthRequired("no Google account connected: run `igneous connect google`") from e
    creds = Credentials.from_authorized_user_info(json.loads(saved), SCOPES)
    if not creds.valid:
        try:
            creds.refresh(Request())
        except RefreshError as e:  # Testing-mode tokens die after 7 days
            raise ReauthRequired(f"Google token for {saved_account} expired or was revoked: "
                                 "run `igneous connect google` again") from e
        save(s, saved_account, creds)
    return saved_account, creds


def connect(s: Session, open_browser: bool = True) -> str:
    """Interactive: shows the consent screen (opens a browser, or prints the URL), stores the token, returns
    the account email."""
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    flow = InstalledAppFlow.from_client_secrets_file(str(client_secret_path()), SCOPES)
    creds = flow.run_local_server(port=0, open_browser=open_browser, prompt="consent select_account", access_type="offline",
                                  authorization_prompt_message="Sign in to Google here:\n{url}\n")
    email = build("oauth2", "v2", credentials=creds, cache_discovery=False).userinfo().get().execute()["email"]
    save(s, email, creds)
    return email
