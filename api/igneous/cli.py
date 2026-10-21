import json
import time
from pathlib import Path

import typer

from igneous.config import settings
from igneous.db import SessionLocal

app = typer.Typer(no_args_is_help=True)


@app.command()
def sync(connector: str = typer.Argument(..., help="email | imessage | maccontacts | gmail | gcal | gcontacts | drive | fs "
                                                   "| ai_chats | github"),
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
    elif connector == "fs":
        from igneous.connectors.filesystem import FilesystemConnector

        roots = [path] if path else [Path(root.strip()) for root in settings.filesystem_roots.split(",") if root.strip()]
        if not roots:
            raise typer.BadParameter("fs requires --path or IGNEOUS_FILESYSTEM_ROOTS")
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


@app.command()
def pipeline(stage: list[str] = typer.Option(None, help="spine, chunk, embed, extract, topics, entities, self, resolve; "
                                                           "default all"),
             extract: int = typer.Option(0, help="LLM-extract up to N newest messages (costs money; 0 = skip)")):
    """Build the graph and index from whatever is new since the last run. Every stage is incremental."""
    from igneous.pipeline import stages as pipeline_stages

    stages = pipeline_stages(extract)
    with SessionLocal() as s:
        for name in stage or stages:
            t = time.time()
            n = stages[name](s)
            print(f"{name:7s} {n:>8} items  {time.time() - t:6.1f}s")


@app.command()
def extract(limit: int = typer.Option(200, help="max messages to process this run")):
    """Stage two: LLM extraction of MENTIONS / ABOUT with span provenance. Costs money; selective by default."""
    from igneous.graph import extract as ex

    t = time.time()
    with SessionLocal() as s:
        n = ex.run(s, limit=limit)
    print(f"extract {n:>6} items  {time.time() - t:6.1f}s")


@app.command()
def resolve(undo: bool = typer.Option(False, help="delete every SAME_AS merge")):
    """Entity resolution: write SAME_AS edges between Person nodes that are the same human."""
    from igneous.graph import resolve as er

    with SessionLocal() as s:
        print(f"removed {er.undo(s)} merges" if undo else er.run(s))


@app.command()
def export(target: str = typer.Argument(..., help="lightrag"), out: Path = typer.Argument(...),
           limit: int = typer.Option(None, help="newest N messages only")):
    """Export the graph for an external engine (criterion 5)."""
    if target != "lightrag":
        raise typer.BadParameter("only 'lightrag' is a file export; Bedrock reads the bedrock_kb view directly")
    from igneous.interop.lightrag import write

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
