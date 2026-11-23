"""The migration chain, and the schema check every entry point runs before touching data."""
import click
import pytest
from conftest import API, TEST_DB
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from igneous import db as dbm
from igneous.config import settings


def _schema(url: str) -> dict:
    """Every column, index and constraint in the public schema, comparable across databases."""
    engine = create_engine(url)
    try:
        with engine.connect() as c:
            return {
                "columns": c.execute(text("""
                    SELECT table_name, column_name, data_type, column_default, is_nullable
                    FROM information_schema.columns WHERE table_schema = 'public' ORDER BY 1, 2""")).all(),
                "indexes": c.execute(text("""
                    SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'public' ORDER BY 1""")).all(),
                "constraints": c.execute(text("""
                    SELECT conrelid::regclass::text, conname, pg_get_constraintdef(oid) FROM pg_constraint
                    WHERE connamespace = 'public'::regnamespace ORDER BY 1, 2""")).all(),
            }
    finally:
        engine.dispose()


def test_the_chain_has_one_head():
    assert len(dbm._script().get_heads()) == 1


def test_downgrading_to_base_and_upgrading_again_gives_the_fresh_schema(test_db, monkeypatch):
    from alembic import command
    from alembic.config import Config

    name = f"{TEST_DB}_roundtrip"
    url = make_url(settings.database_url).set(database=name).render_as_string(hide_password=False)
    admin = create_engine(make_url(settings.database_url).set(database="postgres"), isolation_level="AUTOCOMMIT")
    config = Config(str(API / "alembic.ini"))
    config.set_main_option("script_location", str(API / "migrations"))
    monkeypatch.setattr(settings, "database_url", url)  # migrations/env.py connects to settings.database_url
    try:
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
            c.execute(text(f'CREATE DATABASE "{name}"'))
        command.upgrade(config, "head")
        fresh = _schema(url)
        command.downgrade(config, "base")
        command.upgrade(config, "head")
        assert _schema(url) == fresh
    finally:
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def test_organizations_keyed_by_company_name_converge_on_one_node_each(test_db, monkeypatch):
    """Migration 0009: company:<slug> (contact cards) and name:<slug> (model mentions) become org:<slug>, merging into
    an org: node that already exists; changes pending in the old shared flag carry over to entity resolution."""
    from alembic import command
    from alembic.config import Config

    name = f"{TEST_DB}_orgkeys"
    url = make_url(settings.database_url).set(database=name).render_as_string(hide_password=False)
    admin = create_engine(make_url(settings.database_url).set(database="postgres"), isolation_level="AUTOCOMMIT")
    config = Config(str(API / "alembic.ini"))
    config.set_main_option("script_location", str(API / "migrations"))
    monkeypatch.setattr(settings, "database_url", url)  # migrations/env.py connects to settings.database_url
    try:
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
            c.execute(text(f'CREATE DATABASE "{name}"'))
        command.upgrade(config, "0008")
        engine = create_engine(url)
        with engine.begin() as c:
            ids = dict(c.execute(text("""
                INSERT INTO nodes (type, key, name) VALUES ('Person', 'sam@x.test', 'Sam'), ('Message', 'm1', 'Hi'),
                    ('Organization', 'company:acme-inc', 'Acme Inc'), ('Organization', 'name:acme-inc', 'Acme, Inc.'),
                    ('Organization', 'org:globex', 'Globex'), ('Organization', 'company:globex', 'Globex')
                RETURNING key, id""")).all())
            c.execute(text("""
                INSERT INTO edges (src, dst, type, extractor) VALUES
                    (:sam, :acme_card, 'WORKED_AT', 'organizations'), (:m1, :acme_mention, 'MENTIONS', 'llm-extract'),
                    (:sam, :globex_card, 'WORKED_AT', 'organizations')"""),
                      {"sam": ids["sam@x.test"], "m1": ids["m1"], "acme_card": ids["company:acme-inc"],
                       "acme_mention": ids["name:acme-inc"], "globex_card": ids["company:globex"]})
            c.execute(text("INSERT INTO dirty_rebuild VALUES (true)"))
        command.upgrade(config, "head")
        with engine.connect() as c:
            orgs = c.execute(text("SELECT key FROM nodes WHERE type = 'Organization' ORDER BY key")).scalars().all()
            links = c.execute(text("""SELECT a.key, e.type, b.key FROM edges e JOIN nodes a ON a.id = e.src
                                      JOIN nodes b ON b.id = e.dst ORDER BY 1, 2, 3""")).all()
            pending = c.execute(text("SELECT stage FROM pipeline_changes")).scalars().all()
        engine.dispose()
        assert orgs == ["org:acme-inc", "org:globex"]
        assert [tuple(r) for r in links] == [("m1", "MENTIONS", "org:acme-inc"), ("sam@x.test", "WORKED_AT", "org:acme-inc"),
                                             ("sam@x.test", "WORKED_AT", "org:globex")]
        assert pending == ["resolve"]
    finally:
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def test_corrections_become_intents_and_dropped_links_one_row_each(test_db, monkeypatch):
    """The corrections migration on a database written by the code before it: what the verifier dropped is one row
    per natural key (stale and orphaned rows go), an unlink in the older format is your rejection of the pair, the
    links you added are intents, a thread named without the model is unjudged, and a candidate a failed model call
    rejected is judged again. Downgrading and upgrading again keeps it all."""
    from alembic import command
    from alembic.config import Config

    revision = next(r for r in dbm._script().walk_revisions() if (r.doc or "").startswith("Corrections as intents"))
    name = f"{TEST_DB}_intents"
    url = make_url(settings.database_url).set(database=name).render_as_string(hide_password=False)
    admin = create_engine(make_url(settings.database_url).set(database="postgres"), isolation_level="AUTOCOMMIT")
    config = Config(str(API / "alembic.ini"))
    config.set_main_option("script_location", str(API / "migrations"))
    monkeypatch.setattr(settings, "database_url", url)  # migrations/env.py connects to settings.database_url
    try:
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
            c.execute(text(f'CREATE DATABASE "{name}"'))
        command.upgrade(config, revision.down_revision)
        engine = create_engine(url)
        with engine.begin() as c:
            ids = dict(c.execute(text("""
                INSERT INTO nodes (type, key, name) VALUES ('Person', 'pat@x.test', 'Pat'),
                    ('Project', 'thread:a', 'Atlas'), ('Project', 'thread:b', 'Kiosk'), ('Project', 'project:c', 'Cy')
                RETURNING key, id""")).all())
            pat, a, b = (ids[k] for k in ("pat@x.test", "thread:a", "thread:b"))
            c.execute(text("""
                INSERT INTO edges_dropped (id, src, dst, type, extractor) VALUES
                    (90, :pat, :a, 'INVOLVED_IN', 'threads'), (91, :pat, :a, 'INVOLVED_IN', 'threads'),
                    (92, :pat, :a, 'INVOLVED_IN', 'threads'), (93, :pat, :b, 'RELATED_TO', 'threads'),
                    (94, :pat, 999999, 'INVOLVED_IN', 'threads')"""), {"pat": pat, "a": a, "b": b})
            c.execute(text("""
                INSERT INTO edge_verdicts (src_key, dst_key, type, extractor, evidence, verdict, confidence, source)
                VALUES ('Person:pat@x.test', 'Project:thread:a', 'INVOLVED_IN', 'threads', 'd', 'drop', 0.9, 'model')"""))
            c.execute(text("""INSERT INTO overrides (op, node_keys) VALUES
                              ('unlink', ARRAY['Project:project:c', 'Person:pat@x.test'])"""))
            c.execute(text("""
                INSERT INTO edges (src, dst, type, extractor, props) VALUES
                    (:a, :b, 'PART_OF', 'user', '{}'), (:pat, :b, 'RELATED_TO', 'user', '{"why": "you added this"}')
            """), {"a": a, "b": b, "pat": pat})
            c.execute(text("""
                INSERT INTO thread_registry (key, name, kind, terms, status, origin) VALUES
                    ('thread:p', 'Portland · Aug 2025', 'thread', '{Portland}', 'active', 'burst'),
                    ('thread:q', 'Quiz · May 2025', 'thread', '{Quiz}', 'rejected', 'burst'),
                    ('thread:r', 'Rent', 'chore', '{Rent}', 'rejected', 'burst')"""))
        for _ in range(2):  # the upgrade, then a downgrade and the upgrade again
            command.upgrade(config, "head")
            with engine.connect() as c:
                dropped = c.execute(text("SELECT id, src_key, dst_key FROM edges_dropped")).all()
                rejection = c.execute(text("""SELECT src_key, dst_key, type, extractor, verdict, source
                                              FROM edge_verdicts WHERE source = 'user'""")).all()
                unlink = c.execute(text("SELECT data FROM overrides WHERE op = 'unlink'")).scalar_one()
                links = c.execute(text("SELECT node_keys, value FROM overrides WHERE op = 'link'")).all()
                parents = c.execute(text("SELECT child_key, parent_key, origin FROM thread_parents")).all()
                registry = c.execute(text("SELECT key, status, judged FROM thread_registry ORDER BY key")).all()
            assert [tuple(r) for r in dropped] == [(92, "Person:pat@x.test", "Project:thread:a")]
            assert [tuple(r) for r in rejection] == [
                ("Person:pat@x.test", "Project:project:c", "INVOLVED_IN", "entities", "drop", "user")]
            assert unlink["pair_correction"] and unlink["verdicts"][0]["previous"] is None
            assert [tuple(r) for r in links] == [(["Person:pat@x.test", "Project:thread:b"], "RELATED_TO")]
            assert [tuple(r) for r in parents] == [("thread:a", "thread:b", "user")]
            assert [tuple(r) for r in registry] == [("thread:p", "active", False), ("thread:r", "rejected", True)]
            command.downgrade(config, revision.down_revision)
        engine.dispose()
    finally:
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def test_a_domain_organization_its_company_holds_takes_the_company_key_and_corrections_follow(test_db, monkeypatch):
    """Migration 0015 on a database the code before it built: acme.test, named by its people's own words, gives its
    links to the LinkedIn company Acme Robotics and goes; state.edu, which your resume named, takes the key of the
    company its people name; a domain only the model named, for a company nothing else names, stays. Its backbone
    thread follows, and so do your rejection, your rename, the verifier's verdict and the link it dropped."""
    from alembic import command
    from alembic.config import Config

    revision = next(r for r in dbm._script().walk_revisions() if (r.doc or "").startswith("One organization, one key"))
    name = f"{TEST_DB}_companies"
    url = make_url(settings.database_url).set(database=name).render_as_string(hide_password=False)
    admin = create_engine(make_url(settings.database_url).set(database="postgres"), isolation_level="AUTOCOMMIT")
    config = Config(str(API / "alembic.ini"))
    config.set_main_option("script_location", str(API / "migrations"))
    monkeypatch.setattr(settings, "database_url", url)  # migrations/env.py connects to settings.database_url
    try:
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
            c.execute(text(f'CREATE DATABASE "{name}"'))
        command.upgrade(config, revision.down_revision)
        engine = create_engine(url)
        with engine.begin() as c:
            c.execute(text("""
                INSERT INTO source_items (source, external_id, title, raw_meta, content_hash) VALUES
                    ('gcontacts', 'acct/people/1', 'Cy', '{"emails": ["cy@state.edu"], "orgs": ["State University"]}',
                     'h1')
            """))
            ids = dict(c.execute(text("""
                INSERT INTO nodes (type, key, name, props) VALUES
                    ('Person', 'ana@acme.test', 'Ana', '{}'), ('Person', 'bo@acme.test', 'Bo', '{}'),
                    ('Organization', 'acme.test', 'Acme Robotics',
                     '{"inferred_by": "organizations", "named_from": "their own words"}'),
                    ('Organization', 'org:acme-robotics', 'Acme Robotics', '{"kind": "employer", "source": "linkedin"}'),
                    ('Organization', 'state.edu', 'State U', '{"named_by": "profile"}'),
                    ('Organization', 'initech.test', 'Initech',
                     '{"inferred_by": "organizations", "named_from": "how its people sign"}'),
                    ('Project', 'backbone:acme.test', 'Acme Robotics', '{"derived": "backbone"}'),
                    ('Project', 'thread:x', 'Heron', '{"derived": "threads"}')
                RETURNING key, id""")).all())
            c.execute(text("UPDATE nodes SET props = props || jsonb_build_object('thread', :t) WHERE id = :o"),
                      {"t": ids["backbone:acme.test"], "o": ids["acme.test"]})
            c.execute(text("""
                INSERT INTO edges (src, dst, type, extractor) VALUES
                    (:ana, :acme, 'WORKED_AT', 'organizations'), (:ana, :company, 'MEMBER_OF', 'linkedin'),
                    (:x, :thread, 'PART_OF', 'backbone')"""),
                      {"ana": ids["ana@acme.test"], "acme": ids["acme.test"], "company": ids["org:acme-robotics"],
                       "x": ids["thread:x"], "thread": ids["backbone:acme.test"]})
            c.execute(text("""
                INSERT INTO edge_verdicts (src_key, dst_key, type, extractor, evidence, verdict, confidence, source)
                VALUES ('Person:ana@acme.test', 'Organization:acme.test', 'WORKED_AT', 'organizations', '', 'drop', 1,
                        'user'),
                       ('Person:bo@acme.test', 'Organization:acme.test', 'WORKED_AT', 'organizations', 'd', 'drop', 0.9,
                        'model'),
                       ('Project:thread:x', 'Project:backbone:acme.test', 'PART_OF', 'backbone', 'd', 'keep', 0.9,
                        'model')"""))
            c.execute(text("""
                INSERT INTO edges_dropped (src, dst, type, extractor, src_key, dst_key)
                VALUES (:bo, :acme, 'WORKED_AT', 'organizations', 'Person:bo@acme.test', 'Organization:acme.test')"""),
                      {"bo": ids["bo@acme.test"], "acme": ids["acme.test"]})
            c.execute(text("""INSERT INTO overrides (op, node_keys, value) VALUES
                              ('rename', ARRAY['Project:backbone:acme.test'], 'Acme (mine)')"""))
        command.upgrade(config, revision.revision)
        with engine.connect() as c:
            orgs = c.execute(text("""SELECT key, name, props->'domains', (props->>'thread')::bigint FROM nodes
                                     WHERE type = 'Organization' ORDER BY key""")).all()
            links = c.execute(text("""SELECT a.key, e.type, b.key FROM edges e JOIN nodes a ON a.id = e.src
                                      JOIN nodes b ON b.id = e.dst ORDER BY 1, 2, 3""")).all()
            thread = c.execute(text("""SELECT id, (props->>'org')::bigint FROM nodes
                                       WHERE type = 'Project' AND key = 'backbone:org:acme-robotics'""")).one()
            verdicts = c.execute(text("""SELECT src_key, dst_key, verdict, source FROM edge_verdicts
                                         ORDER BY 1, 2""")).all()
            dropped = c.execute(text("SELECT src_key, dst_key FROM edges_dropped")).all()
            renamed = c.execute(text("SELECT node_keys FROM overrides WHERE op = 'rename'")).scalar_one()
        engine.dispose()
        assert [tuple(r) for r in orgs] == [
            ("initech.test", "Initech", None, None),
            ("org:acme-robotics", "Acme Robotics", ["acme.test"], thread[0]),
            ("org:state-university", "State U", ["state.edu"], None)]
        assert [tuple(r) for r in links] == [("ana@acme.test", "MEMBER_OF", "org:acme-robotics"),
                                             ("ana@acme.test", "WORKED_AT", "org:acme-robotics"),
                                             ("thread:x", "PART_OF", "backbone:org:acme-robotics")]
        assert thread[1] == ids["org:acme-robotics"]
        assert [tuple(r) for r in verdicts] == [
            ("Person:ana@acme.test", "Organization:org:acme-robotics", "drop", "user"),
            ("Person:bo@acme.test", "Organization:org:acme-robotics", "drop", "model"),
            ("Project:thread:x", "Project:backbone:org:acme-robotics", "keep", "model")]
        assert [tuple(r) for r in dropped] == [("Person:bo@acme.test", "Organization:org:acme-robotics")]
        assert renamed == ["Project:backbone:org:acme-robotics"]
    finally:
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def test_schema_status_is_none_at_head(test_db):
    assert dbm.schema_status() is None


