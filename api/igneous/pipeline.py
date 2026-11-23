"""The pipeline: every stage in dependency order, run the same way by `igneous pipeline` and by Rebuild on the
Sources page (sources.run_pipeline).

Source stages (redact, spine, chunk, embed and, when asked, extract) turn new records into nodes, chunks, embeddings
and model mentions; redact first brings records stored under older credential rules (connectors/redact.py) up to the
current ones, so nothing after it reads a credential they now match. Each commits as it goes: what it writes is new
data, safe to show at once, and a killed run resumes where it stopped.

Derived stages (self through corrections) recompute what is inferred from that data, in the order of what each
reads. They run in one transaction (`publishing`) that bumps the graph version once and commits once, after the last
of them: a reader keeps seeing the previous graph until the new one is published, and a failed run publishes
nothing. What a model was paid to answer outlives that transaction, because model results (llm.cache_store) and
verdicts (graph/verify.py) are stored the moment they arrive.

  self, resolve       who you are and which identities are one person: every later stage counts people by them
  thread_flags        which conversations are group conversations (counted in merged people)
  introductions       who introduced whom (merged people, never a service)
  topics, entities    topic merges, and the projects you return to with the people in them (never you)
  profile             your resume and LinkedIn (needs you)
  threads, backbone   life threads, then threads under the orgs on your resume (needs profile and threads)
  organizations       where people work (merged people)
  verify              checks the derived links every stage above wrote
  corrections         re-applies every correction committed before it, so a correction always wins

A derived stage reads and writes through the session it is given and never commits, rolls back or bumps the graph
version; it rebuilds only the rows it owns (its extractor), stores paid model results as they arrive, and leaves
corrections to the last step.
"""
import time
from collections.abc import Callable
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

Progress = Callable[..., None]  # progress(state: dict, force: bool = False): the run's state, see `run`


@dataclass(frozen=True)
class Context:
    """What a stage gets besides the session."""
    report: Callable[..., None]  # report(count=None, total=None, note=None): progress within the stage
    extract: int = 0             # messages extract may read with the model


@dataclass(frozen=True)
class Stage:
    name: str
    label: str                                   # what it does, as the Sources page shows it while it runs
    run: Callable[[Session, Context], object]    # returns a count, or a dict of counts
    derived: bool                                # runs inside the publishing transaction
    gated: bool = False                          # skips work while no source change is pending for it (dirty.py)


def _redact(s: Session, ctx: Context):
    from igneous.connectors.base import redact_stored
    return redact_stored(s)


def _spine(s: Session, ctx: Context):
    from igneous.graph import spine
    return spine.run(s)


def _chunk(s: Session, ctx: Context):
    from igneous.rag import chunking
    return chunking.run(s)


def _embed(s: Session, ctx: Context):
    from igneous.rag import embed
    return embed.run(s, progress=lambda done, total: ctx.report(count=done, total=total))


def _extract(s: Session, ctx: Context):
    from igneous.graph import extract
    return extract.run(s, limit=ctx.extract, log=lambda note: ctx.report(note=note.strip()),
                       progress=lambda done, total: ctx.report(count=done, total=total))


def _self(s: Session, ctx: Context):
    from igneous.graph import self_identity
    return self_identity.run(s)


def _resolve(s: Session, ctx: Context):
    from igneous.graph import resolve
    return resolve.run(s)


def _thread_flags(s: Session, ctx: Context):
    from igneous.graph import item_class
    return item_class.refresh_thread_flags(s)


def _introductions(s: Session, ctx: Context):
    from igneous.graph import history
    return history.derive_introductions(s)


def _topics(s: Session, ctx: Context):
    from igneous.graph import topics
    return topics.run(s)


def _entities(s: Session, ctx: Context):
    from igneous.graph import entities
    return entities.run(s, log=None)


def _profile(s: Session, ctx: Context):
    from igneous.graph import profile
    return profile.run(s, log=None)


def _threads(s: Session, ctx: Context):
    from igneous.graph import threads
    return threads.run(s, log=None)


