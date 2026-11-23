"""Hosted model calls, their result cache, and the one rule for what a hosted model may read.

Every hosted call follows the same policy: a prompt carries text only from records in opted-in sources
(`allowed_sources`), names only of people and things that appear in such records under a name grounded in them
(`nameable`, `visible_nodes`), and counts for everything else. A cached model result may be reused whatever the
policy says now: it discloses nothing new.
"""
import json

from sqlalchemy import text
from sqlalchemy.orm import Session

from igneous import source_catalog
from igneous.config import settings
from igneous.graph.identity import members_sql as identity_members
from igneous.providers import LLMDisabled, ProviderError, ProviderUnavailable, bedrock_key, chat

__all__ = ["LLMDisabled", "ProviderError", "ProviderUnavailable", "allowed_sources", "cache_lookup", "cache_store",
           "complete", "model_status", "nameable", "require_whole_corpus", "structured", "visible_nodes"]

LOCAL_SOURCES = source_catalog.LOCAL_TEXT  # also need IGNEOUS_LOCAL_SOURCES_MODEL_ENABLED


def structured(system: str, user: str, schema: dict, max_tokens: int = 4000, effort: str = "low",
               model: str | None = None) -> tuple[dict, dict]:
    """One schema-constrained chat call (on `model`, else IGNEOUS_LLM_MODEL) returning parsed JSON and usage. Raises
    the providers' errors: LLMDisabled, ProviderUnavailable, or ProviderError, which also covers an answer that
    isn't the JSON object the schema asks for."""
    result = chat([
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ], schema, max_tokens=max_tokens, effort=effort, model=model)
    try:
        parsed = json.loads(result.text)
    except json.JSONDecodeError as e:
        raise ProviderError(f"the model's answer is not JSON: {e}") from e
    if not isinstance(parsed, dict):
        raise ProviderError("the model's answer is not a JSON object")
    return parsed, result.usage


def complete(messages: list[dict], max_tokens: int = 2000, effort: str = "low", model: str | None = None) -> str:
    """One free-text chat call (on `model`, else IGNEOUS_LLM_MODEL) for a caller that drives the conversation itself,
    such as another engine answering from an export of the graph. `messages` are {role, content} with an optional
    system message; their content follows the same rule as every prompt. Raises the providers' errors."""
    return chat(messages, max_tokens=max_tokens, effort=effort, model=model).text


def cache_lookup(key: str) -> dict | None:
    """The model result stored under `key` (llm_cache), or None."""
    from igneous.db import SessionLocal

    with SessionLocal() as cs:
        return cs.execute(text("SELECT response FROM llm_cache WHERE cache_key = :k"), {"k": key}).scalar()


def cache_store(key: str, value: dict, replace: bool = False) -> None:
    """Store a model result under `key` in a transaction of its own, committed now: a paid result outlives the
    caller's rollback or a killed run, and never waits for the caller's transaction. `replace` overwrites an older
    entry; otherwise the first stored result stays."""
    from igneous.db import SessionLocal

    conflict = "DO UPDATE SET response = EXCLUDED.response, created_at = now()" if replace else "DO NOTHING"
    with SessionLocal() as cs:
        cs.execute(text(f"INSERT INTO llm_cache (cache_key, response) VALUES (:k, CAST(:r AS jsonb)) "
                        f"ON CONFLICT (cache_key) {conflict}"), {"k": key, "r": json.dumps(value)})
        cs.commit()


def allowed_sources(value: str | None = None) -> frozenset[str]:
    """Stored source values whose records may reach a hosted model: the ids in IGNEOUS_LLM_SOURCES.

    `takeout` (the Sources page id) stands for every Takeout product. Local files and Apple Notes also need
    IGNEOUS_LOCAL_SOURCES_MODEL_ENABLED=true. Empty, the default, allows nothing."""
    out: set[str] = set()
    for source in (value if value is not None else settings.llm_sources).split(","):
        source = source.strip()
        if source == "takeout":
            out.update(source_catalog.of_connector("takeout"))
        elif source:
            out.add(source)
    if not settings.local_sources_model_enabled:
        out.difference_update(LOCAL_SOURCES)
    return frozenset(out)


def visible_nodes(s: Session, ids, sources: frozenset[str] | None = None) -> set[int]:
    """The nodes among `ids` a prompt may name (`nameable`)."""
    return set(nameable(s, ids, sources))


# The records a node appears in (the node's own record, an edge's record or the record of the node at its other end,
# an edge's evidence), from allowed sources, as EXISTS clauses over `m`; `{match}` narrows the record `si`.
_APPEARS = """(
    EXISTS (SELECT 1 FROM nodes x JOIN source_items si ON si.id = x.source_item_id
            WHERE x.id = m AND si.source = ANY(CAST(:src AS text[])) {match})
    OR EXISTS (SELECT 1 FROM edges e JOIN source_items si ON si.id = e.source_item_id
               WHERE e.src = m AND si.source = ANY(CAST(:src AS text[])) {match})
    OR EXISTS (SELECT 1 FROM edges e JOIN source_items si ON si.id = e.source_item_id
               WHERE e.dst = m AND si.source = ANY(CAST(:src AS text[])) {match})
    OR EXISTS (SELECT 1 FROM edges e JOIN nodes o ON o.id = e.dst JOIN source_items si ON si.id = o.source_item_id
               WHERE e.src = m AND si.source = ANY(CAST(:src AS text[])) {match})
    OR EXISTS (SELECT 1 FROM edges e JOIN nodes o ON o.id = e.src JOIN source_items si ON si.id = o.source_item_id
               WHERE e.dst = m AND si.source = ANY(CAST(:src AS text[])) {match})
    OR EXISTS (SELECT 1 FROM edges e JOIN edge_evidence ev ON ev.edge_id = e.id
               JOIN source_items si ON si.id = ev.source_item_id
               WHERE (e.src = m OR e.dst = m) AND si.source = ANY(CAST(:src AS text[])) {match}))"""
