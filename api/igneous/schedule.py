"""The scheduled sync (macOS LaunchAgent): every 30 minutes, sync each source you have set up, one at a time, then
rebuild the graph if anything is new.

A source counts as set up once it has items or a sync left its cursor, so sources whose requirements merely happen to
pass (a Documents folder, a downloaded Enron corpus) are never read uninvited, and the Demo group never is. Each sync
is a job like the Sources page's, so the page shows it running, and a source already syncing (Sync now, `igneous
sync`) is skipped; so is the rebuild while another build runs.
"""
import os
import platform
import plistlib
import subprocess
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

from igneous import jobs, pipeline, sources
from igneous.config import API_DIR
from igneous.db import SessionLocal

LABEL = "com.igneous.sync"
INTERVAL = 30 * 60


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _require_macos() -> None:
    if platform.system() != "Darwin":
        raise RuntimeError("Scheduling is available on macOS only.")


def install(command: list[str]) -> Path:
    _require_macos()
    path = plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Settings resolve against the api folder from anywhere; starting there too makes every relative path the
    # same as a command typed in api/.
    payload = {"Label": LABEL, "ProgramArguments": [*command, "schedule", "run"], "WorkingDirectory": str(API_DIR),
               "StartInterval": INTERVAL, "RunAtLoad": True,
               "StandardOutPath": str(Path.home() / "Library/Logs/igneous.log"),
               "StandardErrorPath": str(Path.home() / "Library/Logs/igneous.log")}
    with path.open("wb") as out:
        plistlib.dump(payload, out, sort_keys=True)
    # Replace a running agent: bootstrap fails while an older one is loaded.
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(path)], check=False, capture_output=True)
    subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)], check=False)
    return path


def uninstall() -> bool:
    _require_macos()
    path = plist_path()
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(path)], check=False)
    existed = path.exists()
    path.unlink(missing_ok=True)
    return existed


def status() -> dict:
    path = plist_path()
    return {"installed": path.exists(), "path": str(path), "interval_minutes": INTERVAL // 60}


def due(s: Session) -> list[dict]:
    """The Sources page rows of the syncable sources you have set up, outside the Demo group, in page order."""
    return [row for row in sources.describe(s)
            if row["id"] in sources.SYNCABLE and row["group"] != "Demo" and (row["items"] or row["last_sync"])]


def _pipeline_due(s: Session, changed: bool) -> bool:
    """Whether to rebuild: a sync changed something, the last rebuild didn't finish, items changed since it started,
    or a change (such as a deletion) is still pending for a stage (graph/dirty.py). A build job that succeeded counts
    as a rebuild only when it ran every stage a rebuild runs (its result names each, pipeline.run): one that ran some
    (`igneous pipeline --stage`, `igneous extract`, `igneous resolve`) leaves the rest behind."""
    if changed:
        return True
    last = s.execute(text("""SELECT status, started_at FROM jobs
                             WHERE source = 'pipeline' AND (status <> 'succeeded' OR result ?& CAST(:stages AS text[]))
                             ORDER BY started_at DESC LIMIT 1"""),
                     {"stages": [st.name for st in pipeline.plan()]}).first()
    if last is None or last.status != "succeeded":
        return True
    return bool(s.execute(text("SELECT EXISTS (SELECT 1 FROM source_items WHERE updated_at > :t) "
                               "OR EXISTS (SELECT 1 FROM pipeline_changes)"), {"t": last.started_at}).scalar())


def _outcome(job: dict) -> dict:
    return {"status": job["status"], "result": job["result"], **({"error": job["error"]} if job["error"] else {})}


def run(s: Session) -> dict[str, dict]:
    """Sync every set-up source in turn, then rebuild once if anything is new. Returns each source's outcome:
    {"status", "result", "error"} for a job that ran, or {"skipped": why}."""
    jobs.fail_interrupted(s)  # a scheduled sync killed mid-run (sleep, shutdown) left its jobs running
    rows = due(s)
    s.commit()  # no transaction stays open while the syncs run
    result: dict[str, dict] = {}
    changed = False
    for row in rows:
        sid = row["id"]
        if row["status"] == "syncing":
            result[sid] = {"skipped": "already syncing"}
            continue
        if not row["can_sync"]:
            result[sid] = {"skipped": row["message"] or "not ready to sync"}
            continue
        try:
            job = jobs.run("sync", sid, lambda session, progress, sid=sid: sources.sync(sid, session, progress=progress))
        except jobs.JobBusy:
            result[sid] = {"skipped": "already syncing"}
            continue
        result[sid] = _outcome(job)
        stats = job["result"] or {}
        changed |= any(stats.get(k) for k in ("inserted", "updated", "deleted"))
    if not _pipeline_due(s, changed):
        result["pipeline"] = {"skipped": "nothing new since the last rebuild"}
        return result
    try:
        result["pipeline"] = _outcome(jobs.run("pipeline", "pipeline", sources.run_pipeline))
    except jobs.JobBusy:
        result["pipeline"] = {"skipped": "already running"}
    return result


def run_now() -> dict[str, dict]:
    with SessionLocal() as s:
        return run(s)
