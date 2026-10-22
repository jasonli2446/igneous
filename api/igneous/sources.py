"""The Sources registry behind the guided setup page (#101): every connector, in plain language.

Adding a connector or import format? Add an entry to SOURCES here and update web/src/app/settings (CLAUDE.md,
"Setup lives in the UI"). Static text lives in SOURCES; `describe()` adds live status; `build()` constructs the
connector exactly as `igneous sync <id>` does.
"""
import glob
import os
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

from igneous import jobs
from igneous.config import settings
from igneous.connectors import ai_chats, apple_notes, github, google_auth, imessage, mac_contacts, secrets

FDA_STEPS = [
    "Open System Settings, then Privacy & Security, then Full Disk Access.",
    "Turn it on for the app that runs the Igneous API (usually Terminal, iTerm, or your editor).",
    "Quit and reopen that app, restart the API, then press Check again here.",
]
GOOGLE_STEPS = [
    "One-time: create a Google OAuth client of type Desktop app and save its JSON as data/google/client_secret.json "
    "(README: Google). The page will say when it finds the file.",
    "Press Connect a Google account and pick the account in the Google window. Igneous only asks for read-only access.",
    "Come back here. The account appears below; add more accounts the same way.",
]
GOOGLE_NOTE = ("Sign-in returns to this computer at /sources/google/callback on the API. Desktop-type OAuth "
               "clients allow that automatically (any http://localhost address); a Web-type client would need "
               "that exact redirect URI added.")

