"""LinkedIn export importer (#157). Synthetic CSVs only; DB tests use the dev database and clean up after themselves."""
import zipfile
from pathlib import Path

import pytest
from sqlalchemy import text

from igneous.connectors import linkedin
from igneous.db import SessionLocal

TOKEN = "ghp_" + "Q" * 36
FILES = {
    "Profile.csv": 'First Name,Last Name,Headline,Summary,Industry,Geo Location,Birth Date\n'
                   'Pat,Example,Builder of things,"Wrote code, a lot.",Software,Nowhere,Jan 1\n',
    "Positions.csv": 'Company Name,Title,Description,Location,Started On,Finished On\n'
                     f'Acme Corp,Engineer,"Built the thing {TOKEN}",Ohio,Jun 2025,\n'
                     'Old Co,Intern,,,"Sep 2023","Dec 2023"\n',
    "Education.csv": 'School Name,Start Date,End Date,Notes,Degree Name,Activities\n'
                     'Example University,2023,2026,,BS Computer Science,\n',
    "Projects.csv": 'Title,Description,Url,Started On,Finished On\nWidget Tracker,Tracks widgets,http://x.test,Jan 2025,\n',
    "Skills.csv": "Name\nPython\nGo\n",
    "Connections.csv": 'Notes:\n"When exporting your connection data, you may notice that some emails are missing."\n\n'
                       "First Name,Last Name,URL,Email Address,Company,Position,Connected On\n"
                       "Sam,Lee,https://www.linkedin.com/in/sam-lee-123,sam.lee@linkedin-test.example,Globex,Manager,03 Mar 2024\n"
                       "Kim,Wu,https://www.linkedin.com/in/kim-wu,,Globex,Analyst,10 Apr 2024\n"
                       ",,,,,,\n",
}


def write_zip(path: Path, files: dict = FILES, prefix: str = "") -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, content in files.items():
            zf.writestr(prefix + name, content)
    return path


@pytest.fixture
def zpath(tmp_path):
    return write_zip(tmp_path / "Basic_LinkedInDataExport.zip", prefix="export/")


def test_parse_profile_and_connections(zpath):
    items, report = linkedin.find_items([zpath])
    by_id = {i.external_id: i for i in items}
    assert set(by_id) == {"profile", "connection/sam-lee-123", "connection/kim-wu"}
    assert report["connections"] == 2 and report["positions"] == 2 and report["education"] == 1
    prof = by_id["profile"].raw_meta
    assert prof["profile"] == {
        "is_about_owner": True, "person_name": "Pat Example",
        "experience": [
            {"org": "Acme Corp", "title": "Engineer", "start": "2025-06", "end": None, "domain_hint": None},
            {"org": "Old Co", "title": "Intern", "start": "2023-09", "end": "2023-12", "domain_hint": None}],
        "education": [{"school": "Example University", "degree": "BS Computer Science", "start": "2023-01",
                       "end": "2026-12"}],
        "projects": [{"name": "Widget Tracker", "description": "Tracks widgets", "org": None}]}
    assert prof["skills"] == ["Python", "Go"]
    assert "Birth Date" not in str(prof) and "Nowhere" not in str(prof)
    assert TOKEN not in by_id["profile"].body_text and TOKEN not in str(prof)
    sam = by_id["connection/sam-lee-123"]
    assert sam.raw_meta["email"] == "sam.lee@linkedin-test.example" and sam.raw_meta["connected_on"] == "2024-03-03"
    assert sam.raw_meta["company"] == "Globex" and sam.title == "Sam Lee"
    assert by_id["connection/kim-wu"].raw_meta["email"] is None


def test_folder_connections_only_and_missing_columns(tmp_path):
    (tmp_path / "Connections.csv").write_text(FILES["Connections.csv"].replace("URL,", "Profile URL,"))
    items, report = linkedin.find_items([tmp_path])
    assert [i.external_id for i in items] == ["connection/sam-lee-123", "connection/kim-wu"]
    assert report["profile"] == 0
    (tmp_path / "Connections.csv").write_text("First Name,Last Name\nNo,Url\n")  # no URL, no date: hashed id
    items, _ = linkedin.find_items([tmp_path])
    assert len(items) == 1 and items[0].external_id.startswith("connection/h-")


