"""Encrypted connector secrets shared by OAuth and token-based connectors.

The encryption key stays in the git-ignored data/google/token.key location used by the
existing Google connector so stored credentials remain readable after this refactor.
"""
import os
from pathlib import Path

from cryptography.fernet import Fernet
from sqlalchemy import text
from sqlalchemy.orm import Session

from igneous.config import settings


def _key_path() -> Path:
    directory = Path(settings.data_dir) / "google"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / "token.key"


def _fernet() -> Fernet:
    key_file = _key_path()
    if not key_file.exists():
        key_file.write_bytes(Fernet.generate_key())
        os.chmod(key_file, 0o600)
    return Fernet(key_file.read_bytes())


def _ensure_table(s: Session) -> None:
    s.execute(text("""
        CREATE TABLE IF NOT EXISTS oauth_tokens (
            provider text NOT NULL, account text NOT NULL, token_enc bytea NOT NULL,
            updated_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (provider, account))
    """))


def save(s: Session, provider: str, account: str, secret: str) -> None:
    """Encrypt and persist a secret. Callers must never log ``secret``."""
    _ensure_table(s)
    s.execute(text("""
        INSERT INTO oauth_tokens (provider, account, token_enc) VALUES (:p, :a, :t)
        ON CONFLICT (provider, account) DO UPDATE SET token_enc = EXCLUDED.token_enc, updated_at = now()
    """), {"p": provider, "a": account, "t": _fernet().encrypt(secret.encode())})
    s.commit()


def load(s: Session, provider: str, account: str | None = None) -> tuple[str, str]:
    """Return the requested (or most recently saved) decrypted secret."""
    _ensure_table(s)
    row = s.execute(text("""
        SELECT account, token_enc FROM oauth_tokens WHERE provider = :p
          AND (CAST(:a AS text) IS NULL OR account = :a) ORDER BY updated_at DESC LIMIT 1
    """), {"p": provider, "a": account}).first()
    if row is None:
        raise RuntimeError(f"no {provider} account connected")
    return row.account, _fernet().decrypt(bytes(row.token_enc)).decode()


def accounts(s: Session, provider: str) -> list[str]:
    _ensure_table(s)
    return list(s.execute(text("SELECT account FROM oauth_tokens WHERE provider = :p ORDER BY 1"), {"p": provider}).scalars())
