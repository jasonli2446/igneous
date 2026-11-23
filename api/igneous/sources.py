"""The Sources registry: every connector and export format in plain language, and how to build each connector.

The Sources page (web/src/app/settings) shows `describe()`. `igneous sync`, POST /sources/{id}/sync and the
scheduled sync all run `sync()`, which builds a source's connectors with its entry's `build`, so the three can't
construct a source differently. Adding a connector or import format? Add an entry to SOURCES and update the page
(CLAUDE.md, "Setup lives in the UI"). `model_use()` says whether a hosted model may read each source: the rule every
model call follows (igneous/llm.py).

This module stays light (the CLI imports it to list the sources): Google, the model client and the job runner load
inside the functions that use them.
"""
import glob
import os
import sqlite3
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

from igneous import source_catalog
from igneous.config import settings
from igneous.connectors import ai_chats, apple_notes, imessage, mac_contacts, macos
from igneous.connectors.base import Connector
from igneous.parsers import MAX_DOCUMENT_BYTES

Progress = Callable[..., None]  # progress(dict, force=False), as jobs.Progress

# The Enron demo: the mailbox `make demo`, `igneous sync email` and the Sources page's Sync now read by default.
DEMO_MAILBOXES = ("allen-p",)
# How far back a source's first sync reads when it isn't told: about a quarter of Messages, Gmail and Calendar.
FIRST_SYNC_DAYS = {"imessage": 90, "gmail": 90, "gcal": 365}

FDA_STEPS = [
    "Open System Settings, then Privacy & Security, then Full Disk Access.",
    "Turn it on for the app that runs the Igneous API (usually Terminal, iTerm, or your editor).",
    "Quit and reopen that app, restart the API, then press Check again here.",
]
GOOGLE_STEPS = [
    "One-time: create a Google OAuth client of type Desktop app and save its JSON as data/google/client_secret.json "
    "(the README's Google section walks through it). This page says when it finds the file.",
    "Press Connect a Google account and pick the account in the Google window. Igneous only asks for read-only access.",
    "Come back here. The account appears below; add more accounts the same way, and disconnect one to stop syncing it "
    "(what it synced stays).",
]
MAC_ONLY = "macOS only."


@dataclass(frozen=True)
class SyncOptions:
    """What a sync can be asked for beyond its defaults. Each source accepts only the options its entry lists."""
    path: Path | None = None             # fs: one folder; imessage, notes: another database; email: another maildir
    mailboxes: tuple[str, ...] = ()      # email: these mailboxes instead of the demo one
    since_days: int | None = None        # first sync only: how far back (gmail, gcal, imessage)
    all_mail: bool | None = None         # gmail: include Promotions/Social/Forums; None keeps the account's choice


class SetupNeeded(RuntimeError):
    """A source can't sync until something is set up; the message says what."""


class SyncFailed(RuntimeError):
    """Some accounts of a source failed. `result` holds the stats of the accounts that synced."""

    def __init__(self, message: str, result: dict):
        super().__init__(message)
        self.result = result


# ---- building connectors ----
# A builder lists a source's accounts, each with a function that makes its connector when its turn comes: making one
# can fail (an expired Google token) without stopping the others.

Builder = Callable[[Session, str | None, SyncOptions], list[tuple[str, Callable[[], Connector]]]]


def _google(make: Callable[..., Connector]) -> Builder:
    """One connector per connected Google account (or the one asked for), with fresh credentials."""
    def build(s: Session, account: str | None, opts: SyncOptions) -> list[tuple[str, Callable[[], Connector]]]:
        from igneous.connectors import google_auth, secrets

        names = [account] if account else secrets.accounts(s, "google")
        if not names:
            raise SetupNeeded("No Google account connected yet: press Connect a Google account on the Sources page "
                              "or run `igneous connect google`.")

        def connector(name: str) -> Connector:
            name, creds = google_auth.load(s, name)
            return make(creds, name, opts)
        return [(name, lambda name=name: connector(name)) for name in names]
    return build


def _gmail(creds, account: str, opts: SyncOptions) -> Connector:
    from igneous.connectors.gmail import GmailConnector
    return GmailConnector(creds, account, since_days=opts.since_days or FIRST_SYNC_DAYS["gmail"],
                          all_mail=opts.all_mail)