def _backbone(s: Session, ctx: Context):
    from igneous.graph import backbone
    return backbone.run(s, log=None)


def _organizations(s: Session, ctx: Context):
    from igneous.graph import organizations
    return organizations.run(s)


def _verify(s: Session, ctx: Context):
    from igneous.graph import verify
    return verify.run(s, log=None)


def _corrections(s: Session, ctx: Context):
    from igneous.graph import overrides, verify
    return {"overrides": overrides.apply(s), "verdicts": verify.apply_verdicts(s)}


STAGES: tuple[Stage, ...] = (
    Stage("redact", "Removing credentials from records stored under older rules", _redact, derived=False),
    Stage("spine", "Reading your sources into people, conversations and events", _spine, derived=False),
    Stage("chunk", "Splitting text into searchable pieces", _chunk, derived=False),
    Stage("embed", "Indexing meaning for search", _embed, derived=False),
    Stage("extract", "Reading messages with the model for people, topics and promises", _extract, derived=False),
    Stage("self", "Working out which person is you", _self, derived=True),
    Stage("resolve", "Merging duplicate people", _resolve, derived=True, gated=True),
    Stage("thread_flags", "Finding your group conversations", _thread_flags, derived=True),
    Stage("introductions", "Finding who introduced whom", _introductions, derived=True),
    Stage("topics", "Merging duplicate topics", _topics, derived=True),
    Stage("entities", "Finding the projects, groups and series you return to", _entities, derived=True),
    Stage("profile", "Reading your resume and LinkedIn", _profile, derived=True),
    Stage("threads", "Finding trips, classes, jobs and projects", _threads, derived=True),
    Stage("backbone", "Placing them under your schools and workplaces", _backbone, derived=True),
    Stage("organizations", "Finding where people work", _organizations, derived=True),
    Stage("verify", "Checking each connection against its sources", _verify, derived=True),
    Stage("corrections", "Applying your corrections", _corrections, derived=True),
)
NAMES = tuple(st.name for st in STAGES)
GATED = frozenset(st.name for st in STAGES if st.gated)
FINAL = "corrections"  # every run ends with it
FIRST = "redact"  # every run that reads source records starts with it, so none of them reads an unredacted one


def plan(names: list[str] | None = None, extract: int = 0) -> list[Stage]:
    """The stages a run executes, in pipeline order: `names` (default: every stage except extract, which runs only
    when `extract`, the number of messages it may read with the model, is positive), starting with redact when a
    source stage runs and always ending with corrections. Raises ValueError for an unknown name, or for extract
    without a number of messages."""
    unknown = [n for n in names or () if n not in NAMES]
    if unknown:
        raise ValueError(f"unknown stage {', '.join(map(repr, unknown))}; the stages are {', '.join(NAMES)}")
    if names and "extract" in names and extract <= 0:
        raise ValueError("extract needs the number of messages to read with the model (--extract N)")
    wanted = set(names) if names else {n for n in NAMES if n != "extract" or extract > 0}
    if any(not st.derived for st in STAGES if st.name in wanted):
        wanted.add(FIRST)
    return [st for st in STAGES if st.name in wanted | {FINAL}]


class PublishBusy(RuntimeError):
    """A rebuild is publishing the graph, and a request waited for it as long as it may."""


CORRECTION_WAIT_MS = 15_000  # how long a request made through the API waits for a publishing rebuild


def _publish_lock(s: Session, function: str, wait_ms: int | None) -> None:
    """Take the publish lock in the session's transaction: `function` is pg_advisory_xact_lock for a publisher, the
    _shared variant for a correction. With `wait_ms`, give up after that long: roll the transaction back (whatever it
    did is undone) and raise PublishBusy."""
    if wait_ms is not None:
        s.execute(text(f"SET LOCAL lock_timeout = {int(wait_ms)}"))
    try:
        s.execute(text(f"SELECT {function}(hashtext('igneous:publish'))"))
    except OperationalError as e:
        if getattr(e.orig, "sqlstate", None) != "55P03":  # lock_not_available: the wait ran out
            raise
        s.rollback()
        raise PublishBusy("The graph is being rebuilt. Try again when Rebuild on the Sources page finishes.") from e
    if wait_ms is not None:
        s.execute(text("SET LOCAL lock_timeout = DEFAULT"))


