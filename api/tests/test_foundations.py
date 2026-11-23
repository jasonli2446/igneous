"""Shared helpers: identity, visibility, the source catalog, the model-result cache, settings paths."""
import json

from sqlalchemy import text

from igneous import llm, source_catalog, sources
from igneous.config import API_DIR, Settings
from igneous.graph import identity
from igneous.graph.visibility import not_owner, not_service, visible


def _node(s, type_, key, props=None):
    return s.execute(text("INSERT INTO nodes (type, key, name, props) VALUES (:t, :k, :k, CAST(:p AS jsonb)) "
                          "RETURNING id"), {"t": type_, "k": key, "p": json.dumps(props or {})}).scalar_one()


def test_identity_finds_the_owner_heads_and_cluster_members(db):
    assert identity.self_id(db) is None
    me = _node(db, "Person", "me", {"self": True})
    head, alias, alone = _node(db, "Person", "sam@a.test"), _node(db, "Person", "sam@b.test"), _node(db, "Person", "x")
    db.execute(text("INSERT INTO canonical VALUES (:h, :h), (:a, :h)"), {"h": head, "a": alias})
    assert identity.self_id(db) == me
    assert identity.head(db, alias) == head and identity.head(db, head) == head and identity.head(db, alone) == alone
    assert identity.members(db, alias) == identity.members(db, head) == sorted([head, alias])
    assert identity.members(db, alone) == [alone]


def test_visibility_predicates(db):
    shown = _node(db, "Thread", "shown", {"hidden": False})
    _node(db, "Thread", "hidden", {"hidden": True})
    plain = _node(db, "Person", "plain")
    _node(db, "Person", "bot", {"kind": "service"})
    _node(db, "Person", "me", {"self": True})
    rows = db.execute(text(f"SELECT id FROM nodes n WHERE {visible('n')} AND {not_service('n')} AND {not_owner('n')}"
                           " ORDER BY id")).scalars().all()
    assert rows == [shown, plain]


def test_every_stored_source_belongs_to_a_sources_page_entry():
    values = [s.value for s in source_catalog.CATALOG]
    assert len(values) == len(set(values))
    assert {s.connector for s in source_catalog.CATALOG} <= {s["id"] for s in sources.SOURCES}
    assert all(s.kind in source_catalog.KINDS for s in source_catalog.CATALOG)
    assert set(source_catalog.LOCAL_TEXT) == {"fs", "notes"}
    # Browsing and places are life threads' evidence, never content an agent reads by default; cards are neither.
    assert "takeout_chrome" in source_catalog.LIFE and "takeout_chrome" not in source_catalog.CONTENT
    assert not {"gcontacts", "maccontacts", "takeout_maps"} & (set(source_catalog.LIFE) | set(source_catalog.CONTENT))


def test_opting_in_takeout_covers_every_takeout_product():
    assert llm.allowed_sources("takeout") == frozenset(source_catalog.of_connector("takeout"))
    assert len(source_catalog.of_connector("takeout")) == 6


def test_a_stored_model_result_outlives_the_callers_rollback(committed_db):
    from igneous.db import SessionLocal

    with SessionLocal() as caller:
        caller.execute(text("INSERT INTO nodes (type, key, name) VALUES ('Topic', 't', 't')"))
        llm.cache_store("test:outlives", {"answer": 1})
        caller.rollback()
    assert llm.cache_lookup("test:outlives") == {"answer": 1}
    assert committed_db.execute(text("SELECT count(*) FROM nodes")).scalar() == 0


def test_the_first_stored_result_stays_unless_replaced(db):
    assert llm.cache_lookup("test:key") is None
    llm.cache_store("test:key", {"v": 1})
    llm.cache_store("test:key", {"v": 2})
    assert llm.cache_lookup("test:key") == {"v": 1}
    llm.cache_store("test:key", {"v": 3}, replace=True)
    assert llm.cache_lookup("test:key") == {"v": 3}


def test_settings_resolve_against_the_api_folder_wherever_the_process_starts():
    assert Settings.model_config["env_file"] == API_DIR / ".env"
    assert Settings(_env_file=None, data_dir="../data").data_dir == str((API_DIR / "../data").resolve())
    assert Settings(_env_file=None, data_dir="/srv/igneous-data").data_dir == "/srv/igneous-data"