# kind: google | local | remote | import | planned. cli: equivalent command shown in muted text.
SOURCES: list[dict] = [
    {"id": "gmail", "kind": "google", "name": "Gmail", "group": "Google",
     "reads": "Your email: senders, recipients, subjects, bodies, and threads. Promotions, Social and Forums are skipped.",
     "steps": GOOGLE_STEPS, "cli": "igneous connect google && igneous sync gmail"},
    {"id": "gcal", "kind": "google", "name": "Google Calendar", "group": "Google",
     "reads": "Your events: titles, times, attendees, and places.",
     "steps": GOOGLE_STEPS, "cli": "igneous sync gcal"},
    {"id": "gcontacts", "kind": "google", "name": "Google Contacts", "group": "Google",
     "reads": "Names, emails, phone numbers, and organizations on your contact cards. Helps merge duplicate people.",
     "steps": GOOGLE_STEPS, "cli": "igneous sync gcontacts"},
    {"id": "drive", "kind": "google", "name": "Google Drive", "group": "Google",
     "reads": "The text of your Docs, Sheets, Slides, PDFs, and other readable files.",
     "steps": GOOGLE_STEPS, "cli": "igneous sync drive"},
    {"id": "imessage", "kind": "local", "name": "Messages", "group": "This Mac",
     "reads": "iMessage and SMS conversations on this Mac, grouped into one entry per conversation per day.",
     "steps": ["Messages must be signed in on this Mac so the history is on disk.", *FDA_STEPS,
               "Press Sync now. The first sync reads the last 90 days."],
     "cli": "igneous sync imessage --since-days 90"},
    {"id": "maccontacts", "kind": "local", "name": "Contacts", "group": "This Mac",
     "reads": "Contacts.app cards (where iPhone contacts sync): names, phone numbers, and emails. "
              "Lets Messages show names instead of numbers.",
     "steps": [*FDA_STEPS, "Press Sync now."], "cli": "igneous sync maccontacts"},
    {"id": "ai_chats", "kind": "local", "name": "Claude Code and Codex chats", "group": "This Mac",
     "reads": "Your prompts and the assistant's written replies from coding sessions on this Mac. Tool output and "
              "file contents are skipped, and secrets are redacted.",
     "steps": ["Nothing to set up: it reads ~/.claude/projects and ~/.codex/sessions if you use those tools.",
               "Press Sync now."],
     "cli": "igneous sync ai_chats"},
    {"id": "fs", "kind": "local", "name": "Local files", "group": "This Mac",
     "reads": "Metadata for every non-secret file in the folders you choose, plus text from readable documents. "
              "Secrets are skipped and document text is redacted. Local text never reaches a model unless "
              "IGNEOUS_LOCAL_SOURCES_MODEL_ENABLED=true.",
     "steps": ["Set IGNEOUS_FILESYSTEM_ROOTS to a comma-separated list of folders (in api/.env or the API's "
               "environment), then restart the API. With no setting, existing Documents, Desktop, Downloads, and "
               "iCloud Drive folders are used.",
               "Press Sync now. Each sync reconciles deletions without reopening unchanged file bodies."],
     "cli": "igneous sync fs --path ~/Documents/notes"},
    {"id": "notes", "kind": "local", "name": "Apple Notes", "group": "This Mac",
     "reads": "Unlocked Notes.app titles and bodies from the local database. Bodies are redacted and never sent "
              "to a model unless IGNEOUS_LOCAL_SOURCES_MODEL_ENABLED=true.",
     "steps": [*FDA_STEPS, "Press Sync now. Locked notes are skipped."], "cli": "igneous sync notes"},
    {"id": "email", "kind": "local", "name": "Enron demo mailbox", "group": "Demo",
     "reads": "The public Enron email corpus. For demos and evaluation only; never mix it with your own data.",
     "steps": ["Run make enron once to download the mailboxes into data/maildir.", "Press Sync now."],
     "cli": "igneous sync email --mailbox allen-p"},
    {"id": "chatgpt", "kind": "import", "name": "ChatGPT export", "group": "Exports",
     "reads": "Your ChatGPT conversation history from OpenAI's data export.",
     "steps": ["Request the export in ChatGPT settings (Data controls, Export data).",
               "Open Import and upload the .zip you receive by email."]},
    {"id": "takeout", "kind": "import", "name": "Google Takeout", "group": "Exports",
     "reads": "Your Google My Activity (Search, YouTube, Maps, Gemini and the rest), Chrome history and bookmarks, "
              "and Maps places and visits. Gemini chats are part of My Activity (a Gemini-only export works too). "
              "Takeout data stays local and is never sent to a model unless you opt in (IGNEOUS_LLM_SOURCES).",
     "steps": ["Go to takeout.google.com and click Deselect all.",
               "Tick My Activity, Chrome, and Maps (your places). If you see Location History (Timeline), tick it too.",
               "On My Activity click Multiple formats and change Activity records from HTML to JSON. HTML is not read.",
               "Click Next step, keep one-time export and .zip, then Create export. It usually takes hours and may "
               "arrive as several zips.",
               "Download every zip. On Import choose Scan Downloads for takeout-*.zip, give a path, or upload them."],
     "cli": "igneous import takeout ~/Downloads/takeout-*.zip"},
    {"id": "linkedin", "kind": "import", "name": "LinkedIn", "group": "Exports",
     "reads": "Your LinkedIn profile (positions, education, projects, skills) and your connections with their company, "
              "position and connection date. Positions and education become your employers and schools; connections "
              "become named people linked to their companies. Login data is never read, and the data stays local "
              "unless you opt in (IGNEOUS_LLM_SOURCES).",
     "steps": ["On linkedin.com click Me, then Settings & Privacy, then Data privacy, then Get a copy of your data.",
               "For the fast option choose Want something in particular? and tick Connections only (usually minutes). "
               "For positions, education, projects and skills choose Download larger data archive "
               "(it can take up to 24 hours).",
               "Click Request archive and confirm your password. LinkedIn emails you a download link.",
               "Download the .zip. On Import upload it, or give its path or an extracted folder."],
     "cli": "igneous import linkedin ~/Downloads/Basic_LinkedInDataExport.zip"},
    {"id": "claude_ai", "kind": "import", "name": "Claude.ai export", "group": "Exports",
     "reads": "Your claude.ai conversation history from Anthropic's data export.",
     "steps": ["Request the export in claude.ai settings (Privacy, Export data).",
               "Open Import and upload the .zip you receive by email."]},
    {"id": "github", "kind": "remote", "name": "GitHub", "group": "Code",
     "reads": "Your repositories, README files, issues, pull requests, comments, and daily commit activity (read-only).",
     "steps": ["On github.com open Settings, Developer settings, Fine-grained tokens, and generate a token.",
               "Give it read-only access (Contents, Issues, Pull requests: Read) to the repositories you want.",
               "Paste it below. It is stored encrypted on this computer and never shown again."]},
]
BY_ID = {s["id"]: s for s in SOURCES}
SYNCABLE = {s["id"] for s in SOURCES if s["kind"] in ("google", "local", "remote")}
IMPORT_TOOL = {"chatgpt", "gemini", "claude_ai"}  # raw_meta.tool values stored under source 'ai_chats'


