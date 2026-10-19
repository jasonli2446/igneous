import time
from pathlib import Path

import typer

from igneous.config import settings
from igneous.db import SessionLocal

app = typer.Typer(no_args_is_help=True)


@app.command()
def sync(connector: str = typer.Argument(..., help="email"),
         path: Path = typer.Option(None, help="maildir root (default: <data_dir>/maildir)"),
         mailbox: list[str] = typer.Option(None, help="restrict to these mailboxes; repeatable"),
         limit: int = typer.Option(None, help="stop after about this many items")):
    """Pull items from a source into source_items. Resumable: re-run continues from the saved cursor."""
    from igneous.connectors.base import run_sync

    if connector != "email":
        raise typer.BadParameter("only 'email' exists so far")
    from igneous.connectors.email_maildir import MaildirConnector

    conn = MaildirConnector(path or Path(settings.data_dir) / "maildir", mailbox or None)
    t = time.time()
    with SessionLocal() as s:
        print(run_sync(s, conn, limit=limit), f"in {time.time() - t:.1f}s")


@app.command()
def pipeline(stage: list[str] = typer.Option(None, help="spine, chunk, embed; default all")):
    """Build the graph and index from whatever source_items are new since the last run."""
    from igneous.graph import spine
    from igneous.rag import chunking, embed

    stages = {"spine": spine.run, "chunk": chunking.run, "embed": embed.run}
    with SessionLocal() as s:
        for name in stage or stages:
            t = time.time()
            n = stages[name](s)
            print(f"{name:6s} {n:>8} items  {time.time() - t:6.1f}s")


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
def search(q: str, k: int = 5, mode: str = "hybrid"):
    """Hybrid search from the terminal."""
    from igneous.rag.search import hybrid

    with SessionLocal() as s:
        for h in hybrid(s, q, k=k, mode=mode):
            print(f"{h['score']:.4f} {','.join(h['matched']):14s} {h['title']!r} [{h['external_id']}]")


if __name__ == "__main__":
    app()
