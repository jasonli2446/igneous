"""Introduction derivation on synthetic messages in the migrated schema."""
import json
from datetime import UTC, datetime, timedelta
from itertools import count

import pytest
from sqlalchemy import text

from igneous.graph.history import EXTRACTOR, derive_introductions

T0 = datetime(2024, 1, 1, tzinfo=UTC)
_message_ids = count(1000)  # people use small explicit ids; messages never collide with them


@pytest.fixture
def s(db):
    return db


def _people(s, *ids, services=()):
    for i in ids:
        s.execute(text("""INSERT INTO nodes (id, type, key, name, props)
                          VALUES (:i, 'Person', :k, :k, CAST(:p AS jsonb))"""),
                  {"i": i, "k": f"p{i}@example.test", "p": json.dumps({"kind": "service"} if i in services else {})})


def _send(s, day, sender, *recipients):
    """One email from `sender` to `recipients`, `day` days after T0."""
    m, at = next(_message_ids), T0 + timedelta(days=day)
    sid = s.execute(text("""INSERT INTO source_items (source, external_id, content_hash, occurred_at)
                            VALUES ('email', :x, :x, :at) RETURNING id"""), {"x": f"m{m}", "at": at}).scalar_one()
    s.execute(text("INSERT INTO nodes (id, type, key, name, source_item_id) VALUES (:m, 'Message', :k, :k, :sid)"),
              {"m": m, "k": f"m{m}", "sid": sid})
    s.execute(text("""INSERT INTO edges (src, dst, type, valid_from, valid_to, source_item_id)
                      VALUES (:p, :m, 'SENT', :at, :at, :sid)"""), {"p": sender, "m": m, "at": at, "sid": sid})
    for r in recipients:
        s.execute(text("""INSERT INTO edges (src, dst, type, valid_from, valid_to, source_item_id)
                          VALUES (:m, :p, 'RECEIVED', :at, :at, :sid)"""), {"p": r, "m": m, "at": at, "sid": sid})


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