def _gcal(creds, account: str, opts: SyncOptions) -> Connector:
    from igneous.connectors.gcal import CalendarConnector
    return CalendarConnector(creds, account, since_days=opts.since_days or FIRST_SYNC_DAYS["gcal"])


def _gcontacts(creds, account: str, opts: SyncOptions) -> Connector:
    from igneous.connectors.gcal import ContactsConnector
    return ContactsConnector(creds, account)


def _drive(creds, account: str, opts: SyncOptions) -> Connector:
    from igneous.connectors.drive import DriveConnector
    return DriveConnector(creds, account)


def _github(s: Session, account: str | None, opts: SyncOptions) -> list[tuple[str, Callable[[], Connector]]]:
    from igneous.connectors import secrets
    from igneous.connectors.github import GithubConnector

    names = [account] if account else secrets.accounts(s, "github")
    if not names:
        raise SetupNeeded("No GitHub token saved yet: paste one on the Sources page or run `igneous connect github`.")

    def connector(name: str) -> Connector:
        name, token = secrets.load(s, "github", name)
        return GithubConnector(token, name)
    return [(name, lambda name=name: connector(name)) for name in names]


def _local(make: Callable[[SyncOptions], Connector]) -> Builder:
    """A source on this computer: one connector, under the account "default"."""
    def build(s: Session, account: str | None, opts: SyncOptions) -> list[tuple[str, Callable[[], Connector]]]:
        return [("default", lambda: make(opts))]
    return build


def _fs(opts: SyncOptions) -> Connector:
    from igneous.connectors.filesystem import FilesystemConnector

    configured = fs_roots()
    roots = [opts.path] if opts.path else configured
    if not roots:
        raise SetupNeeded("No folders to read: set IGNEOUS_FILESYSTEM_ROOTS, or pass --path.")
    return FilesystemConnector(roots, anchors=configured)


def _email(opts: SyncOptions) -> Connector:
    from igneous.connectors.email_maildir import MaildirConnector
    return MaildirConnector(opts.path or Path(settings.data_dir) / "maildir", list(opts.mailboxes or DEMO_MAILBOXES))


def _persona(s: Session, account: str | None, opts: SyncOptions) -> list[tuple[str, Callable[[], Connector]]]:
    """The synthetic persona's exports, each read by its own format's connector (persona/load.py), into a database
    that holds nothing else."""
    from igneous.persona import MANIFEST, load

    folder = opts.path or load.root()
    if not (folder / MANIFEST).is_file():
        raise SetupNeeded(f"No persona at {folder}: run make persona (or igneous persona generate {folder}).")
    if problem := load.mixing(s):
        raise SetupNeeded(problem)
    return load.connectors(s, folder)


