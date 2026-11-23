"""The migration chain, and the schema check every entry point runs before touching data."""
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
    # Commands that never open the database still run.
    assert CliRunner().invoke(cli.app, ["synthetic-corpus", str(tmp_path / "corpus")]).exit_code == 0


def test_the_mcp_server_refuses_to_start_on_a_wrong_schema(monkeypatch, capsys):
    from igneous import mcp_server

    monkeypatch.setattr(dbm, "schema_status", lambda: PROBLEM)
    with pytest.raises(SystemExit) as stopped:
        mcp_server.main()
    assert stopped.value.code == 2 and "the code needs 0002" in capsys.readouterr().err