# ---- requirement checks: cheap, read-only, no network ----

def _sqlite_readable(path: str) -> str | None:
    """None if readable, else a plain-language reason (the Full Disk Access case is the usual one)."""
    if not os.path.exists(path):
        return f"{path} not found."
    try:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            db.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchall()
        finally:
            db.close()
    except (sqlite3.Error, PermissionError):
        return "macOS is blocking access. Grant Full Disk Access (steps above), restart the API, and check again."
    return None


def check_requirements(source_id: str) -> tuple[bool, str | None]:
    """(ok, message-when-not-ok). Never touches the network."""
    kind = BY_ID[source_id]["kind"]
    if kind == "google":
        if not (Path(settings.data_dir) / "google" / "client_secret.json").exists():
            return False, "No Google OAuth client found. Save its JSON as data/google/client_secret.json (step 1)."
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
            return False, "No Claude Code or Codex sessions found on this Mac."
    elif source_id == "fs":
        roots = _fs_roots()
        if not roots:
            return False, "No folders configured. Set IGNEOUS_FILESYSTEM_ROOTS (step 1) and restart the API."
        if missing := [r for r in roots if not r.is_dir()]:
            return False, f"Folder not found: {missing[0]}"
    elif source_id == "email":
        if not (Path(settings.data_dir) / "maildir").is_dir():
            return False, "No mailboxes at data/maildir. Run make enron first."
    return True, None


def _fs_roots() -> list[Path]:
    configured = [Path(r.strip()).expanduser() for r in settings.filesystem_roots.split(",") if r.strip()]
    if configured:
        return configured
    defaults = [Path.home() / name for name in ("Documents", "Desktop", "Downloads")]
    defaults.append(Path.home() / "Library/Mobile Documents/com~apple~CloudDocs")
    return [path for path in defaults if path.is_dir()]


# ---- live description ----

def describe(s: Session) -> list[dict]:
    counts = dict(s.execute(text("SELECT source, count(*) FROM source_items GROUP BY 1")).all())
    export_counts = dict(s.execute(text("""
        SELECT raw_meta->>'tool', count(*) FROM source_items WHERE source = 'ai_chats' GROUP BY 1""")).all())
    states: dict[str, list[tuple[str, object]]] = {}
    for connector, account, updated in s.execute(text("SELECT connector, account, updated_at FROM sync_state")):
        states.setdefault(connector, []).append((account, updated))
    latest = jobs.latest_by_source(s)
    google_accounts = google_auth.accounts(s)
    github_accounts = google_auth.accounts(s, "github")
    ai_local = sum(n for tool, n in export_counts.items() if tool not in IMPORT_TOOL)

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
        else:
            count = counts.get(sid, 0)
        last = max((u for _, u in states.get(sid, [])), default=None)
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
        if kind == "planned":
            status, message = "not_set_up", "Coming soon."
        elif sid == "github" and not github_accounts:
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
                   coming_soon=kind == "planned", job=job_view(job) if job else None)
        out.append(row)
    return out


def job_view(j: dict) -> dict:
    return {k: j[k] for k in ("id", "kind", "source", "account", "status", "started_at", "finished_at",
                              "progress", "error", "result")}


# ---- running ----