# kind: google | local | remote | import. cli: the equivalent command, shown in muted text. build: how `sync()`
# makes the source's connectors; options: the SyncOptions fields it accepts.
SOURCES: list[dict] = [
    {"id": "gmail", "kind": "google", "name": "Gmail", "group": "Connected accounts",
     "reads": "Your email: senders, recipients, subjects, bodies, and threads, from the last "
              f"{FIRST_SYNC_DAYS['gmail']} days on. Spam, drafts, Promotions, Social and Forums are skipped "
              "(igneous sync gmail --all-mail keeps Promotions, Social and Forums for that account).",
     "steps": GOOGLE_STEPS, "cli": "igneous connect google && igneous sync gmail",
     "build": _google(_gmail), "options": {"since_days", "all_mail"}},
    {"id": "gcal", "kind": "google", "name": "Google Calendar", "group": "Connected accounts",
     "reads": f"Your events from the last {FIRST_SYNC_DAYS['gcal']} days on, in every calendar on your list: titles, "
              "times, attendees, and places.",
     "steps": GOOGLE_STEPS, "cli": "igneous sync gcal", "build": _google(_gcal), "options": {"since_days"}},
    {"id": "gcontacts", "kind": "google", "name": "Google Contacts", "group": "Connected accounts",
     "reads": "Names, emails, phone numbers, and organizations on your contact cards. Helps merge duplicate people.",
     "steps": GOOGLE_STEPS, "cli": "igneous sync gcontacts", "build": _google(_gcontacts), "options": set()},
    {"id": "drive", "kind": "google", "name": "Google Drive", "group": "Connected accounts",
     "reads": "The text of your Docs, Sheets (every sheet), Slides, PDFs, and other readable files up to "
              f"{MAX_DOCUMENT_BYTES // 2**20} MB. Anything else is never downloaded.",
     "steps": GOOGLE_STEPS, "cli": "igneous sync drive", "build": _google(_drive), "options": set()},
    {"id": "github", "kind": "remote", "name": "GitHub", "group": "Connected accounts",
     "reads": "The repositories you own or collaborate on that the token can read: their README files, issues, pull "
              "requests, comments, and daily commit activity (read-only).",
     "steps": ["On github.com open Settings, Developer settings, Fine-grained tokens, and generate a token.",
               "Give it read-only access (Contents, Issues, Pull requests: Read) to the repositories you want.",
               "Paste it below. It is stored encrypted on this computer and never shown again. When it expires, "
               "paste a new one the same way: a token for the same account replaces the old one."],
     "cli": "igneous connect github && igneous sync github", "build": _github, "options": set()},
    {"id": "chatgpt", "kind": "import", "name": "ChatGPT export", "group": "Exports you upload",
     "reads": "Your ChatGPT conversation history from OpenAI's data export: your messages and ChatGPT's written "
              "replies (images, code runs and tool output are skipped).",
     "steps": ["Request the export in ChatGPT settings (Data controls, Export data).",
               "Upload the .zip you receive by email in the box below, or give its path."],
     "cli": "igneous import ai-export ~/Downloads/chatgpt-export.zip"},
    {"id": "claude_ai", "kind": "import", "name": "Claude.ai export", "group": "Exports you upload",
     "reads": "Your claude.ai conversation history from Anthropic's data export: your messages and Claude's written "
              "replies.",
     "steps": ["Request the export in claude.ai settings (Privacy, Export data).",
               "Upload the .zip you receive by email in the box below, or give its path."],
     "cli": "igneous import ai-export ~/Downloads/claude-export.zip"},
    {"id": "linkedin", "kind": "import", "name": "LinkedIn", "group": "Exports you upload",
     "reads": "Your LinkedIn profile (positions, education, projects, skills) and your connections with their company, "
              "position and connection date. Positions and education become your employers and schools; connections "
              "become named people linked to their companies. Login data is never read.",
     "steps": ["On linkedin.com click Me, then Settings & Privacy, then Data privacy, then Get a copy of your data.",
               "For the fast option choose Want something in particular? and tick Connections only (usually minutes). "
               "For positions, education, projects and skills choose Download larger data archive "
               "(it can take up to 24 hours).",
               "Click Request archive and confirm your password. LinkedIn emails you a download link.",
               "Download the .zip. In the box below upload it, or give its path or an extracted folder."],
     "cli": "igneous import linkedin ~/Downloads/Basic_LinkedInDataExport.zip"},
    {"id": "takeout", "kind": "import", "name": "Google Takeout", "group": "Exports you upload",
     "reads": "Your Google My Activity (Search, YouTube, Maps, Gemini and the rest), Chrome history and bookmarks, "
              "and Maps places and visits. Gemini chats are part of My Activity (a Gemini-only export works too).",
     "steps": ["Go to takeout.google.com and click Deselect all.",
               "Tick My Activity, Chrome, and Maps (your places). If you see Location History (Timeline), tick it too.",
               "On My Activity click Multiple formats and change Activity records from HTML to JSON. HTML is not read.",
               "Click Next step, keep one-time export and .zip, then Create export. It usually takes hours and may "
               "arrive as several zips.",
               "Download every zip. In the box below choose Scan Downloads for takeout-*.zip, give a path, or upload them."],
     "cli": "igneous import takeout ~/Downloads/takeout-*.zip"},
    {"id": "imessage", "kind": "local", "name": "Messages", "group": "On this computer",
     "reads": "iMessage and SMS conversations on this Mac, grouped into one entry per conversation per day. "
              + MAC_ONLY,
     "steps": ["Messages must be signed in on this Mac so the history is on disk.", *FDA_STEPS,
               f"Press Sync now. The first sync reads the last {FIRST_SYNC_DAYS['imessage']} days; later ones read "
               "what is new, including older messages that arrive late from your other devices."],
     "cli": "igneous sync imessage",
     "build": _local(lambda o: imessage.MessagesConnector(o.path or imessage.DEFAULT_DB,
                                                          since_days=o.since_days or FIRST_SYNC_DAYS["imessage"])),
     "options": {"path", "since_days"}},
    {"id": "maccontacts", "kind": "local", "name": "Contacts", "group": "On this computer",
     "reads": "Contacts.app cards (where iPhone contacts sync): names, phone numbers, and emails. "
              "Lets Messages show names instead of numbers. " + MAC_ONLY,
     "steps": [*FDA_STEPS, "Press Sync now. A card you delete in Contacts goes at the next sync."],
     "cli": "igneous sync maccontacts",
     "build": _local(lambda o: mac_contacts.MacContactsConnector()), "options": set()},
    {"id": "ai_chats", "kind": "local", "name": "Claude Code and Codex chats", "group": "On this computer",
     "reads": "Your prompts and the assistant's written replies from coding sessions on this computer. Tool output "
              "and file contents are skipped, and secrets are redacted.",
     "steps": ["Nothing to set up: it reads ~/.claude/projects and ~/.codex/sessions if you use those tools.",
               "Press Sync now."],
     "cli": "igneous sync ai_chats", "build": _local(lambda o: ai_chats.AIChatConnector()), "options": set()},
    {"id": "fs", "kind": "local", "name": "Local files", "group": "On this computer",
     "reads": "Name, folder and size of every file in the folders you choose, the text of documents (PDF, .docx, "
              f".xlsx, .pptx, text and code, up to {MAX_DOCUMENT_BYTES // 2**20} MB), and each git checkout. Key "
              "and .env files are never read, and credentials in text are redacted. Search indexes the text on this "
              "computer; a hosted model reads it only if you opt it in (below).",
     "steps": ["Set IGNEOUS_FILESYSTEM_ROOTS to a comma-separated list of folders (in api/.env or the API's "
               "environment), then restart the API. With no setting, existing Documents, Desktop, Downloads, and "
               "iCloud Drive folders are used.",
               "On macOS, let the app that runs the API read those folders: System Settings, Privacy & Security, "
               "Files and Folders (or Full Disk Access), then turn on its Documents, Desktop and Downloads "
               "folders. Restart that app and the API. A folder it can't read is skipped and keeps what an earlier "
               "sync stored.",
               "Press Sync now. Each sync notices added, changed and deleted files in those folders without "
               "reopening unchanged ones."],
     "cli": "igneous sync fs (or --path FOLDER for one folder, leaving the others as they are)",
     "build": _local(_fs), "options": {"path"}},
    {"id": "notes", "kind": "local", "name": "Apple Notes", "group": "On this computer",
     "reads": "Unlocked Notes.app titles and bodies from the local database, with credentials redacted. Search "
              "indexes them on this computer; a hosted model reads them only if you opt them in (below). " + MAC_ONLY,
     "steps": [*FDA_STEPS, "Press Sync now. Locked notes are skipped."], "cli": "igneous sync notes",
     "build": _local(lambda o: apple_notes.AppleNotesConnector(o.path or apple_notes.DEFAULT_DB)),
     "options": {"path"}},
    {"id": "email", "kind": "local", "name": "Enron demo mailbox", "group": "Demo",
     "reads": f"The public Enron email corpus: the {', '.join(DEMO_MAILBOXES)} mailbox (about 3,000 emails). For "
              "demos and evaluation only; never mix it with your own data.",
     "steps": ["Run make enron once to download the mailboxes into data/maildir.",
               f"Press Sync now to read {', '.join(DEMO_MAILBOXES)}. Other mailboxes are read from the command line "
               "(igneous sync email --mailbox lay-k)."],
     "cli": "igneous sync email", "build": _local(_email), "options": {"path", "mailboxes"}},
    {"id": "persona", "kind": "local", "name": "Synthetic persona", "group": "Demo",
     "reads": "Two invented years of Mira Castellanos, a computer science student at the fictional Lakeshore "
              "University: her mailbox, Google Takeout (chat, tasks, Meet, Maps, My Activity, Chrome), LinkedIn and "
              "ChatGPT exports, and a documents folder with her resumes, notes and a git checkout. Generated on this "
              "computer from a seed, so every source and the trips, classes, job and project they make can be shown "
              "without personal data. For demos and evaluation, in a database of its own.",
     "steps": ["From the repository root run make persona. It writes the exports to data/persona, creates the database "
               "igneous_persona (make persona CLEAN=1 replaces it), reads each export through its own source on "
               "this page, and builds the graph.",
               "Point the API at it: set IGNEOUS_DATABASE_URL=postgresql+psycopg://igneous:igneous@localhost:5432/"
               "igneous_persona in api/.env (or the API's environment) and restart the API.",
               "Sync now reads data/persona into this database again. It refuses a database that holds anything "
               "else, so the persona never mixes with your data or the Enron demo."],
     "cli": "make persona (igneous persona generate ../data/persona, then igneous sync persona)",
     "build": _persona, "options": {"path"}},
]
# The persona's records are stored under the sources its exports come through.
PERSONA_STORED = ("email", "fs", *source_catalog.of_connector("takeout"), "linkedin", "ai_chats")
BY_ID = {s["id"]: s for s in SOURCES}
SYNCABLE = tuple(s["id"] for s in SOURCES if "build" in s)  # in page order
IMPORT_TOOL = {"chatgpt", "gemini", "claude_ai"}  # raw_meta.tool values stored under source 'ai_chats'
MODEL_ID = {"chatgpt": "ai_chats", "claude_ai": "ai_chats"}  # the IGNEOUS_LLM_SOURCES id that covers an export
_OPTION_FLAG = {"path": "--path", "mailboxes": "--mailbox", "since_days": "--since-days", "all_mail": "--all-mail"}


