"""macOS LaunchAgent for paced, serialized incremental syncs."""
import platform
import plistlib
import subprocess
import time
from pathlib import Path

from sqlalchemy.orm import Session

from igneous import jobs, sources
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
    payload = {"Label": LABEL, "ProgramArguments": [*command, "schedule", "run"],
               "StartInterval": INTERVAL, "RunAtLoad": True,
               "StandardOutPath": str(Path.home() / "Library/Logs/igneous.log"),
               "StandardErrorPath": str(Path.home() / "Library/Logs/igneous.log")}
    with path.open("wb") as out:
        plistlib.dump(payload, out, sort_keys=True)
    subprocess.run(["launchctl", "bootstrap", f"gui/{__import__('os').getuid()}", str(path)], check=False)
    return path


def uninstall() -> bool:
    _require_macos()
    path = plist_path()
    subprocess.run(["launchctl", "bootout", f"gui/{__import__('os').getuid()}", str(path)], check=False)
    existed = path.exists()
    path.unlink(missing_ok=True)
    return existed


def status() -> dict:
    path = plist_path()
    return {"installed": path.exists(), "path": str(path), "interval_minutes": INTERVAL // 60}


def _wait(job_id: str) -> dict:
    while True:
        with SessionLocal() as s:
            job = jobs.get(s, job_id)
        if job and job["status"] != "running":
            return job
        time.sleep(0.2)


def run(s: Session) -> dict[str, dict]:
    """Sync eligible connected sources one at a time, then rebuild once.

    Skips sources the browser/API is already syncing: connector cursors stay safe,
    and Gmail's connector retains its own quota pacing.
    """
    active = {j["source"] for j in jobs.latest_by_source(s).values() if j["status"] == "running"}
    result: dict[str, dict] = {}
    for source in sources.SYNCABLE:
        if source in active:
            result[source] = {"skipped": "already running"}
            continue
        info = next(item for item in sources.describe(s) if item["id"] == source)
        if not info["can_sync"]:
            continue
        try:
            job = jobs.start("sync", source, lambda session, progress, source=source: sources.build(source, session, None, progress))
        except jobs.JobBusy:
            result[source] = {"skipped": "already running"}
            continue
        result[source] = _wait(job["id"])["result"] or {}
    try:
        job = jobs.start("pipeline", "pipeline", sources.run_pipeline)
    except jobs.JobBusy:
        result["pipeline"] = {"skipped": "already running"}
    else:
        result["pipeline"] = _wait(job["id"])["result"] or {}
    return result


def run_now() -> dict[str, dict]:
    with SessionLocal() as s:
        return run(s)
