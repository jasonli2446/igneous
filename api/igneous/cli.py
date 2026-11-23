import json
import sys
import time
from enum import Enum
from pathlib import Path

import typer

from igneous.config import settings
from igneous.db import SessionLocal
from igneous.pipeline import STAGES

app = typer.Typer(no_args_is_help=True)
schedule_app = typer.Typer(no_args_is_help=True, help="macOS: sync your connected sources and rebuild the graph "
                                                      "every 30 minutes.")
app.add_typer(schedule_app, name="schedule")

# Commands that never open the database (the schedule group checks in `schedule run`, the one that does).
NO_DATABASE = {"synthetic-corpus", "verify-synthetic-corpus", "schedule"}


@app.callback()
def main(ctx: typer.Context):
    """Build and query your personal knowledge graph. Every command reads the database IGNEOUS_DATABASE_URL names
    (api/.env); run `igneous COMMAND --help` for one command."""
    if ctx.invoked_subcommand not in NO_DATABASE:
        _require_schema()


def _require_schema() -> None:
    from igneous.db import schema_status

    if problem := schema_status():
        typer.echo(f"igneous: {problem}", err=True)
        raise typer.Exit(code=2)


@schedule_app.command("install")
def schedule_install():
    """Install the 30-minute macOS incremental sync LaunchAgent."""
    from igneous import schedule
    path = schedule.install([sys.executable, "-m", "igneous.cli"])
    print(f"installed {path}; logs: ~/Library/Logs/igneous.log")


@schedule_app.command("status")
def schedule_status():
    """Show whether the macOS scheduled sync is installed."""
    from igneous import schedule
    print(json.dumps(schedule.status()))


@schedule_app.command("uninstall")
def schedule_uninstall():
    """Remove the macOS scheduled sync LaunchAgent."""
    from igneous import schedule
    print("removed" if schedule.uninstall() else "not installed")


@schedule_app.command("run", hidden=True)
def schedule_run():
    """LaunchAgent entry point; sources are serialized before one incremental pipeline rebuild."""
    from igneous import schedule

    _require_schema()
    print(json.dumps(schedule.run_now(), default=str))


@app.command()
def sync(connector: str = typer.Argument(..., help="email | imessage | maccontacts | gmail | gcal | gcontacts | drive | fs "
                                                   "| ai_chats | notes | github"),
         path: Path = typer.Option(None, help="source path; fs defaults to IGNEOUS_FILESYSTEM_ROOTS"),
         mailbox: list[str] = typer.Option(None, help="restrict to these mailboxes; repeatable"),
         limit: int = typer.Option(None, help="stop at the first checkpoint after this many items (resumable)"),
         since_days: int = typer.Option(None, help="first sync only: how far back to go (imessage, gmail, gcal)"),
         account: str = typer.Option(None, help="google: which connected account (default: every connected one)"),
         all_mail: bool = typer.Option(False, help="gmail: include Promotions/Social/Forums (skipped by default)")):
    """Pull items into source_items; fs deliberately performs a fresh complete scan."""
    from igneous.connectors.base import run_sync

    if connector == "email":
        from igneous.connectors.email_maildir import MaildirConnector

        conn = MaildirConnector(path or Path(settings.data_dir) / "maildir", mailbox or None)
    elif connector == "imessage":
        from igneous.connectors.imessage import DEFAULT_DB, MessagesConnector

        conn = MessagesConnector(path or DEFAULT_DB, since_days=since_days)
    elif connector == "ai_chats":
        from igneous.connectors.ai_chats import AIChatConnector

        conn = AIChatConnector()
    elif connector == "maccontacts":
        from igneous.connectors.mac_contacts import MacContactsConnector

        conn = MacContactsConnector()
    elif connector == "notes":
        from igneous.connectors.apple_notes import AppleNotesConnector

        conn = AppleNotesConnector(path) if path else AppleNotesConnector()
    elif connector == "fs":
        from igneous.connectors.filesystem import FilesystemConnector
        from igneous.sources import _fs_roots

        roots = [path] if path else _fs_roots()
        if not roots:
            raise typer.BadParameter("no default folders found; pass --path or set IGNEOUS_FILESYSTEM_ROOTS")
        conn = FilesystemConnector(roots)
    elif connector == "github":
        from igneous.connectors import secrets
        from igneous.connectors.github import GithubConnector

        with SessionLocal() as s:
            acct, token = secrets.load(s, "github", account)
        conn = GithubConnector(token, acct)
        with SessionLocal() as s:
            print(run_sync(s, conn, account=acct, limit=limit), flush=True)
        return
    elif connector in ("gmail", "gcal", "gcontacts", "drive"):
        from igneous.connectors import google_auth

        # No --account: sync every connected Google account in turn (each keeps its own cursor).
        with SessionLocal() as s:
            targets = [account] if account else google_auth.accounts(s)
        if not targets:
            raise typer.BadParameter("no Google account connected: run `igneous connect google`")
        for acct in targets:
            with SessionLocal() as s:
                acct, creds = google_auth.load(s, acct)
            conn = _google_connector(connector, creds, acct, since_days, all_mail)
            t = time.time()
            with SessionLocal() as s:
                print(acct, run_sync(s, conn, account=acct, limit=limit), f"in {time.time() - t:.1f}s", flush=True)
        return
    else:
        raise typer.BadParameter("unknown connector")
    t = time.time()
    with SessionLocal() as s:
        print(run_sync(s, conn, account=account or "default", limit=limit), f"in {time.time() - t:.1f}s")


