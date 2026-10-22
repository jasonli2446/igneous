"""Introduction derivation on synthetic rows, in a throwaway database (never the real ones)."""
import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from igneous.config import settings
from igneous.graph.history import EXTRACTOR, derive_introductions

T0 = datetime(2024, 1, 1)


@pytest.fixture
def s():
    admin_url = make_url(settings.database_url).set(database="postgres")
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    name = f"igneous_test_{uuid.uuid4().hex[:8]}"
    try:
        with admin.connect() as c:
            c.execute(text(f'CREATE DATABASE "{name}"'))
    except Exception as e:
        pytest.skip(f"no scratch database available: {type(e).__name__}")
    engine = create_engine(admin_url.set(database=name))
    try:
        with engine.begin() as c:
            c.execute(text("CREATE TABLE nodes (id bigint PRIMARY KEY, props jsonb NOT NULL DEFAULT '{}')"))
            c.execute(text("CREATE TABLE canonical (node_id bigint PRIMARY KEY, canonical_id bigint NOT NULL)"))
            c.execute(text("""
                CREATE TABLE edges (id bigserial PRIMARY KEY, src bigint, dst bigint, type text, props jsonb,
                    valid_from timestamp, valid_to timestamp, extractor text, extractor_version text,
                    confidence real, source_item_id bigint, UNIQUE (src, dst, type, extractor))"""))
        with Session(engine) as session:
            yield session
    finally:
        engine.dispose()
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def _people(s, *ids, services=()):
    for i in ids:
        s.execute(text("INSERT INTO nodes (id, props) VALUES (:i, CAST(:p AS jsonb))"),
                  {"i": i, "p": '{"kind": "service"}' if i in services else "{}"})


_msg = iter(range(1000, 10**6))


def _send(s, day, sender, *recipients):
    m, at = next(_msg), T0 + timedelta(days=day)
    s.execute(text("INSERT INTO edges (src, dst, type, valid_from, source_item_id) VALUES (:p, :m, 'SENT', :at, :m)"),
              {"p": sender, "m": m, "at": at})
    for r in recipients:
        s.execute(text("INSERT INTO edges (src, dst, type, valid_from, source_item_id) "
                       "VALUES (:m, :p, 'RECEIVED', :at, :m)"), {"p": r, "m": m, "at": at})


def _introduced(s):
    return [tuple(r) for r in s.execute(text(
        "SELECT src, dst, (props->>'introducer')::int FROM edges WHERE type = 'INTRODUCED' "
        "AND extractor = :ex ORDER BY src, dst"), {"ex": EXTRACTOR}).all()]


def test_group_chat_makes_no_introductions(s):
    _people(s, *range(1, 11))
    _send(s, 0, 1, *range(2, 11))
    _send(s, 5, 2, 3)
    assert derive_introductions(s) == 0


def test_three_person_intro_then_one_to_one_makes_exactly_one(s):
    _people(s, 1, 2, 3)
    _send(s, 0, 1, 2, 3)
    _send(s, 10, 2, 3)
    _send(s, 11, 3, 2)
    assert derive_introductions(s) == 1
    assert _introduced(s) == [(2, 3, 1)]
    assert derive_introductions(s) == 1          # idempotent rebuild
    assert len(_introduced(s)) == 1


def test_later_contact_only_with_introducer_present_makes_none(s):
    _people(s, 1, 2, 3)
    _send(s, 0, 1, 2, 3)
    _send(s, 10, 2, 3, 1)
    _send(s, 12, 3, 1, 2)
    assert derive_introductions(s) == 0


def test_prior_contact_late_contact_and_services_are_rejected(s):
    _people(s, 1, 2, 3, 4, 5, services=(5,))
    _send(s, -3, 2, 3)                           # 2 and 3 already knew each other
    _send(s, 0, 1, 2, 3)
    _send(s, 10, 2, 3)
    _send(s, 0, 1, 4, 5)                         # 5 is a service
    _send(s, 10, 4, 5)
    _send(s, 0, 4, 2, 1)                         # 1 and 2 later talk only outside the window
    _send(s, 400, 2, 1)
    assert derive_introductions(s) == 0


def test_canonical_identities_merge_and_earliest_message_wins(s):
    _people(s, 1, 2, 3, 30)
    s.execute(text("INSERT INTO canonical VALUES (30, 3)"))
    _send(s, 0, 1, 2, 30)                        # 30 is a second address of 3
    _send(s, 4, 1, 2, 3)
    _send(s, 10, 3, 2)
    assert derive_introductions(s) == 1
    assert _introduced(s) == [(2, 3, 1)]