def test_schema_status_names_a_database_behind_or_ahead_of_the_code(db):
    script = dbm._script()
    head = script.get_heads()[0]
    previous = script.get_revision(head).down_revision
    connection = db.connection()
    db.execute(text("UPDATE alembic_version SET version_num = :v"), {"v": previous})
    behind = dbm.schema_status(bind=connection)
    assert f"is at migration {previous}, the code needs {head}" in behind and "alembic upgrade head" in behind
    db.execute(text("UPDATE alembic_version SET version_num = 'f00d'"))
    assert "is at migration f00d, which this code doesn't have" in dbm.schema_status(bind=connection)


def test_schema_status_names_a_database_without_a_schema(db):
    connection = db.connection()
    db.execute(text("ALTER TABLE alembic_version RENAME TO alembic_version_elsewhere"))
    assert "has no schema yet" in dbm.schema_status(bind=connection)


def test_schema_status_names_an_unreachable_server(monkeypatch):
    monkeypatch.setattr(dbm, "engine", create_engine("postgresql+psycopg://igneous:igneous@127.0.0.1:1/nowhere"))
    problem = dbm.schema_status()
    assert problem.startswith("cannot reach database nowhere at 127.0.0.1:1") and "IGNEOUS_DATABASE_URL" in problem


def test_schema_status_names_two_migration_heads(monkeypatch):
    class TwoHeads:
        def get_heads(self):
            return ["0009b", "0009a"]

    monkeypatch.setattr(dbm, "_script", TwoHeads)
    assert dbm.schema_status() == ("the code has 2 migration heads (0009a, 0009b): merge them with "
                                   "`cd api && uv run alembic merge heads`")


