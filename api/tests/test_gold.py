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


def test_er_pairs_are_judged_by_key_through_the_resolvers_decisions(db, tools):
    from igneous.graph.resolve import load
    from igneous.graph.resolve import score as pair_score

    # A phone named like two different people with addresses: score() alone joins it to one of them on the exact
    # name, but the resolver's decision (candidates()) doesn't, because two people carry that name.
    _person(db, "ann.lee@alpha.test", "Ann Lee")
    _person(db, "ann.lee@beta.test", "Ann Lee")
    _person(db, "+15550001111", "Ann Lee")
    a, b = _person(db, "ken.lay@enron.com", "Ken Lay"), _person(db, "ken_lay@enron.net", "Ken Lay")  # one local part
    people = {p.key: p for p in load(db)}
    assert pair_score(people["+15550001111"], people["ann.lee@alpha.test"])[0] >= 0.5
    rows = [
        {"a_key": "+15550001111", "b_key": "ann.lee@alpha.test", "label": "0", "reviewed": "0", "reviewer": "m"},
        {"a_key": "ken.lay@enron.com", "b_key": "ken_lay@enron.net", "label": "1", "reviewed": "0", "reviewer": "m"},
        {"a_key": "name:ken-lay", "b_key": "ken.lay@enron.com", "label": "1", "reviewed": "0", "reviewer": "m"},
    ]
    report = tools.er_labels.score((0.5,), rows)
    assert report["pairs_labeled"] == 3 and report["pairs_not_in_database"] == 1
    results = {(r["decision"], r["subset"]): r for r in report["results"]}
    assert not any(decision == "clusters" for decision, _ in results)  # none before resolution runs
    present, every = results[("pairs >= 0.5", "present")], results[("pairs >= 0.5", "every labeled")]
    assert (present["tp"], present["fp"], present["fn"], present["pairs"]) == (1, 0, 0, 2)
    assert (every["tp"], every["fp"], every["fn"], every["pairs"]) == (1, 0, 1, 3)  # the absent pair isn't merged
    db.execute(text("INSERT INTO canonical (node_id, canonical_id) VALUES (:a, :a), (:b, :a)"), {"a": a, "b": b})
    results = {(r["decision"], r["subset"]): r for r in tools.er_labels.score((0.5,), rows)["results"]}
    clusters = results[("clusters", "present")]
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


def test_a_local_file_is_keyed_by_its_path_in_whatever_folder_was_read(db, tools):
    """A documents folder's ids start with a hash of where it sits on disk, so gold names the file by its path."""
    ids = [db.execute(text("INSERT INTO source_items (source, external_id, content_hash) VALUES ('fs', :e, :e) "
                           "RETURNING id"), {"e": e}).scalar_one()
           for e in ("a1b2c3/Travel/plan.md", "d4e5f6/Mira/Travel/plan.md", "a1b2c3/Travel/other-plan.md")]
    key = {"source": "fs", "path": "Travel/plan.md"}
    assert tools.gold.resolve(db, [key]) == [{ids[0], ids[1]}]
    cited = tools.gold.keys_of(db, [ids[1]])[ids[1]]
    assert cited["path"] == "Mira/Travel/plan.md" and tools.gold.same(cited, key)
    assert not tools.gold.same(tools.gold.keys_of(db, [ids[2]])[ids[2]], key)


def test_thread_gold_names_records_by_key_and_says_what_sits_under_nothing(db, tools):
    def node(type_, key, name, item=None):
        return db.execute(text("INSERT INTO nodes (type, key, name, source_item_id) VALUES (:t, :k, :n, :i) "
                               "RETURNING id"), {"t": type_, "k": key, "n": name, "i": item}).scalar_one()
    sent, inbox = _item(db, "mira/Sent/cur/1.", "fp-trip"), _item(db, "mira/INBOX/cur/2.", "fp-hostel")
    first, second = node("Message", "fp-trip", "Lisbon?", sent), node("Message", "fp-hostel", "Hostel booked", inbox)
    node("Project", "backbone:org:u", "Lakeshore University")  # the school again, with nothing under it
    school = node("Project", "backbone:u", "Lakeshore University")
    trip = node("Project", "thread:lisbon", "Lisbon · Jan-Mar 2025")
    folder = node("Project", "project:lisbon", "Lisbon 2025")  # the same trip again, named by its folder
    db.execute(text("INSERT INTO edges (src, dst, type) VALUES (:a, :t, 'PART_OF'), (:a, :f, 'PART_OF'), "
                    "(:b, :f, 'PART_OF'), (:t, :s, 'PART_OF')"),
               {"a": first, "b": second, "t": trip, "f": folder, "s": school})
    gold = {"threads": [{"name": "Lakeshore University", "terms": ["Lakeshore University"]},
                        {"name": "Lisbon trip", "terms": ["Lisbon"], "parent": None,
                         "must_include": [_key("mira/Sent/cur/1.", "fp-trip"), _key("mira/INBOX/cur/2.", "fp-hostel")]}]}
    report = tools.threads.score(gold)
    school_row, lisbon = report["threads"]
    assert (school_row["records"], school_row["duplicates"]) == (1, 1)  # it holds what its threads hold
    assert (lisbon["found"], lisbon["duplicates"], lisbon["include_recall"]) == ("Lisbon 2025", 1, 1.0)
    assert lisbon["parent_ok"] is False  # its duplicate sits under the school, and a trip isn't part of it
    assert (report["summary"]["hierarchy_ok"], report["summary"]["hierarchy_of"]) == (0, 1)
    db.execute(text("DELETE FROM edges WHERE src = :t AND dst = :s"), {"t": trip, "s": school})
    report = tools.threads.score(gold)
    assert report["threads"][1]["parent_ok"] is True and report["threads"][0]["records"] == 0
