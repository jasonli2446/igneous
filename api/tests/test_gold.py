"""The eval's tooling: gold keys resolve on any build, an abstention never counts as a gold citation, the
committed answers reproduce the published numbers, ER pairs are judged by the resolver's real decision, and a gold
thread with only short terms matches nothing."""
import csv
import json
from pathlib import Path

import pytest
from sqlalchemy import text

EVAL = Path(__file__).resolve().parents[1] / "eval"


@pytest.fixture
def tools(monkeypatch):
    monkeypatch.syspath_prepend(str(EVAL))
    import er_labels
    import gold
    import run_eval
    import threads_eval
    return type("Tools", (), {"gold": gold, "run_eval": run_eval, "er_labels": er_labels, "threads": threads_eval})


def _item(s, external_id, fingerprint) -> int:
    return s.execute(text("""
        INSERT INTO source_items (source, external_id, content_hash, raw_meta)
        VALUES ('email', :e, :e, CAST(:m AS jsonb)) RETURNING id"""),
        {"e": external_id, "m": json.dumps({"fingerprint": fingerprint})}).scalar_one()


def _key(external_id, fingerprint):
    return {"source": "email", "external_id": external_id, "fingerprint": fingerprint}


def test_keys_resolve_to_every_copy_of_a_message_in_this_database(db, tools):
    inbox, filed, other = _item(db, "lay-k/inbox/1.", "fp-a"), _item(db, "lay-k/all_documents/5.", "fp-a"), \
        _item(db, "allen-p/inbox/2.", "fp-b")
    found = tools.gold.resolve(db, [_key("lay-k/inbox/1.", "fp-a"), _key("skilling-j/inbox/9.", "fp-b"),
                                    _key("arnold-j/inbox/3.", "fp-gone")])
    assert found == [{inbox, filed}, {other}, set()]
    assert tools.gold.keys_of(db, [filed]) == {filed: _key("lay-k/all_documents/5.", "fp-a")}


def test_an_abstention_never_counts_as_a_gold_citation(tools):
    questions = [
        {"id": "q1", "kind": "lookup", "answerable": True, "gold": [_key("lay-k/inbox/1.", "fp-a")]},
        {"id": "q2", "kind": "multihop", "answerable": True, "gold": [_key("lay-k/inbox/2.", "fp-b")]},
        {"id": "q3", "kind": "unanswerable", "answerable": False, "gold": []},
    ]
    rows = [  # q1 cites another copy of its gold; q2 lists its gold but abstains; q3 abstains
        {"config": "full", "id": "q1", "answerable": True, "cited": [_key("lay-k/all_documents/5.", "fp-a")], "seconds": 1.0},
        {"config": "full", "id": "q2", "answerable": False, "cited": [_key("lay-k/inbox/2.", "fp-b")], "seconds": 3.0},
        {"config": "full", "id": "q3", "answerable": False, "cited": [], "seconds": 2.0},
    ]
    full = tools.run_eval.qa_metrics(questions, rows)["full"]
    assert (full["cited_gold"], full["cited_gold_single"], full["cited_gold_multihop"]) == (0.5, 1.0, 0.0)
    assert full["abstention"] == 1.0 and full["errors"] == 0 and full["p50_s"] == 2.0


def test_the_committed_answers_reproduce_the_published_numbers(tools):
    rows = [json.loads(line) for line in (EVAL / "results" / "2026-10-18-answers.jsonl").read_text().splitlines()]
    report = tools.run_eval.qa_metrics(tools.gold.load(), rows)
    published = json.loads((EVAL / "results" / "2026-10-18.json").read_text())
    for config in ("full", "baseline"):
        for metric, value in report[config].items():
            assert published[config][metric] == value, (config, metric)
    assert (report["full"]["cited_gold"], report["baseline"]["cited_gold"]) == (0.871, 0.743)


def _person(s, key, name) -> int:
    return s.execute(text("INSERT INTO nodes (type, key, name) VALUES ('Person', :k, :n) RETURNING id"),
                     {"k": key, "n": name}).scalar_one()


def test_er_pairs_are_judged_by_key_through_the_resolvers_decisions(db, tools, tmp_path):
    # One name on two addresses with nothing else in common: score() alone says different (0.4), but a name only
    # two identities carry is merged on the name (candidates() gives it 0.6).
    a = _person(db, "ann.lee@alpha.test", "Ann Lee")
    b = _person(db, "ann.lee@beta.test", "Ann Lee")
    _person(db, "bob.ray@alpha.test", "Bob Ray")
    _person(db, "bill.ray@alpha.test", "Bill Ray")
    rows = [
        {"a_key": "ann.lee@alpha.test", "b_key": "ann.lee@beta.test", "label": "1", "reviewed": "0", "reviewer": "m"},
        {"a_key": "bob.ray@alpha.test", "b_key": "bill.ray@alpha.test", "label": "0", "reviewed": "0", "reviewer": "m"},
        {"a_key": "name:ken-lay", "b_key": "ken.lay@enron.com", "label": "1", "reviewed": "0", "reviewer": "m"},
    ]
    report = tools.er_labels.score((0.5,), rows)
    assert report["pairs_labeled"] == 3 and report["pairs_not_in_database"] == 1
    (pairs,) = [r for r in report["results"] if r["subset"] == "all"]  # no clusters before resolution runs
    assert pairs["decision"] == "pairs >= 0.5" and (pairs["tp"], pairs["fp"], pairs["fn"]) == (1, 0, 0)
    db.execute(text("INSERT INTO canonical (node_id, canonical_id) VALUES (:a, :a), (:b, :a)"), {"a": a, "b": b})
    clusters = next(r for r in tools.er_labels.score((0.5,), rows)["results"] if r["decision"] == "clusters")
    assert (clusters["tp"], clusters["fp"], clusters["fn"], clusters["pairs"]) == (1, 0, 0, 2)


def test_the_committed_er_pairs_carry_keys_not_row_ids(tools):
    with (EVAL / "er_pairs.csv").open(newline="") as fh:
        reader = csv.DictReader(fh)
        assert reader.fieldnames == tools.er_labels.FIELDS
        assert all(r["a_key"] and r["b_key"] for r in reader)


def test_a_gold_thread_with_only_short_terms_matches_no_project(db, tools):
    db.execute(text("INSERT INTO nodes (type, key, name) VALUES ('Project', 'thread:robotics', 'Robotics Lab')"))
    found = tools.threads.score({"threads": [{"name": "AI club", "terms": ["ai", "ml"]},
                                             {"name": "Robots", "terms": ["robotics"]}]})
    assert [t["found"] for t in found["threads"]] == [None, "Robotics Lab"]