def test_preflight_rejects_empty(tmp_path):
    (tmp_path / "other.csv").write_text("a,b\n1,2\n")
    with pytest.raises(linkedin.LinkedInError):
        linkedin.preflight([tmp_path])


def clean():
    with SessionLocal() as s:
        s.execute(text("DELETE FROM nodes WHERE key LIKE 'linkedin:%' OR key LIKE '%@linkedin-test.example' "
                       "OR key IN ('org:globex', 'linkedin-test.example')"))
        s.execute(text("DELETE FROM source_items WHERE source = 'linkedin'"))
        s.execute(text("DELETE FROM sync_state WHERE connector = 'linkedin'"))
        s.commit()


@pytest.fixture
def db():
    try:
        with SessionLocal() as s:
            s.execute(text("SELECT count(*) FROM source_items"))
    except Exception:
        pytest.skip("Postgres not available")
    clean()
    yield
    clean()


def test_reimport_is_noop_and_graph(db, zpath):
    from igneous.graph import spine
    from igneous.graph.invariants import check_graph

    with SessionLocal() as s:
        first = linkedin.run_import(s, [zpath])
        assert first["inserted"] == 3 and first["unchanged"] == 0
    with SessionLocal() as s:
        second = linkedin.run_import(s, [zpath])
        assert second["inserted"] == 0 and second["updated"] == 0 and second["unchanged"] == 3
    with SessionLocal() as s:
        # an existing mail identity for Sam, named only by its address
        s.execute(text("""INSERT INTO nodes (type, key, name, props) VALUES
            ('Person', 'sam.lee@linkedin-test.example', 'sam.lee@linkedin-test.example', '{}')
            ON CONFLICT DO NOTHING"""))
        s.commit()
        spine.run(s)
        sam = s.execute(text("SELECT id, name, props FROM nodes WHERE type='Person' "
                             "AND key='sam.lee@linkedin-test.example'")).one()
        assert sam.name == "Sam Lee" and sam.props["named_by"] == "linkedin"
        same = s.execute(text("""SELECT count(*) FROM edges e JOIN nodes l ON l.id IN (e.src, e.dst)
                                 WHERE e.type='SAME_AS' AND e.extractor='linkedin' AND l.key='linkedin:sam-lee-123'
                                   AND :sam IN (e.src, e.dst)"""), {"sam": sam.id}).scalar()
        assert same == 1
        members = s.execute(text("""SELECT p.name, o.key, o.name FROM edges e JOIN nodes p ON p.id = e.src
                                    JOIN nodes o ON o.id = e.dst
                                    WHERE e.type='MEMBER_OF' AND e.extractor='linkedin' AND o.name = 'Globex'
                                    ORDER BY p.name""")).all()
        # no email domain -> org:<slug>; a known work-email domain keys the org by that domain
        assert [(m[0], m[1]) for m in members] == [("Kim Wu", "org:globex"), ("Sam Lee", "linkedin-test.example")]
        assert [v for v in check_graph(s) if v.details and v.details.get("edge_type") in ("MEMBER_OF", "SAME_AS")
                and v.edge_id in set(s.execute(text("SELECT id FROM edges WHERE extractor='linkedin'")).scalars())] == []
        n_edges = s.execute(text("SELECT count(*) FROM edges WHERE extractor='linkedin'")).scalar()
        # a second spine pass over unchanged data adds nothing
        s.execute(text("UPDATE source_items SET updated_at = now() WHERE source = 'linkedin'"))
        s.commit()
        spine.run(s)
        assert s.execute(text("SELECT count(*) FROM edges WHERE extractor='linkedin'")).scalar() == n_edges


def test_content_change_updates(db, tmp_path, zpath):
    with SessionLocal() as s:
        linkedin.run_import(s, [zpath])
        changed = dict(FILES, **{"Skills.csv": "Name\nPython\nRust\n"})
        stats = linkedin.run_import(s, [write_zip(tmp_path / "b.zip", changed)])
        assert stats["updated"] == 1 and stats["unchanged"] == 2