def _google_connector(connector: str, creds, account: str, since_days: int | None, all_mail: bool):
    if connector == "gmail":
        from igneous.connectors.gmail import GmailConnector

        return GmailConnector(creds, account, since_days=since_days or 90,
                              query=None if all_mail else GmailConnector.DEFAULT_QUERY)
    if connector == "drive":
        from igneous.connectors.drive import DriveConnector

        return DriveConnector(creds, account)
    if connector == "gcal":
        from igneous.connectors.gcal import CalendarConnector

        return CalendarConnector(creds, account, since_days=since_days or 365)
    from igneous.connectors.gcal import ContactsConnector

    return ContactsConnector(creds, account)


import_app = typer.Typer(no_args_is_help=True, help="Import exports you downloaded.")
app.add_typer(import_app, name="import")


@import_app.command("takeout")
def import_takeout(paths: list[Path] = typer.Argument(None, help="Takeout .zip file(s) (pass every part of a split "
                                                                 "export), or an extracted folder"),
                   downloads: bool = typer.Option(False, "--downloads", help="also use ~/Downloads/takeout-*.zip")):
    """Google Takeout: My Activity (incl. Gemini), Chrome, Maps and Location History. Re-running is a no-op."""
    from igneous.connectors import takeout

    try:
        resolved = takeout.resolve_paths([str(p) for p in paths or []], downloads)
        takeout.preflight(resolved)
    except takeout.TakeoutError as e:
        raise typer.BadParameter(str(e)) from e
    t = time.time()
    with SessionLocal() as s:
        result = takeout.run_import(s, resolved, lambda p, force=False: print(
            f"\r  {p.get('items', 0)}/{p['items_total']} items" if "items_total" in p else "", end="", flush=True))
    print()
    for note in result.pop("notes", []):
        print("note:", note)
    print(result, f"in {time.time() - t:.1f}s")


@import_app.command("linkedin")
def import_linkedin(paths: list[Path] = typer.Argument(..., help="LinkedIn export .zip (or Connections-only zip), "
                                                                 "or an extracted folder")):
    """LinkedIn data export: your profile (positions, education, projects, skills) and connections. Re-running is a no-op."""
    from igneous.connectors import linkedin

    try:
        resolved = linkedin.resolve_paths([str(p) for p in paths])
        linkedin.preflight(resolved)
    except linkedin.LinkedInError as e:
        raise typer.BadParameter(str(e)) from e
    t = time.time()
    with SessionLocal() as s:
        result = linkedin.run_import(s, resolved)
    print(result, f"in {time.time() - t:.1f}s")


@app.command()
def connect(provider: str = typer.Argument(..., help="google"),
            browser: bool = typer.Option(True, help="open the browser; --no-browser just prints the URL")):
    """Sign in once per Google account (opens a browser; run again to add another account). The token is stored
    encrypted; Testing-mode tokens last 7 days."""
    if provider != "google":
        raise typer.BadParameter("only google")
    from igneous.connectors import google_auth

    with SessionLocal() as s:
        print("connected", google_auth.connect(s, open_browser=browser), flush=True)


StageName = Enum("StageName", [(st.name, st.name) for st in STAGES], type=str)


class _Printer:
    """`igneous pipeline` progress: a line per finished stage, its notes, and counts within a long stage at most
    every few seconds. Flushed, so a pipe or a log file shows it as it happens."""

    def __init__(self):
        self.printed: set[str] = set()
        self.notes: set[str] = set()
        self.counted = 0.0

    def __call__(self, state: dict, force: bool = False) -> None:
        for name, done in state["results"].items():
            if name not in self.printed:
                self.printed.add(name)
                result = done["result"]
                shown = result if isinstance(result, int) else json.dumps(result, default=str)
                print(f"{name:13s} {shown:>8}  {done['seconds']:6.1f}s", flush=True)
        if state.get("note") and (state["stage"], state["note"]) not in self.notes:
            self.notes.add((state["stage"], state["note"]))
            print(f"  {state['stage'] or 'pipeline'}: {state['note']}", flush=True)
        if state.get("count") is not None and time.monotonic() - self.counted >= 5:
            self.counted = time.monotonic()
            total = f" of {state['total']:,}" if state.get("total") else ""
            print(f"  {state['stage']}: {state['count']:,}{total}", flush=True)