PROBLEM = "database igneous is at migration 0001, the code needs 0002: run `cd api && uv run alembic upgrade head`"


def test_the_api_refuses_to_start_on_a_wrong_schema(monkeypatch):
    from fastapi.testclient import TestClient

    from igneous import main

    monkeypatch.setattr(main, "schema_status", lambda: PROBLEM)
    with pytest.raises(RuntimeError, match="the code needs 0002"), TestClient(main.app):
        pass


def test_the_cli_refuses_database_commands_on_a_wrong_schema(monkeypatch, tmp_path):
    from typer.testing import CliRunner

    from igneous import cli

    monkeypatch.setattr(dbm, "schema_status", lambda: PROBLEM)
    refused = CliRunner().invoke(cli.app, ["pipeline", "--stage", "spine"])
    assert refused.exit_code == 2 and "the code needs 0002" in refused.output
    # Commands that never open the database still run, and every command's help reads without one.
    assert CliRunner().invoke(cli.app, ["synthetic-corpus", str(tmp_path / "corpus")]).exit_code == 0
    for command in (["sync", "--help"], ["pipeline", "--help"], ["import", "takeout", "--help"], ["--help"]):
        shown = CliRunner().invoke(cli.app, command)
        assert shown.exit_code == 0 and "Usage" in click.unstyle(shown.output), command


def test_the_mcp_server_refuses_to_start_on_a_wrong_schema(monkeypatch, capsys):
    from igneous import mcp_server

    monkeypatch.setattr(dbm, "schema_status", lambda: PROBLEM)
    with pytest.raises(SystemExit) as stopped:
        mcp_server.main()
    assert stopped.value.code == 2 and "the code needs 0002" in capsys.readouterr().err
