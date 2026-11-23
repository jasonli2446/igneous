"""Credentials, encrypted at rest with a Fernet key in data/google/token.key (git-ignored, mode 600, created on first
use): tokens saved under one key can't be read with another, so the data folder must stay the same (IGNEOUS_DATA_DIR).

- Connector tokens (Google OAuth, GitHub) belong to the database whose records they sync: the `oauth_tokens` table
  (`save`, `load`, `accounts`, `delete`).
- The hosted model's key belongs to this computer, whatever database a process opens: `data/secrets/<name>`
  (`save_local`, `load_local`, `delete_local`).
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


def save(s: Session, provider: str, account: str, secret: str) -> None:
    """Encrypt and persist a secret. Callers must never log ``secret``."""
    s.execute(text("""
        INSERT INTO oauth_tokens (provider, account, token_enc) VALUES (:p, :a, :t)
        ON CONFLICT (provider, account) DO UPDATE SET token_enc = EXCLUDED.token_enc, updated_at = now()
    """), {"p": provider, "a": account, "t": _fernet().encrypt(secret.encode())})
    s.commit()


def load(s: Session, provider: str, account: str | None = None) -> tuple[str, str]:
    """Return the requested (or most recently saved) decrypted secret."""
    row = s.execute(text("""
        SELECT account, token_enc FROM oauth_tokens WHERE provider = :p
          AND (CAST(:a AS text) IS NULL OR account = :a) ORDER BY updated_at DESC LIMIT 1
    """), {"p": provider, "a": account}).first()
    if row is None:
        raise RuntimeError(f"no {provider} account connected")
    return row.account, _fernet().decrypt(bytes(row.token_enc)).decode()


def accounts(s: Session, provider: str) -> list[str]:
    return list(s.execute(text("SELECT account FROM oauth_tokens WHERE provider = :p ORDER BY 1"), {"p": provider}).scalars())


def delete(s: Session, provider: str, account: str) -> bool:
    """Forget an account's secret. Its synced items and cursor stay: connecting it again resumes where it stopped."""
    gone = s.execute(text("DELETE FROM oauth_tokens WHERE provider = :p AND account = :a"),
                     {"p": provider, "a": account}).rowcount
    s.commit()
    return bool(gone)


def _local(name: str) -> Path:
    return Path(settings.data_dir) / "secrets" / name


def save_local(name: str, secret: str) -> None:
    """Encrypt and store a secret of this computer's (the hosted model's key). Callers must never log ``secret``."""
    path = _local(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_fernet().encrypt(secret.strip().encode()))
    os.chmod(path, 0o600)


def load_local(name: str) -> str | None:
    """The decrypted secret saved under `name`, or None when there is none."""
    path = _local(name)
    return _fernet().decrypt(path.read_bytes()).decode() if path.exists() else None


def delete_local(name: str) -> bool:
    """Forget a secret of this computer's; whether there was one."""
    path = _local(name)
    if not path.exists():
        return False
    path.unlink()
    return True