def model_use(sid: str, allowed: frozenset[str]) -> dict:
    """Whether a hosted model may read this source (yes | partly | no), and how to change that."""
    if sid == "persona":
        n = sum(v in allowed for v in PERSONA_STORED)
        state = "yes" if n == len(PERSONA_STORED) else "partly" if n else "no"
        return {"id": "email,fs,takeout,linkedin,ai_chats", "allowed": state, "note": (
            "Its records are stored under the sources it came through. A hosted model reads the ones IGNEOUS_LLM_SOURCES "
            "lists: email,fs,takeout,linkedin,ai_chats for all of it, with IGNEOUS_LOCAL_SOURCES_MODEL_ENABLED=true "
            "for its documents." + (" All of it is opted in." if state == "yes" else ""))}
    mid = MODEL_ID.get(sid, sid)
    stored = source_catalog.of_connector(mid) or (mid,)
    n = sum(v in allowed for v in stored)
    state = "yes" if n == len(stored) else "partly" if n else "no"
    if state == "yes":
        note = f"Opted in: a hosted model may read it (IGNEOUS_LLM_SOURCES lists {mid})."
    elif state == "partly":
        note = f"Partly opted in: IGNEOUS_LLM_SOURCES lists some of it. Add {mid} to opt in all of it."
    elif sid in ("gcontacts", "maccontacts"):
        note = ("Cards are never sent to a model. Names from them label people in the records of opted-in "
                f"sources. To opt the cards in, add {mid} to IGNEOUS_LLM_SOURCES.")
    else:
        note = (f"Never sent to a hosted model. To allow it, add {mid} to IGNEOUS_LLM_SOURCES"
                + (" and set IGNEOUS_LOCAL_SOURCES_MODEL_ENABLED=true" if mid in source_catalog.LOCAL_TEXT else "")
                + (" (that covers all AI chat history)" if mid != sid else "") + ", then restart the API.")
    if sid == "takeout":  # stored with the other AI chats, not under a takeout_* source
        note += " Its Gemini chats count as AI chats (ai_chats)."
    return {"id": mid, "allowed": state, "note": note}