# ...whose text holds the name being checked (:name per row, lowercase).
_HOLDS = ("AND strpos(lower(coalesce(si.title, '') || ' ' || coalesce(si.body_text, '') || ' ' || "
          "CAST(si.raw_meta AS text)), {name}) > 0")


def _named_elsewhere(type_: str, props: dict, own_record_allowed: bool) -> bool:
    """Whether a node's name came from outside the records it appears in, so the name itself must be found there:
    a contact card's, LinkedIn's or your resume's name for a person or another identity's name shown for them; any
    organization's (contact cards, LinkedIn, your resume, a domain's signatures, a fact); a Project named by your
    resume, by you or by an agent (a fact brought it in: graph/facts.py); a chat or folder (named by its own source,
    which may not be allowed)."""
    if type_ == "Person":
        return props.get("named_by") in ("contacts", "linkedin", "profile") or "shown_name" in props
    if type_ == "Project":
        return bool(props.get("renamed")) or props.get("named_from") in ("profile", "you") or "added_by" in props
    if type_ == "Organization":
        return True
    return not own_record_allowed


def nameable(s: Session, ids, sources: frozenset[str] | None = None) -> dict[int, str]:
    """The nodes among `ids` a prompt may name, each with the name it may use. A node qualifies when it appears in a
    record from an allowed source (the record is the node itself, the node at the other end of one of its edges, or
    that edge's evidence; for a person, any of their identities' records) and its name is grounded in such records:
    read from them (a sender's display name, a calendar title, a term found in them) or given by a model that read
    only them (a judged thread). A name that came from elsewhere (`_named_elsewhere`), or a person's that only another
    of their identities' records hold, must itself appear in one of them, in any identity's records for a person;
    failing that, a Project you renamed is named as it was derived."""
    ids = sorted({int(i) for i in ids if i is not None})
    allowed = sorted(allowed_sources() if sources is None else sources)
    if not ids or not allowed:
        return {}
    rows = s.execute(text(f"""
        WITH c AS (SELECT n.*, EXISTS (SELECT 1 FROM (SELECT n.id AS m) q WHERE {_APPEARS.format(match='')}) AS direct
                   FROM nodes n WHERE n.id = ANY(CAST(:ids AS bigint[])))
        SELECT c.id, c.type, c.name, c.props, c.direct,
               COALESCE(own.source = ANY(CAST(:src AS text[])), false) AS own
        FROM c LEFT JOIN source_items own ON own.id = c.source_item_id
        WHERE c.direct OR (c.type = 'Person' AND EXISTS (
            SELECT 1 FROM ({identity_members('c.id')}) q(m) WHERE {_APPEARS.format(match='')}))
    """), {"ids": ids, "src": allowed}).all()
    out = {r.id: r.name for r in rows if r.direct and not _named_elsewhere(r.type, r.props or {}, r.own)}
    check = {r.id: r for r in rows if r.id not in out}
    if not check:
        return out
    # Names from elsewhere: kept where one of the records (any identity's, for a person) holds the name.
    candidates = [{"id": r.id, "name": r.name.lower()} for r in check.values()]
    candidates += [{"id": r.id, "name": (r.props or {})["original_name"].lower()} for r in check.values()
                   if r.type == "Project" and (r.props or {}).get("original_name")
                   and not _named_elsewhere("Project", {**r.props, "renamed": False}, r.own)]
    held = s.execute(text(f"""
        WITH c AS (SELECT * FROM jsonb_to_recordset(CAST(:c AS jsonb)) AS c(id bigint, name text)),
        ident AS (SELECT c.id, c.name, COALESCE((SELECT canonical_id FROM canonical WHERE node_id = c.id), c.id) AS head
                  FROM c),
        mem AS (SELECT id, name, head AS m FROM ident UNION SELECT id, name, id FROM ident
                UNION SELECT i.id, i.name, k.node_id FROM ident i JOIN canonical k ON k.canonical_id = i.head)
        SELECT DISTINCT id, name FROM mem WHERE {_APPEARS.format(match=_HOLDS.format(name='mem.name'))}
    """), {"c": json.dumps(candidates), "src": allowed}).all()
    for nid, name in held:
        r = check[nid]
        if name == r.name.lower():
            out[nid] = r.name
        elif nid not in out:
            out[nid] = r.props["original_name"]
    return out


def require_whole_corpus(s: Session) -> None:
    """For eval and demo tools that send records from the whole database: stop unless every source in it is
    opted in (IGNEOUS_LLM_SOURCES=email for the Enron demo database)."""
    present = set(s.execute(text("SELECT DISTINCT source FROM source_items")).scalars())
    missing = sorted(present - allowed_sources())
    if missing:
        raise SystemExit(f"This sends records from the whole database to the model. Opt in every source it holds "
                         f"(IGNEOUS_LLM_SOURCES); not opted in: {', '.join(missing)}")


def model_status() -> dict:
    """What the Sources page says about hosted model use."""
    return {"enabled": settings.llm_enabled, "configured": bool(bedrock_key()),
            "sources": sorted(allowed_sources()), "local_sources": settings.local_sources_model_enabled}
