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
version; it rebuilds only the rows it owns (its extractor) through graph/derived.py's Writer, which never writes a link
you rejected, stores paid model results as they arrive, and leaves corrections to the last step.

Corrections never wait for a rebuild to finish (graph/corrections.py): one made while the derived phase runs is
recorded at once and applied by this run's final step, which holds the publish lock only for itself and the commit.
"""
import time
from collections.abc import Callable
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session


def watermark(s: Session, stage: str) -> datetime:
    s.execute(text("INSERT INTO pipeline_state (stage) VALUES (:s) ON CONFLICT DO NOTHING"), {"s": stage})
    return s.execute(text("SELECT watermark FROM pipeline_state WHERE stage = :s"), {"s": stage}).scalar_one()


def advance(s: Session, stage: str, to: datetime) -> None:
    s.execute(text("UPDATE pipeline_state SET watermark = :t, updated_at = now() WHERE stage = :s"),
              {"s": stage, "t": to})


Progress = Callable[..., None]  # progress(state: dict, force: bool = False): the run's state, see `run`


@dataclass(frozen=True)
class Context:
    """What a stage gets besides the session."""
    # report(count=None, total=None, note=None): progress within the stage. A count (of a total) replaces the last
    # one, a note replaces the last note and leaves the count, and a call with neither clears the count.
    report: Callable[..., None]
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
    return threads.run(s, log=None, report=ctx.report)


def _backbone(s: Session, ctx: Context):
    from igneous.graph import backbone
    return backbone.run(s, log=None)


def _organizations(s: Session, ctx: Context):
    from igneous.graph import organizations
    return organizations.run(s)


def _verify(s: Session, ctx: Context):
    from igneous.graph import verify
    return verify.run(s, log=None, progress=lambda done, total: ctx.report(count=done, total=total))


def _corrections(s: Session, ctx: Context):
    return final_step(s)


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


# Advisory locks (hashtext of the name). A run holds RUN while it reads sources and publishes, and SOURCES while its
# source stages run, until it holds DERIVING for its derived phase; the final step holds PUBLISH exclusively until the
# commit, while a correction records its intent under PUBLISH shared. A correction applies in place only while
# neither SOURCES nor DERIVING is held, and then under PUBLISH exclusively, like a final step (graph/corrections.py).
RUN_LOCK = "igneous:run"
SOURCES_LOCK = "igneous:sources"
DERIVING_LOCK = "igneous:deriving"
PUBLISH_LOCK = "igneous:publish"


def _lock(s: Session, name: str) -> None:
    s.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": name})


@contextmanager
def publishing(s: Session):
    """One change to the derived graph, published at once: hold the deriving lock (a rebuild deriving waits for any
    other, and a correction meanwhile leaves its application to this rebuild's final step), run the body in the
    session's transaction, then bump the graph version once and commit. The body ends with `final_step`. An exception
    rolls everything back, so readers keep the previous graph."""
    _lock(s, DERIVING_LOCK)
    try:
        yield
        from igneous.graph import dirty
        dirty.touch(s)
        s.commit()
    except BaseException:
        s.rollback()
        raise


def final_step(s: Session) -> dict:
    """The last step of every rebuild: wait for corrections still being recorded (the exclusive publish lock, held
    until the commit), then apply every correction recorded before it (graph/corrections.py apply_all)."""
    from igneous.graph import corrections

    _lock(s, PUBLISH_LOCK)
    return corrections.apply_all(s)


@contextmanager
def _one_run_at_a_time(waiting: Callable[[], None]):
    """Hold the run lock while a run reads sources and publishes, so two runs never interleave their writes, and the
    sources lock until the caller says its source stages are done (yields that call). Both are session locks on a
    connection of their own, which the stages' commits don't release."""
    from igneous.db import engine

    with engine.connect() as c:
        if not c.execute(text("SELECT pg_try_advisory_lock(hashtext(:k))"), {"k": RUN_LOCK}).scalar():
            waiting()
            c.execute(text("SELECT pg_advisory_lock(hashtext(:k))"), {"k": RUN_LOCK})
        c.execute(text("SELECT pg_advisory_lock(hashtext(:k))"), {"k": SOURCES_LOCK})
        reading = [True]

        def done_reading() -> None:
            if reading.pop() if reading else False:
                c.execute(text("SELECT pg_advisory_unlock(hashtext(:k))"), {"k": SOURCES_LOCK})
        try:
            yield done_reading
        finally:
            done_reading()
            c.execute(text("SELECT pg_advisory_unlock(hashtext(:k))"), {"k": RUN_LOCK})


def run(s: Session, names: list[str] | None = None, *, extract: int = 0, progress: Progress | None = None) -> dict:
    """Run `plan(names, extract)`: the source stages, then the derived ones in one publishing transaction.

    `progress(state, force)` gets the run's state when a stage starts (force=True) and as a long one counts
    (force=False): {stage, label, step, steps, count, total, note, results}, where `results` maps each finished stage
    to {result, seconds}. Returns `results`."""
    stages = plan(names, extract)
    notify = progress or (lambda state, force=False: None)
    state: dict = {"stage": None, "label": None, "step": 0, "steps": len(stages), "count": None, "total": None,
                   "note": None, "results": {}}

    def report(count: int | None = None, total: int | None = None, note: str | None = None) -> None:
        if count is not None or note is None:
            state.update(count=count, total=total)
        if note is not None:
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
    # A run that only recomputes (`--stage resolve`) reads no source: the deriving lock is all it needs.
    with _one_run_at_a_time(lambda: report(note="waiting for another pipeline run to finish")) if reads_sources \
            else nullcontext(lambda: None) as done_reading:
        for st in stages:
            if not st.derived:
                execute(st)
        with publishing(s):
            done_reading()  # only now: a correction never applies in place between the two phases
            for st in stages:
                if st.derived:
                    execute(st)
    state.update(stage=None, label=None, count=None, total=None, note=None)
    notify(dict(state), True)
    return state["results"]
