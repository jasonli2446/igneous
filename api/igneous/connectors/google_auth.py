"""Google OAuth for the Gmail / Drive / Calendar / Contacts connectors.

`igneous connect google` runs the installed-app loopback flow once per account and stores the refresh token
in Postgres, **encrypted at rest** (CO-5) with a key in data/google/token.key (git-ignored, mode 600). The
OAuth client JSON lives in data/google/client_secret.json, also git-ignored.

The Google Cloud project is in Testing mode (FD-1), so refresh tokens expire after 7 days. When that happens
the connectors raise ReauthRequired and you re-run `igneous connect google`.
"""
import json
import os
from pathlib import Path

from cryptography.fernet import Fernet
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from sqlalchemy import text
from sqlalchemy.orm import Session

from igneous.config import settings

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


def _fernet() -> Fernet:
    key_file = _dir() / "token.key"
    if not key_file.exists():
        key_file.write_bytes(Fernet.generate_key())
        os.chmod(key_file, 0o600)
    return Fernet(key_file.read_bytes())


def _ensure_table(s: Session) -> None:
    s.execute(text("""
        CREATE TABLE IF NOT EXISTS oauth_tokens (
            provider   text NOT NULL,
            account    text NOT NULL,
            token_enc  bytea NOT NULL,       -- Fernet-encrypted authorized-user JSON
            updated_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (provider, account))
    """))


def save(s: Session, account: str, creds: Credentials) -> None:
    _ensure_table(s)
    s.execute(text("""
        INSERT INTO oauth_tokens (provider, account, token_enc) VALUES ('google', :a, :t)
        ON CONFLICT (provider, account) DO UPDATE SET token_enc = EXCLUDED.token_enc, updated_at = now()
    """), {"a": account, "t": _fernet().encrypt(creds.to_json().encode())})
    s.commit()


def save_secret(s: Session, provider: str, account: str, secret: str) -> None:
    """Store a non-OAuth secret (e.g. a GitHub token) encrypted the same way. Never log or return `secret`."""
    _ensure_table(s)
    s.execute(text("""
        INSERT INTO oauth_tokens (provider, account, token_enc) VALUES (:p, :a, :t)
        ON CONFLICT (provider, account) DO UPDATE SET token_enc = EXCLUDED.token_enc, updated_at = now()
    """), {"p": provider, "a": account, "t": _fernet().encrypt(secret.encode())})
    s.commit()


def accounts(s: Session, provider: str = "google") -> list[str]:
    _ensure_table(s)
    return list(s.execute(text("SELECT account FROM oauth_tokens WHERE provider = :p ORDER BY 1"),
                          {"p": provider}).scalars())


def load(s: Session, account: str | None = None) -> tuple[str, Credentials]:
    """Credentials for `account` (or the only connected account), refreshed and re-saved if needed."""
    _ensure_table(s)
    row = s.execute(text("""
        SELECT account, token_enc FROM oauth_tokens WHERE provider = 'google'
          AND (CAST(:a AS text) IS NULL OR account = :a) ORDER BY updated_at DESC LIMIT 1
    """), {"a": account}).first()
    if row is None:
        raise ReauthRequired("no Google account connected: run `igneous connect google`")
    creds = Credentials.from_authorized_user_info(json.loads(_fernet().decrypt(bytes(row.token_enc))), SCOPES)
    if not creds.valid:
        try:
            creds.refresh(Request())
        except RefreshError as e:  # Testing-mode tokens die after 7 days
            raise ReauthRequired(f"Google token for {row.account} expired or was revoked: "
                                 "run `igneous connect google` again") from e
        save(s, row.account, creds)
    return row.account, creds


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