# ---- requirement checks: cheap, read-only, no network ----

def _sqlite_readable(path: str) -> str | None:
    """None if readable, else a plain-language reason (the Full Disk Access case is the usual one)."""
    try:
        with macos.read_only(path):
            pass
    except PermissionError:
        return "macOS is blocking access. Grant Full Disk Access (steps above), restart the API, and check again."
    except (FileNotFoundError, sqlite3.DatabaseError) as e:
        return str(e)
    return None


def check_requirements(source_id: str) -> tuple[bool, str | None]:
    """(ok, message-when-not-ok). Never touches the network."""
    kind = BY_ID[source_id]["kind"]
    if kind == "google":
        if not (Path(settings.data_dir) / "google" / "client_secret.json").exists():
            return False, "No Google OAuth client found. Save its JSON as data/google/client_secret.json (step 1)."
    elif source_id in ("imessage", "maccontacts", "notes") and sys.platform != "darwin":
        return False, f"{BY_ID[source_id]['name']} is read on macOS only."
    elif source_id == "imessage":
        problem = _sqlite_readable(str(imessage.DEFAULT_DB))
        return (problem is None), problem
    elif source_id == "maccontacts":
        paths = glob.glob(mac_contacts.SOURCES)
        if not paths:
            return False, "No Contacts database found on this Mac."
        problems = [p for p in map(_sqlite_readable, paths) if p]
        return (not problems), (problems[0] if problems else None)
    elif source_id == "notes":
        problem = _sqlite_readable(str(apple_notes.DEFAULT_DB))
        return (problem is None), problem
    elif source_id == "ai_chats":
        if not (os.path.isdir(ai_chats.CLAUDE_ROOT) or os.path.isdir(ai_chats.CODEX_ROOT)):
            return False, "No Claude Code or Codex sessions found on this computer."
    elif source_id == "fs":
        roots = fs_roots()
        if not roots:
            return False, "No folders configured. Set IGNEOUS_FILESYSTEM_ROOTS (step 1) and restart the API."
        if missing := [r for r in roots if not r.is_dir()]:
            return False, f"Folder not found: {missing[0]}"
    elif source_id == "email":
        if not (Path(settings.data_dir) / "maildir").is_dir():
            return False, "No mailboxes at data/maildir. Run make enron first."
    elif source_id == "persona":
        from igneous.persona import MANIFEST, load

        if not (load.root() / MANIFEST).is_file():
            return False, "No persona at data/persona yet. Run make persona from the repository root."
    return True, None