def _run_stages(names: list[str] | None, extract: int = 0) -> None:
    from igneous import pipeline as runner

    try:
        runner.plan(names, extract)
    except ValueError as e:
        raise typer.BadParameter(str(e), param_hint="--stage") from e
    with SessionLocal() as s:
        runner.run(s, names, extract=extract, progress=_Printer())


@app.command()
def pipeline(stage: list[StageName] = typer.Option(
                 None, help="run only this stage (repeatable); stages run in pipeline order and every run ends with "
                            "corrections, which publishes. Default: every stage except extract"),
             extract: int = typer.Option(0, min=0, help="also read up to N newest messages with the model for "
                                                        "people, topics and promises (costs money; 0 = skip)")):
    """Build the graph: read what is new in your sources, then recompute everything derived from it and publish it
    at once (Rebuild on the Sources page does the same, without extract). Readers keep the previous graph until the
    new one is published."""
    _run_stages([st.value for st in stage] if stage else None, extract)


@app.command()
def extract(limit: int = typer.Option(200, min=1, help="max messages to read with the model this run")):
    """Read the newest messages of opted-in sources with the model: people and organizations mentioned (MENTIONS),
    topics (ABOUT), promises and decisions, each with the quoted span. Costs money."""
    _run_stages(["extract"], limit)


@app.command()
def resolve(undo: bool = typer.Option(False, help="instead, delete the merges entity resolution scored (SAME_AS "
                                                  "with extractor 'er'), recompute identity clusters from the links "
                                                  "that remain (contact cards, your accounts, merges you made) and "
                                                  "publish; the next `igneous resolve` scores them again")):
    """Entity resolution: score which people are the same human (SAME_AS, extractor 'er'), recompute identity
    clusters and publish."""
    from igneous import pipeline as runner
    from igneous.graph import resolve as er

    with SessionLocal() as s, runner.publishing(s):
        result = {"removed": er.undo(s)} if undo else er.run(s, rescore=True)
        runner.final_step(s)  # your corrections, applied to what it publishes
    print(json.dumps(result), flush=True)


@app.command()
def export(target: str = typer.Argument(..., help="lightrag"), out: Path = typer.Argument(...),
           limit: int = typer.Option(None, help="newest N messages only")):
    """Export the graph for an external engine (criterion 5)."""
    if target != "lightrag":
        raise typer.BadParameter("only 'lightrag' is a file export; Bedrock reads the bedrock_kb view directly")
    from igneous.interop.lightrag import write
    from igneous.llm import allowed_sources

    if not allowed_sources():
        raise typer.BadParameter("LightRAG sends the export to its model, so it holds only opted-in sources and "
                                 "none is: set IGNEOUS_LLM_SOURCES (email for the Enron demo)")

    with SessionLocal() as s:
        print(write(s, out, limit))


@app.command("synthetic-corpus")
def synthetic_corpus(out: Path = typer.Argument(..., help="directory for the generated fixture corpus")):
    """Create small, deterministic synthetic files and an ICS calendar fixture."""
    from igneous.synthetic_corpus import generate

    manifest = generate(out)
    print(f"wrote {manifest['document_count']} documents and "
          f"{manifest['calendar_event_count']} calendar events to {out}")


@app.command("verify-synthetic-corpus")
def verify_synthetic_corpus(out: Path = typer.Argument(..., help="generated fixture corpus directory")):
    """Verify a synthetic corpus was generated completely and without edits."""
    from igneous.synthetic_corpus import verify

    stats = verify(out)
    print(f"verified {stats['documents']} documents and {stats['calendar_events']} calendar events")


@app.command()
def check_graph():
    """Validate persisted graph invariants without changing any data."""
    from igneous.graph.invariants import check_graph as run_checks

    with SessionLocal() as s:
        violations = run_checks(s)
    if not violations:
        print("graph check passed (0 violations)")
        return
    for violation in violations:
        print(json.dumps(violation.as_dict(), sort_keys=True))
    raise typer.Exit(code=1)


@app.command()
def mcp():
    """Serve the graph to MCP clients (Claude Code, Claude Desktop, Cursor) over stdio. Read-only."""
    from igneous.mcp_server import main

    main()


@app.command()
def search(q: str, k: int = 5, mode: str = "hybrid"):
    """Hybrid search from the terminal."""
    from igneous.rag.search import hybrid

    with SessionLocal() as s:
        for h in hybrid(s, q, k=k, mode=mode):
            print(f"{h['score']:.4f} {','.join(h['matched']):14s} {h['title']!r} [{h['external_id']}]")


if __name__ == "__main__":
    app()
