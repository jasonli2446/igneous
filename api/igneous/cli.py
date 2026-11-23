import json
import sys
import time
from pathlib import Path

import typer

from igneous import sources
from igneous.db import SessionLocal

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


def _counter(label: str = "items"):
    """A progress callback that keeps one line on a terminal up to date, at most every half second."""
    shown = [0.0]

    def progress(p: dict, force: bool = False) -> None:
        if sys.stderr.isatty() and (force or time.monotonic() - shown[0] >= 0.5) and "items" in p:
            shown[0] = time.monotonic()
            total = f" of {p['items_total']}" if p.get("items_total") else ""
            print(f"\r  {p['items']}{total} {label}", end="", file=sys.stderr, flush=True)
    return progress


def _stats(stats: dict) -> str:
    return (f"{stats['inserted']} new, {stats['updated']} updated, {stats['unchanged']} unchanged, "
            f"{stats['deleted']} deleted, {stats['redactions']} secrets removed")


SOURCE_HELP = f"One of: {', '.join(sources.SYNCABLE)}."
MAILBOX_HELP = f"email: read these mailboxes (repeatable; default {', '.join(sources.DEMO_MAILBOXES)})"
SINCE_HELP = ("first sync only: how many days back (default: "
              + ", ".join(f"{k} {v}" for k, v in sources.FIRST_SYNC_DAYS.items()) + ")")


@app.command()
def sync(source: str = typer.Argument(..., metavar="SOURCE", help=SOURCE_HELP),
         path: Path = typer.Option(None, help="fs: this one folder instead of the configured ones "
                                              "(IGNEOUS_FILESYSTEM_ROOTS); imessage, notes: another database; "
                                              "email: another maildir folder"),
         mailbox: list[str] = typer.Option(None, help=MAILBOX_HELP),
         limit: int = typer.Option(None, help="stop at the first checkpoint after this many items (resumable)"),
         since_days: int = typer.Option(None, help=SINCE_HELP),
         account: str = typer.Option(None, help="Google sources and github: only this connected account "
                                                "(default: every one)"),
         all_mail: bool = typer.Option(None, "--all-mail/--no-all-mail",
                                       help="gmail: include Promotions/Social/Forums (skipped by default); the "
                                            "account remembers the choice")):
    """Sync a source into the database, as Sync now on the Sources page does. Each sync continues where the last
    one stopped; run `igneous pipeline` afterwards to add what is new to the graph."""
    opts = sources.SyncOptions(path=path, mailboxes=tuple(mailbox or ()), since_days=since_days, all_mail=all_mail)
    try:
        sources.check_options(source, opts, account)
    except ValueError as e:
        raise typer.BadParameter(str(e)) from e
    with SessionLocal() as s:
        try:
            result = sources.sync(source, s, account, opts, progress=_counter(), limit=limit)
        except sources.SetupNeeded as e:
            typer.echo(f"igneous: {e}", err=True)
            raise typer.Exit(code=2) from e
        except sources.SyncFailed as e:
            for name, stats in e.result["accounts"].items():
                print(f"{name}: {_stats(stats)} in {stats['seconds']}s")
            typer.echo(f"\nigneous: {source} sync failed: {e}", err=True)
            raise typer.Exit(code=1) from e
    if sys.stderr.isatty():
        print(file=sys.stderr)
    for name, stats in result["accounts"].items():
        print(f"{'' if name == 'default' else name + ': '}{_stats(stats)} in {stats['seconds']}s")


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


@import_app.command("ai-export")
def import_ai_export(path: Path = typer.Argument(..., exists=True, dir_okay=False,
                                                 help="the ChatGPT or Claude.ai export .zip as emailed, or the "
                                                      "conversations.json inside it; a Gemini Apps MyActivity.json "
                                                      "from Takeout works too"),
                     provider: str = typer.Option(None, help="chatgpt, claude_ai or gemini (default: recognised "
                                                             "from the file)")):
    """ChatGPT and Claude.ai chat history: your messages and the assistant's prose, one item per conversation per day.
    Re-running is a no-op."""
    from igneous.connectors import ai_exports

    t = time.time()
    with SessionLocal() as s:
        try:
            result = ai_exports.run_import(s, path, provider)
        except ai_exports.ExportError as e:
            raise typer.BadParameter(str(e)) from e
    print(f"{result['provider']}: {result['conversations']} conversations; {_stats(result)} in {time.time() - t:.1f}s")


@app.command()
def connect(provider: str = typer.Argument(..., metavar="PROVIDER", help="google or github"),
            browser: bool = typer.Option(True, help="google: open the browser; --no-browser just prints the URL")):
    """Connect an account, read-only; tokens are stored encrypted.

    google: sign in through the browser, once per account (run it again to add another). A Google project in Testing
    mode issues tokens that last 7 days: connect again when a sync says the token expired.

    github: a fine-grained token with Contents, Issues and Pull requests: Read, typed at the hidden prompt or given
    on stdin (`igneous connect github < token.txt`). Saving a token for the same account replaces it."""
    if provider == "google":
        from igneous.connectors import google_auth

        with SessionLocal() as s:
            print("connected", google_auth.connect(s, open_browser=browser), flush=True)
    elif provider == "github":
        from igneous.connectors import github, secrets

        token = typer.prompt("GitHub token", hide_input=True) if sys.stdin.isatty() else sys.stdin.read()
        try:
            login = github.account_for(token)
        except github.GithubError as e:
            raise typer.BadParameter(str(e)) from e
        with SessionLocal() as s:
            secrets.save(s, "github", login, token.strip())
        print("connected", login)
    else:
        raise typer.BadParameter("connect google or github")


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
            # Stages return a count or a stats dict; print either.
            shown = n if isinstance(n, int) else json.dumps(n, default=str)
            print(f"{name:7s} {shown:>8} items  {time.time() - t:6.1f}s")


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