def fs_roots() -> list[Path]:
    """The Local files folders: IGNEOUS_FILESYSTEM_ROOTS, or else the Documents, Desktop, Downloads and iCloud
    Drive folders that exist."""
    configured = [Path(r.strip()).expanduser() for r in settings.filesystem_roots.split(",") if r.strip()]
    if configured:
        return configured
    defaults = [Path.home() / name for name in ("Documents", "Desktop", "Downloads")]
    defaults.append(Path.home() / "Library/Mobile Documents/com~apple~CloudDocs")
    return [path for path in defaults if path.is_dir()]


# ---- live description ----

def describe(s: Session) -> list[dict]:
    from igneous import jobs, llm
    from igneous.connectors import secrets
    from igneous.persona import formats as persona_formats
    from igneous.persona import load as persona

    counts = dict(s.execute(text("SELECT source, count(*) FROM source_items GROUP BY 1")).all())
    # The persona's mailbox is read by the same connector as the Enron demo's: it counts as the persona's, not Enron's.
    persona_mail = s.execute(text("SELECT count(*) FROM source_items WHERE source = 'email' AND external_id LIKE :p"),
                             {"p": f"{persona_formats.MAILBOX}/%"}).scalar()
    export_counts = dict(s.execute(text("""
        SELECT raw_meta->>'tool', count(*) FROM source_items WHERE source = 'ai_chats' GROUP BY 1""")).all())
    latest_item = dict(s.execute(text("SELECT source, max(updated_at) FROM source_items GROUP BY 1")).all())
    latest_export = dict(s.execute(text("""
        SELECT raw_meta->>'tool', max(updated_at) FROM source_items WHERE source = 'ai_chats' GROUP BY 1""")).all())
    states: dict[str, list[tuple[str, object]]] = {}
    for connector, account, updated in s.execute(text("SELECT connector, account, updated_at FROM sync_state")):
        states.setdefault(connector, []).append((account, updated))
    latest = jobs.latest_by_source(s)
    google_accounts = secrets.accounts(s, "google")
    github_accounts = secrets.accounts(s, "github")
    ai_local = sum(n for tool, n in export_counts.items() if tool not in IMPORT_TOOL)
    allowed = llm.allowed_sources()

    out = []
    for src in SOURCES:
        sid, kind = src["id"], src["kind"]
        row = {k: src[k] for k in ("id", "kind", "name", "group", "reads", "steps")}
        row["cli"] = src.get("cli")
        if sid == "takeout":
            count = sum(n for src_, n in counts.items() if src_.startswith("takeout_")) + export_counts.get("gemini", 0)
        elif sid == "linkedin":
            count = counts.get("linkedin", 0)
        elif kind == "import":
            count = export_counts.get(sid, 0)
        elif sid == "ai_chats":
            count = ai_local
        elif sid == "persona":  # a persona database holds nothing else (persona/load.py)
            count = sum(counts.values()) if persona_mail else 0
        elif sid == "email":
            count = counts.get(sid, 0) - persona_mail
        else:
            count = counts.get(sid, 0)
        last = max((u for _, u in states.get(sid, [])), default=None)
        if sid == "persona":
            last = max((u for connector, label in persona.ACCOUNTS for a, u in states.get(connector, []) if a == label),
                       default=None)
        elif sid == "email":
            last = max((u for a, u in states.get(sid, []) if a != dict(persona.ACCOUNTS)["email"]), default=None)
        if kind == "import":  # imports have no sync checkpoint: last import = newest item they wrote
            stamps = ([t for k, t in latest_item.items() if k.startswith("takeout_")] + [latest_export.get("gemini")]
                      if sid == "takeout" else [latest_item.get(sid) if sid == "linkedin" else latest_export.get(sid)])
            last = max((t for t in stamps if t), default=None)
        ok, message = check_requirements(sid) if kind in ("google", "local") else (True, None)
        accounts = []
        if kind == "google":
            by_account = dict(states.get(sid, []))
            accounts = [{"account": a, "last_sync": by_account.get(a)} for a in google_accounts]
        elif sid == "github":
            by_account = dict(states.get(sid, []))
            accounts = [{"account": a, "last_sync": by_account.get(a)} for a in github_accounts]
        job = latest.get(sid)
        status = "ready"
        if sid == "github" and not github_accounts:
            status, message = "not_set_up", "Save a read-only GitHub token to connect."
        elif kind == "import":
            status = "ready" if count else "not_set_up"
        elif kind == "google" and not google_accounts:
            status, message = "not_set_up", message or "No Google account connected yet."
        elif not ok:
            status = "needs_attention" if (count or last) else "not_set_up"
        elif not (count or last or (job and job["status"] == "running")) and kind == "local":
            status, message = "not_set_up", None
        if job and job["status"] == "running":
            status, message = "syncing", None
        elif job and job["status"] == "failed" and kind in ("google", "local", "remote") and (kind != "google" or google_accounts):
            status, message = "needs_attention", job["error"]
        row.update(status=status, message=message, items=count, last_sync=last, accounts=accounts,
                   requirements_ok=ok, can_sync=sid in SYNCABLE and ok and (kind != "google" or bool(google_accounts))
                   and (sid != "github" or bool(github_accounts)),
                   job=job_view(job) if job else None, model=model_use(sid, allowed))
        out.append(row)
    return out


