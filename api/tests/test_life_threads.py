"""The threads pipeline on a synthetic life (tests/life_threads.py): every gold thread found, once, under the right
parent; two rebuilds with no new data change nothing, whatever the hash seed of the process that runs each; and
candidates come out the same whatever the hash seed or the order of the records."""
import json
import os
import random
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import life_threads
import pytest
from conftest import API
from life_threads import life  # noqa: F401
from pipeline_data import local_models  # noqa: F401
from sqlalchemy import text

from igneous import pipeline
from igneous.graph import threads


@pytest.mark.usefixtures("life")
def test_every_gold_thread_is_found_once_under_its_parent_and_the_long_stages_say_how_far_they_are(db, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(API) / "eval"))  # the eval's modules import each other by name
    from threads_eval import score

    states = []
    pipeline.run(db, progress=lambda state, force=False: states.append(dict(state)))
    out = score(life_threads.GOLD)
    assert out["summary"] == {"found": 8, "of": 8, "duplicated": 0, "leaks": 0, "hierarchy_ok": 4,
                              "hierarchy_of": 4, "participant_errors": 0}, json.dumps(out, indent=1, default=str)
    kinds = {r["name"]: r["kind"] for r in out["threads"]}
    assert kinds["Portland trip"] == kinds["Lisbon trip"] == "trip" and kinds["CS 401"] == kinds["EE 221"] == "class"
    # Within its stage, as the Sources page shows it: how many candidates of how many, then the next step.
    judging = [(st["count"], st["total"]) for st in states
               if st["stage"] == "threads" and st["note"] == "judging new threads" and st["count"]]
    assert judging and judging[-1][0] == judging[-1][1] > 1
    assert any(st["note"] == "resolving duplicate threads" and st["count"] is None for st in states)
    counted = [(st["count"], st["total"]) for st in states if st["stage"] == "verify" and st["count"]]
    assert counted and counted[-1][0] == counted[-1][1]  # every planned model call made


def snapshot(s) -> dict:
    """Everything a rebuild derives, by natural keys (edge ids are new every run; node ids must not be)."""
    def rows(sql: str) -> list:
        return [tuple(json.dumps(v, sort_keys=True, default=str) if isinstance(v, (dict, list)) else v for v in r)
                for r in s.execute(text(sql)).all()]
    out = {
        "nodes": rows("SELECT id, type, key, name, props FROM nodes ORDER BY type, key"),
        "edges": rows("""
            SELECT a.type || ':' || a.key, b.type || ':' || b.key, e.type, e.extractor, e.props, e.confidence,
                   e.valid_from, e.valid_to, e.source_item_id,
                   (SELECT array_agg(ev.source_item_id || '/' || ev.kind ORDER BY ev.source_item_id, ev.kind)
                    FROM edge_evidence ev WHERE ev.edge_id = e.id)
            FROM edges e JOIN nodes a ON a.id = e.src JOIN nodes b ON b.id = e.dst ORDER BY 1, 2, 3, 4"""),
        "canonical": rows("""SELECT n.key, h.key FROM canonical c JOIN nodes n ON n.id = c.node_id
                             JOIN nodes h ON h.id = c.canonical_id ORDER BY 1"""),
        "registry": rows("""SELECT key, name, kind, terms, span_start, span_end, status, merged_into, pinned, origin,
                                   confidence, anchor, anchor_name, judged, merged_by FROM thread_registry ORDER BY key"""),
        "verdicts": rows("""SELECT src_key, dst_key, type, extractor, evidence, verdict, confidence, reason, source
                            FROM edge_verdicts ORDER BY 1, 2, 3, 4"""),
        "dropped": rows("SELECT src_key, dst_key, type, extractor FROM edges_dropped ORDER BY 1, 2, 3, 4"),
    }
    s.rollback()
    return out


REBUILD = """
import sys
sys.path.insert(0, sys.argv[1])
import life_threads, pipeline_data
from igneous import llm, pipeline, providers
from igneous.db import SessionLocal

models = pipeline_data.LocalModels()
for factory in ("_embedder", "_reranker", "_chat_model"):
    setattr(providers, factory, lambda: models)
llm.structured = life_threads.model
llm.settings.llm_sources = life_threads.SOURCES
llm.settings.local_sources_model_enabled = True
with SessionLocal() as s:
    pipeline.run(s)
"""


def test_two_rebuilds_with_no_new_data_change_nothing_whatever_the_hash_seed(committed_db):
    """Each rebuild runs in a process of its own, under another PYTHONHASHSEED: nothing it derives (nodes, links,
    keys, names, identities, the registry, verdicts) may depend on the order a set or dict happens to iterate in."""
    life_threads.build(committed_db)
    snapshots = []
    for seed in ("1", "2"):
        subprocess.run([sys.executable, "-c", REBUILD, str(Path(__file__).parent)], check=True, cwd=API,
                       env={**os.environ, "PYTHONHASHSEED": seed})
        snapshots.append(snapshot(committed_db))
    first, second = snapshots
    assert first["registry"] and first["verdicts"]
    for part in first:
        assert second[part] == first[part], part


CANDIDATES = """
import json, sys
from datetime import datetime
from types import SimpleNamespace
from igneous.graph.threads import burst_candidates, thread_key
items = [SimpleNamespace(**{**r, "occurred_at": datetime.fromisoformat(r["occurred_at"])})
         for r in json.load(open(sys.argv[1]))]
print(json.dumps([[thread_key(c), c["terms"], sorted(c["items"])] for c in burst_candidates(items, set())]))
"""


def _candidates(items) -> list:
    return [[threads.thread_key(c), c["terms"], sorted(c["items"])] for c in threads.burst_candidates(items, set())]


@pytest.mark.usefixtures("life")
def test_candidates_are_the_same_whatever_the_hash_seed_or_record_order(db, tmp_path):
    rows = [dict(r._mapping) for r in threads._items(db)]
    items = [SimpleNamespace(**r) for r in rows]
    expected = _candidates(items)
    assert {"Portland", "Lisbon", "Kiosk Prototype"} <= {terms[0] for _key, terms, _items in expected}
    for seed in (1, 2):
        shuffled = items[:]
        random.Random(seed).shuffle(shuffled)
        assert _candidates(shuffled) == expected
    path = tmp_path / "items.json"
    path.write_text(json.dumps(rows, default=str))
    seen = []
    for seed in ("0", "12345"):
        out = subprocess.run([sys.executable, "-c", CANDIDATES, str(path)], capture_output=True, text=True, check=True,
                             cwd=API, env={**os.environ, "PYTHONHASHSEED": seed})
        seen.append(json.loads(out.stdout))
    assert seen[0] == seen[1] == json.loads(json.dumps(expected))
