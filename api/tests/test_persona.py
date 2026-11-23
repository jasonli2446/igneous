"""The synthetic persona: one seed writes the same bytes, every importer reads every file the generator writes, the
gold set names records that exist and quotes them exactly, and a slice of the persona builds its graph end to end
through the Sources registry and the pipeline, with a stubbed model."""
import json
import re
import zipfile
from datetime import date
from pathlib import Path

import pytest
from pipeline_data import local_models  # noqa: F401
from sqlalchemy import text
from typer.testing import CliRunner

from igneous import llm, persona, pipeline, sources
from igneous.cli import app as cli
from igneous.connectors import ai_exports, linkedin, takeout
from igneous.connectors.base import SourceItem
from igneous.connectors.email_maildir import MaildirConnector
from igneous.connectors.filesystem import FilesystemConnector
from igneous.persona import load, project

SLICE = date(2025, 8, 31)  # the first year: hiring, two semesters of classes, Lisbon, the summer field trials


@pytest.fixture(scope="module")
def full(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("persona") / "persona"
    persona.generate(out)
    return out


def tree(root: Path) -> dict[str, tuple[bytes, int]]:
    return {p.relative_to(root).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in sorted(root.rglob("*")) if p.is_file()}


def test_one_seed_writes_the_same_bytes_and_dates(full, tmp_path):
    again = tmp_path / "again"
    persona.generate(again)
    assert tree(again) == tree(full)
    a, b = tmp_path / "a", tmp_path / "b"
    persona.generate(a, seed=7, until=SLICE)
    persona.generate(b, seed=8, until=SLICE)
    assert tree(a).keys() != tree(b).keys()  # another seed: other times, so other files


def test_generating_replaces_a_persona_but_never_another_folder(tmp_path):
    out = tmp_path / "p"
    persona.generate(out, until=date(2024, 9, 30))
    (out / "stale.txt").write_text("left from an earlier run")
    persona.generate(out, until=date(2024, 9, 30))
    assert not (out / "stale.txt").exists()
    other = tmp_path / "notes"
    other.mkdir()
    (other / "todo.md").write_text("mine")
    with pytest.raises(ValueError, match="holds no persona"):
        persona.generate(other)
    assert (other / "todo.md").read_text() == "mine"


def test_every_importer_reads_every_file_it_is_given(full):
    m = json.loads((full / persona.MANIFEST).read_text())
    counts, ex = m["counts"], m["exports"]

    files = [p for p in (full / ex["maildir"]).rglob("*") if p.is_file()]
    mails = [x for x in MaildirConnector(full / ex["maildir"], [ex["mailbox"]]).sync({}) if isinstance(x, SourceItem)]
    assert len(mails) == len(files) == counts["mail"]
    assert all(x.title and x.body_text and x.occurred_at and x.raw_meta["from"] for x in mails)
    sent = {x.raw_meta["from"][0]["email"] for x in mails if x.external_id.split("/")[1] == "Sent"}
    assert sent == {"mcastellanos@lakeshore.example.edu"}  # the archive's owner, by its sent folder

    items, report = takeout.parse([full / ex["takeout"]])
    with zipfile.ZipFile(full / ex["takeout"]) as zf:
        names = [n for n in zf.namelist() if not n.endswith("/")]
    unread = sorted(n.split("/", 1)[1] for n in names
                    if (kind := takeout.classify(n.split("/", 1)[1])) is None or kind[0] == "chat_user")
    assert unread[0] == "Calendar/mcastellanos@lakeshore.example.edu.ics" and unread[1].startswith("Google Chat/Users/")
    assert unread[2:] == ["archive_browser.html"]  # no importer reads a calendar yet
    c = report["counts"]
    assert (c["chat_messages"], c["tasks"], c["meet_conferences"], c["place_visits"], c["chrome_visits"]) == (
        counts["chat_lines"], counts["tasks"], counts["meetings"], counts["place_visits"], counts["page_visits"])
    assert c["activity_entries"] == counts["activity"]
    gemini_days = {i.external_id for i in items if i.source == "ai_chats"}
    assert report["parts"]["gemini"] == len(gemini_days) and 0 < len(gemini_days) <= counts["gemini_prompts"]
    assert report["notes"] == [] and report["redactions"] == 0
    assert {i.source for i in items} == {"takeout_chat", "takeout_tasks", "takeout_meet", "takeout_maps",
                                         "takeout_activity", "takeout_chrome", "ai_chats"}

    found, report = linkedin.find_items([full / ex["linkedin"]])
    assert (report["profile"], report["positions"], report["education"], report["connections"]) == (
        1, 3, 1, counts["linkedin_connections"])
    assert report["files"] == ["connections.csv", "education.csv", "languages.csv", "positions.csv", "profile.csv",
                               "projects.csv", "skills.csv"]
    assert found[0].raw_meta["profile"]["projects"][0]["name"] == "Tidepool"

    convs = list(ai_exports.conversations(full / ex["chatgpt"]))
    assert len(convs) == counts["chatgpt_conversations"] and {kind for kind, _ in convs} == {"chatgpt"}
    assert all(turns and turns[0]["role"] == "me" for kind, (meta, turns) in convs)

    docs = [x for x in FilesystemConnector(full / ex["documents"]).sync({}) if isinstance(x, SourceItem)]
    on_disk = [p for p in (full / ex["documents"]).rglob("*") if p.is_file() and ".git" not in p.parts]
    repos = [x for x in docs if x.raw_meta.get("kind") == "repo"]
    assert len(docs) == len(on_disk) + 1 and len(repos) == 1 == counts["documents"] - len(on_disk) - 1
    assert repos[0].raw_meta["remote"] == project.REMOTE and repos[0].title == "tidepool"
    unparsed = [x.title for x in docs if x not in repos and (x.raw_meta.get("parse_status") or not x.body_text)]
    assert unparsed == []  # every document, Word, Excel, PowerPoint and PDF included, gives its text
    assert {Path(x.title).suffix for x in docs} >= {".md", ".pdf", ".docx", ".xlsx", ".pptx", ".py", ".txt"}


def _texts(root: Path) -> dict[tuple[str, str], str]:
    """Every record's title and text, keyed by (source, external id) and, for a file, (fs, its path)."""
    m = json.loads((root / persona.MANIFEST).read_text())
    ex = m["exports"]
    items = [x for x in MaildirConnector(root / ex["maildir"], [ex["mailbox"]]).sync({}) if isinstance(x, SourceItem)]
    items += takeout.parse([root / ex["takeout"]])[0]
    items += [x for x in ai_exports.ExportConnector(root / ex["chatgpt"], "chatgpt").sync({})]
    out = {(x.source, x.external_id): f"{x.title}\n{x.body_text}" for x in items}
    for x in FilesystemConnector(root / ex["documents"]).sync({}):
        if isinstance(x, SourceItem):
            out[("fs", x.external_id.split("/", 1)[1])] = f"{x.title}\n{x.body_text}"
    return out


def _key(k: dict) -> tuple[str, str]:
    return k["source"], k.get("path") or k["external_id"]


def test_the_gold_set_names_records_the_generator_writes_and_quotes_them(full):
    texts = _texts(full)
    keys = json.loads((full / persona.MANIFEST).read_text())["keys"]
    flat = lambda s: re.sub(r"\s+", " ", s)  # noqa: E731
    for q in persona.load_gold("questions.jsonl"):
        assert all(_key(k) in texts for k in q["gold"] + q.get("bridge", [])), q["id"]
        if q["answerable"]:
            assert any(flat(q["answer_quote"]) in flat(texts[_key(k)]) for k in q["gold"]), q["id"]
    threads = persona.load_gold("threads.json")["threads"]
    names = {t["name"] for t in threads}
    for t in threads:
        assert all(_key(k) in texts for k in t.get("must_include", []) + t.get("must_exclude", [])), t["name"]
        assert t.get("parent") in names | {None}
    tagged = {_key(k) for k in keys.values()}
    assert {_key(k) for t in threads for k in t.get("must_include", [])} <= tagged
    people = persona.load_gold("people.json")
    assert all(len(p["identities"]) >= 2 for p in people["people"]) and people["distinct"]


def test_the_persona_has_its_own_database(db, monkeypatch, full):
    with pytest.raises(ValueError, match="must be named igneous_persona"):
        load.create_database()
    db.execute(text("INSERT INTO source_items (source, external_id, content_hash) VALUES ('gmail', 'x', 'h')"))
    with pytest.raises(sources.SetupNeeded, match="never beside other records"):
        sources.sync("persona", db, opts=sources.SyncOptions(path=full))
    with pytest.raises(sources.SetupNeeded, match="No persona at"):
        sources.sync("persona", db, opts=sources.SyncOptions(path=full.parent / "nowhere"))


def test_the_cli_generates_a_slice(tmp_path):
    out = tmp_path / "persona"
    result = CliRunner().invoke(cli, ["persona", "generate", str(out), "--until", "2024-10-31"])
    assert result.exit_code == 0, result.output
    assert result.output.startswith("wrote Mira Castellanos, 2024-08-19 to 2024-10-31")
    assert json.loads((out / persona.MANIFEST).read_text())["last_day"] == "2024-10-31"


# ---- a slice, end to end ----

THREADS = persona.load_gold("threads.json")["threads"]


def stub(system: str, user: str, schema: dict, **_options):
    """A model that answers as a careful reader of the persona would: it knows its threads (the gold set), reads its
    resumes, keeps every link it checks, and names nothing it isn't sure of."""
    props = schema["properties"]
    if "defining_terms" in props:  # the threads stage's judge
        shared = (re.search(r"^Shared terms: (.*)$", user, re.M) or [None, ""])[1].lower()
        shown = len(re.findall(r"^\d+\. \[", user, re.M))
        for g in THREADS:
            if any(t.lower() in shared for t in g["terms"]) and g.get("kind") not in ("organization",):
                return {"keep": True, "kind": g["kind"], "name": g["name"], "confidence": 0.9,
                        "defining_terms": g["terms"], "members": list(range(shown))}, {}
        return {"keep": False, "kind": "", "name": "", "confidence": 0.9, "defining_terms": [], "members": []}, {}
    if "is_about_owner" in props:  # the profile stage reading a resume
        return {"is_about_owner": True, "person_name": "Mira Castellanos", "projects": [],
                "education": [{"school": "Lakeshore University", "degree": "B.S. Computer Science", "start": "2022-08",
                               "end": "2026-05", "domain_hint": "lakeshore.example.edu"}],
                "experience": [{"org": "Harbor Street Coffee", "title": "Barista", "start": "2022-09", "end": "2024-08",
                                "domain_hint": "", "parent": "", "project": ""}]}, {}
    if "edges" in props:  # the verifier: every link it is shown holds
        return {"edges": [{"edge": i, "verdict": "keep", "confidence": 0.9, "reason": "the records show it"}
                          for i in range(user.count("### Edge "))]}, {}
    if "relation" in props:
        return {"relation": "distinct", "label": "", "confidence": 0.9, "reason": "", "shared_identity": "",
                "container": ""}, {}
    if "keep" in props:  # the entities stage's screen
        return {"keep": [line[2:] for line in user.splitlines() if line.startswith("- ")]}, {}
    return {k: {"array": [], "boolean": False, "number": 0, "integer": 0}.get(v.get("type"), "")
            for k, v in props.items()}, {}


@pytest.mark.usefixtures("local_models")
def test_a_slice_builds_its_graph_through_the_sources_registry_and_the_pipeline(db, monkeypatch, tmp_path):
    root = tmp_path / "persona"
    manifest = persona.generate(root, until=SLICE)
    result = sources.sync("persona", db, opts=sources.SyncOptions(path=root))
    stored = dict(db.execute(text("SELECT source, count(*) FROM source_items GROUP BY 1")).all())
    assert set(result["accounts"]) == {"mail", "documents", "takeout", "linkedin", "chatgpt"}
    assert result["accounts"]["mail"]["inserted"] == stored["email"] == manifest["counts"]["mail"]
    assert stored["linkedin"] == manifest["counts"]["linkedin_connections"] + 1  # the profile and each connection
    assert {s["id"]: s["items"] for s in sources.describe(db)}["persona"] == sum(stored.values())

    monkeypatch.setattr(llm, "structured", stub)
    monkeypatch.setattr(llm.settings, "llm_sources", "email,fs,takeout,linkedin,ai_chats")
    monkeypatch.setattr(llm.settings, "local_sources_model_enabled", True)
    pipeline.run(db)

    (intro,) = db.execute(text("""
        SELECT a.name, b.name, i.name FROM edges e JOIN nodes a ON a.id = e.src JOIN nodes b ON b.id = e.dst
        JOIN nodes i ON i.id = (e.props->>'introducer')::bigint WHERE e.type = 'INTRODUCED'""")).all()
    assert {intro[0], intro[1]} == {"Mira Castellanos", "Owen Brandt"} and intro[2] == "Ruth Adeyemi"

    def cluster(key: str) -> int:
        return db.execute(text("SELECT COALESCE(c.canonical_id, n.id) FROM nodes n LEFT JOIN canonical c "
                               "ON c.node_id = n.id WHERE n.type = 'Person' AND n.key = :k"), {"k": key}).scalar_one()
    for identities in (("dkimura@lakeshore.example.edu", "dana.kimura@inbox.example.net", "linkedin:dana-kimura-wq41"),
                       ("owen@northwind.example.com", "linkedin:owen-brandt-nw")):
        assert len({cluster(k) for k in identities}) == 1, identities

    def thread(name: str):
        return db.execute(text("SELECT id, props FROM nodes WHERE type = 'Project' AND name = :n"), {"n": name}).one()

    def took_part(name: str) -> set[str]:
        return set(db.execute(text("""SELECT n.name FROM edges e JOIN nodes n ON n.id = e.src
                                      WHERE e.dst = :t AND e.type = 'INVOLVED_IN'"""), {"t": thread(name).id}).scalars())

    # A trip, found by its burst and judged; who went is who was in its group chat.
    assert thread("Lisbon trip").props["kind"] == "trip"
    assert {"Dana Kimura", "Sofia Lindgren", "Rafael Ortiz"} <= took_part("Lisbon trip")
    assert "Theo Marsh" not in took_part("Lisbon trip")
    # Classes, under the school on Mira's LinkedIn; the job: the hiring is Northwind's own thread. Each organization is
    # one node (the company LinkedIn names, holding the domain its mail comes from), standing for one thread.
    def standing_for(org: str) -> dict[int, dict]:
        return dict(db.execute(text("""SELECT p.id, p.props FROM nodes p JOIN nodes o ON o.id = (p.props->>'org')::bigint
                                       WHERE p.type = 'Project' AND o.name = :o"""), {"o": org}).all())

    assert db.execute(text("""SELECT key, props->'domains' FROM nodes WHERE type = 'Organization'
                              AND name IN ('Lakeshore University', 'Northwind Robotics') ORDER BY key""")).all() == [
        ("org:lakeshore-university", ["lakeshore.example.edu"]), ("org:northwind-robotics", ["northwind.example.com"])]
    schools = set(standing_for("Lakeshore University"))
    assert len(schools) == len(standing_for("Northwind Robotics")) == 1
    for course in ("CS 362", "ENVS 210"):
        assert thread(course).props["kind"] == "class"
        assert schools & set(db.execute(text("SELECT dst FROM edges WHERE src = :c AND type = 'PART_OF'"),
                                        {"c": thread(course).id}).scalars()), course
    assert any(job.get("kind") == "job" and (job.get("records") or 0) >= 6
               for job in standing_for("Northwind Robotics").values())
    assert {"Priya Raman", "Alex Chen"} <= took_part("Heron field trials")  # on the depth-drift thread with Mira
    # The side project, pinned from the LinkedIn export's projects.
    tidepool = thread("Tidepool").props
    assert tidepool["kind"] == "project" and tidepool["records"] >= 6