@contextmanager
def publishing(s: Session, wait_ms: int | None = None):
    """One change to the derived graph, published at once: wait for any other publisher (at most `wait_ms`, if
    given), run the body in the session's transaction, then bump the graph version once and commit. An exception
    rolls everything back, so readers keep the previous graph. Corrections made through the API wait for it
    (`hold_off_publishing`)."""
    _publish_lock(s, "pg_advisory_xact_lock", wait_ms)
    try:
        yield
        from igneous.graph import dirty
        dirty.touch(s)
        s.commit()
    except BaseException:
        s.rollback()
        raise


def hold_off_publishing(s: Session) -> None:
    """For a correction made through the API, before it reads or writes the graph: wait until no rebuild is
    publishing, and keep the next one from starting until the caller's transaction ends. The correction then never
    interleaves with a rebuild's writes (so neither can deadlock the other) and lands on the graph the rebuild just
    published. Raises PublishBusy after CORRECTION_WAIT_MS."""
    _publish_lock(s, "pg_advisory_xact_lock_shared", CORRECTION_WAIT_MS)


@contextmanager
def _one_run_at_a_time(waiting: Callable[[], None]):
    """Hold the run lock while a run reads sources and publishes, so two runs never interleave their writes. It is a
    session lock on a connection of its own, which the stages' commits don't release."""
    from igneous.db import engine

    with engine.connect() as c:
        if not c.execute(text("SELECT pg_try_advisory_lock(hashtext('igneous:run'))")).scalar():
            waiting()
            c.execute(text("SELECT pg_advisory_lock(hashtext('igneous:run'))"))
        try:
            yield
        finally:
            c.execute(text("SELECT pg_advisory_unlock(hashtext('igneous:run'))"))


def run(s: Session, names: list[str] | None = None, *, extract: int = 0, progress: Progress | None = None,
        wait_ms: int | None = None) -> dict:
    """Run `plan(names, extract)`: the source stages, then the derived ones in one publishing transaction, which
    waits at most `wait_ms` (if given) for another publisher, then raises PublishBusy with nothing published.

    `progress(state, force)` gets the run's state when a stage starts (force=True) and as a long one counts
    (force=False): {stage, label, step, steps, count, total, note, results}, where `results` maps each finished stage
    to {result, seconds}. Returns `results`."""
    stages = plan(names, extract)
    notify = progress or (lambda state, force=False: None)
    state: dict = {"stage": None, "label": None, "step": 0, "steps": len(stages), "count": None, "total": None,
                   "note": None, "results": {}}

    def report(count: int | None = None, total: int | None = None, note: str | None = None) -> None:
        if note is None:
            state.update(count=count, total=total)
        else:
            state["note"] = note
        notify(dict(state))

    ctx = Context(report=report, extract=extract)

    def execute(st: Stage) -> None:
        state.update(stage=st.name, label=st.label, step=state["step"] + 1, count=None, total=None, note=None)
        notify(dict(state), True)
        started = time.monotonic()
        result = st.run(s, ctx)
        state["results"][st.name] = {"result": result, "seconds": round(time.monotonic() - started, 1)}

    reads_sources = any(not st.derived for st in stages)
    # A run that only recomputes (an API correction's rebuild, `--stage resolve`) needs only the publish lock.
    with _one_run_at_a_time(lambda: report(note="waiting for another pipeline run to finish")) if reads_sources \
            else nullcontext():
        for st in stages:
            if not st.derived:
                execute(st)
        with publishing(s, wait_ms):
            for st in stages:
                if st.derived:
                    execute(st)
    state.update(stage=None, label=None, count=None, total=None, note=None)
    notify(dict(state), True)
    return state["results"]