def job_view(j: dict) -> dict:
    return {k: j[k] for k in ("id", "kind", "source", "account", "status", "started_at", "finished_at",
                              "progress", "error", "result")}


# ---- syncing ----

def check_options(source_id: str, opts: SyncOptions, account: str | None = None) -> None:
    """ValueError naming the option a source doesn't take."""
    entry = BY_ID.get(source_id)
    if entry is None or "build" not in entry:
        raise ValueError(f"{source_id!r} is not a source you can sync; choose one of {', '.join(SYNCABLE)}.")
    for name, flag in _OPTION_FLAG.items():
        if getattr(opts, name) not in (None, ()) and name not in entry["options"]:
            raise ValueError(f"{flag} doesn't apply to {source_id}.")
    if account and entry["kind"] not in ("google", "remote"):
        raise ValueError(f"{source_id} has no accounts: it reads this computer.")


def sync(source_id: str, s: Session, account: str | None = None, opts: SyncOptions | None = None,
         progress: Progress | None = None, limit: int | None = None) -> dict:
    """Sync one source: each of its accounts (or `account`) in turn, continuing past one that fails. Returns the
    summed run_sync stats, with each account's under "accounts". Raises SetupNeeded when there is nothing to sync
    from, and SyncFailed (carrying the stats) naming every account that failed."""
    from igneous import jobs

    opts = opts or SyncOptions()
    check_options(source_id, opts, account)
    total = {"inserted": 0, "updated": 0, "unchanged": 0, "deleted": 0, "redactions": 0}
    per_account: dict[str, dict] = {}
    failed: dict[str, Exception] = {}
    done = 0
    report = progress or (lambda p, force=False: None)
    for name, make in BY_ID[source_id]["build"](s, account, opts):
        t = time.time()
        try:
            stats = jobs.run_connector(s, make(), name, lambda p, d=done, a=name: report(
                {"items": d + p["items"], "account": a}), limit)
        except Exception as e:  # one account's failure is reported, and the others still sync
            s.rollback()  # what the last checkpoint committed stays
            failed[name] = e
            continue
        per_account[name] = {**stats, "seconds": round(time.time() - t, 1)}
        for key in total:
            total[key] += stats[key]
        done += stats["inserted"] + stats["updated"] + stats["unchanged"]
    result = {**total, "accounts": per_account}
    if failed:
        reasons = {name: str(e) or type(e).__name__ for name, e in failed.items()}
        # A source on this computer has one account, "default": its reason needs no name.
        message = reasons["default"] if list(reasons) == ["default"] else \
            "; ".join(f"{name}: {why}" for name, why in reasons.items())
        raise SyncFailed(message, result) from next(iter(failed.values()))
    return result