def build(source_id: str, s: Session, account: str | None, progress: Callable[..., None]) -> dict:
    """Run `igneous sync <source_id>` (same construction as cli.sync) and return summed run_sync stats."""
    from igneous.cli import _google_connector

    if source_id in ("gmail", "gcal", "gcontacts", "drive"):
        targets = [account] if account else google_auth.accounts(s)
        if not targets:
            raise google_auth.ReauthRequired("No Google account connected yet. Press Connect a Google account.")
        total = {"inserted": 0, "updated": 0, "unchanged": 0, "deleted": 0}
        done = 0
        for acct in targets:
            acct, creds = google_auth.load(s, acct)
            conn = _google_connector(source_id, creds, acct, None, False)
            stats = jobs.run_connector(s, conn, acct, lambda p, d=done: progress({"items": d + p["items"]}))
            for k in total:
                total[k] += stats[k]
            done += stats["inserted"] + stats["updated"] + stats["unchanged"]
        return total
    if source_id == "github":
        targets = [account] if account else secrets.accounts(s, "github")
        if not targets:
            raise RuntimeError("No GitHub token connected. Save a read-only token first.")
        total = {"inserted": 0, "updated": 0, "unchanged": 0, "deleted": 0}
        for acct in targets:
            _, token = secrets.load(s, "github", acct)
            stats = jobs.run_connector(s, github.GithubConnector(token, acct), acct, progress)
            for key in total:
                total[key] += stats[key]
        return total
    if source_id == "imessage":
        conn, acct = imessage.MessagesConnector(imessage.DEFAULT_DB), "default"
    elif source_id == "maccontacts":
        conn, acct = mac_contacts.MacContactsConnector(), "default"
    elif source_id == "notes":
        conn, acct = apple_notes.AppleNotesConnector(), "default"
    elif source_id == "ai_chats":
        conn, acct = ai_chats.AIChatConnector(), "default"
    elif source_id == "fs":
        from igneous.connectors.filesystem import FilesystemConnector

        conn, acct = FilesystemConnector(_fs_roots()), "default"
    elif source_id == "email":
        from igneous.connectors.email_maildir import MaildirConnector

        conn, acct = MaildirConnector(Path(settings.data_dir) / "maildir"), "default"
    else:
        raise ValueError(f"{source_id} cannot be synced")
    return jobs.run_connector(s, conn, acct, progress)


def run_pipeline(s: Session, progress: Callable[..., None]) -> dict:
    """The stages `igneous pipeline` runs by default, minus extract (it costs money)."""
    from igneous.pipeline import stages

    todo = [(name, fn) for name, fn in stages().items() if name != "extract"]
    done: dict[str, int] = {}
    for i, (name, fn) in enumerate(todo):
        progress({"stage": name, "step": i + 1, "steps": len(todo), "done": done}, force=True)
        t = time.time()
        done[name] = fn(s)
        s.commit()
        done[f"{name}_seconds"] = round(time.time() - t, 1)
    progress({"stage": None, "step": len(todo), "steps": len(todo), "done": done}, force=True)
    return done


# ---- Google sign-in from the browser ----

_PENDING: dict[str, tuple[str, str]] = {}  # OAuth state -> (redirect_uri, PKCE code_verifier)


def google_authorization_url(redirect_uri: str) -> str:
    """Consent-screen URL. Desktop-type clients accept any http://localhost[:port][/path] redirect (Google's
    loopback rule), so the API's own callback works without registering it."""
    from google_auth_oauthlib.flow import Flow

    flow = Flow.from_client_secrets_file(str(google_auth.client_secret_path()), google_auth.SCOPES,
                                         redirect_uri=redirect_uri)
    url, state = flow.authorization_url(prompt="consent select_account", access_type="offline")
    _PENDING[state] = (redirect_uri, flow.code_verifier)
    return url


def google_finish(s: Session, state: str, code: str) -> str:
    """Exchange the callback code for tokens, store them encrypted, return the account email."""
    from google_auth_oauthlib.flow import Flow
    from googleapiclient.discovery import build

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
