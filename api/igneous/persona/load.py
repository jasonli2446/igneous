"""Putting the persona in a database: its own database, and its exports read by the connectors the Sources page and
the CLI use for each format (the "persona" entry in sources.SOURCES builds them; `igneous sync persona` runs them).

The persona never shares a database with other records: `create_database` makes only databases named igneous_persona
(or igneous_persona_<something>), and the persona's sync refuses any other database that already holds records.
"""
import json
from collections.abc import Callable
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from igneous.config import settings

DATABASE = "igneous_persona"
# Where each export's records are stored, as (connector, sync_state account) pairs: what its sync wrote last.
ACCOUNTS = (("email", "mail"), ("fs", "documents"), ("takeout", "takeout"), ("linkedin", "linkedin"),
            ("ai_chats", "chatgpt"), ("vcard", "contacts"))


class DatabaseExists(RuntimeError):
    pass


def root() -> Path:
    """Where `make persona` writes the persona: data/persona."""
    return Path(settings.data_dir) / "persona"


def manifest(folder: Path) -> dict:
    from igneous.persona import MANIFEST

    return json.loads((folder / MANIFEST).read_text())


def connectors(s: Session, folder: Path) -> list[tuple[str, Callable]]:
    """The persona's exports as (name, connector factory), each read the way the Sources page reads its format: the
    maildir as `igneous sync email --path --mailbox` does, the documents folder as `igneous sync fs --path` does (both
    built by their entries in sources.SOURCES), and the other exports by the connectors their imports run. A persona
    generated before it had a contacts export is read without one."""
    from igneous import sources
    from igneous.connectors.ai_exports import ExportConnector, preflight
    from igneous.connectors.linkedin import LinkedInConnector
    from igneous.connectors.takeout import TakeoutConnector, synced_events
    from igneous.connectors.vcard import VCardConnector

    ex = manifest(folder)["exports"]
    ((_, mail),) = sources.BY_ID["email"]["build"](
        s, None, sources.SyncOptions(path=folder / ex["maildir"], mailboxes=(ex["mailbox"],)))
    ((_, documents),) = sources.BY_ID["fs"]["build"](s, None, sources.SyncOptions(path=folder / ex["documents"]))
    chatgpt = folder / ex["chatgpt"]
    out = [
        ("mail", mail),
        ("documents", documents),
        ("takeout", lambda: TakeoutConnector([folder / ex["takeout"]], synced=synced_events(s))),
        ("linkedin", lambda: LinkedInConnector([folder / ex["linkedin"]])),
        ("chatgpt", lambda: ExportConnector(chatgpt, preflight(chatgpt))),
    ]
    if ex.get("contacts"):
        out.append(("contacts", lambda: VCardConnector([folder / ex["contacts"]])))
    return out


def database_name(s: Session) -> str:
    return s.get_bind().engine.url.database or ""


def mixing(s: Session) -> str | None:
    """Why the persona can't go into this session's database, or None: it goes into a persona database, or into one
    that holds no records yet."""
    name = database_name(s)
    if name.startswith(DATABASE):
        return None
    held = s.execute(text("SELECT count(*) FROM source_items")).scalar()
    if not held:
        return None
    return (f"The persona goes into a database of its own, never beside other records: {name} already holds "
            f"{held:,}. Run make persona, which creates {DATABASE}, and point IGNEOUS_DATABASE_URL at it.")


def present(s: Session) -> bool:
    """Whether this database holds the persona (its mailbox, which no other archive has)."""
    from igneous.persona import formats

    return bool(s.execute(text("SELECT EXISTS (SELECT 1 FROM source_items WHERE source = 'email' "
                               "AND external_id LIKE :p)"), {"p": f"{formats.MAILBOX}/%"}).scalar())


def create_database(clean: bool = False) -> str:
    """Create the database IGNEOUS_DATABASE_URL names, on its server, and migrate it to the code's head. Only a
    persona database (igneous_persona...): it fails when one exists, unless `clean` drops it first."""
    from alembic import command
    from alembic.config import Config

    from igneous.db import API_DIR

    url = make_url(settings.database_url)
    name = url.database or ""
    if not name.startswith(DATABASE):
        raise ValueError(f"IGNEOUS_DATABASE_URL names {name or 'no database'}; the persona's database must be named "
                         f"{DATABASE} (or {DATABASE}_<something>). make persona sets it.")
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as c:
            exists = c.execute(text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": name}).scalar()
            if exists and not clean:
                raise DatabaseExists(f"database {name} already exists. Use it as it is, or replace it: make persona "
                                     "CLEAN=1 (igneous persona database --clean) drops it first.")
            if exists:
                c.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
            c.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        admin.dispose()
    config = Config(str(API_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(API_DIR / "migrations"))
    command.upgrade(config, "head")
    return name