def run_pipeline(s: Session, progress: Progress) -> dict:
    """Rebuild: what `igneous pipeline` runs, every stage but extract (it costs money), published at the end
    (igneous/pipeline.py). Progress names each stage as it starts and counts within long ones."""
    from igneous import pipeline

    return pipeline.run(s, progress=progress)


# ---- Google sign-in from the browser ----

_PENDING: dict[str, tuple[str, str]] = {}  # OAuth state -> (redirect_uri, PKCE code_verifier)


def google_authorization_url(redirect_uri: str) -> str:
    """Consent-screen URL. Desktop-type clients accept any http://localhost[:port][/path] redirect (Google's
    loopback rule), so the API's own callback works without registering it."""
    from google_auth_oauthlib.flow import Flow

    from igneous.connectors import google_auth

    flow = Flow.from_client_secrets_file(str(google_auth.client_secret_path()), google_auth.SCOPES,
                                         redirect_uri=redirect_uri)
    url, state = flow.authorization_url(prompt="consent select_account", access_type="offline")
    _PENDING[state] = (redirect_uri, flow.code_verifier)
    return url


def google_finish(s: Session, state: str, code: str) -> str:
    """Exchange the callback code for tokens, store them encrypted, return the account email."""
    from google_auth_oauthlib.flow import Flow
    from googleapiclient.discovery import build

    from igneous.connectors import google_auth

    if state not in _PENDING:
        raise ValueError("This sign-in link expired or was already used. Press Connect again.")
    redirect_uri, verifier = _PENDING.pop(state)
    flow = Flow.from_client_secrets_file(str(google_auth.client_secret_path()), google_auth.SCOPES,
                                         redirect_uri=redirect_uri, state=state)
    flow.code_verifier = verifier
    flow.fetch_token(code=code)
    email = build("oauth2", "v2", credentials=flow.credentials, cache_discovery=False).userinfo().get().execute()["email"]
    google_auth.save(s, email, flow.credentials)
    return email
