"""Open links per source, the safe-meta allow-list, path confinement and the Why view's pure parts (#141).
Synthetic data only: no database."""
from datetime import UTC, datetime
from pathlib import Path

from igneous.connectors.filesystem import _root_key
from igneous.graph.traversal import edge_view, shortest_path, visible_edge
from igneous.items import item_card, open_target, resolve_local, safe_meta

WHEN = datetime(2025, 3, 4, 15, 0, tzinfo=UTC)


def test_gmail_link_uses_account_and_message_id():
    t = open_target("gmail", "18c0ffee", {"account": "me@example.com"})
    assert t["open_url"] == "https://mail.google.com/mail/?authuser=me@example.com#all/18c0ffee"
    assert t["open_label"] == "Open in Gmail" and t["local_path"] is None


def test_gcal_opens_the_day():
    t = open_target("gcal", "me@example.com/cal/ev1", {"account": "me@example.com"}, WHEN)
    assert t["open_url"] == "https://calendar.google.com/calendar/r/day/2025/03/04?authuser=me@example.com"
    assert open_target("gcal", "x", {}, None)["open_url"] is None


def test_drive_and_github_use_stored_links():
    assert open_target("drive", "a/1", {"web_view_link": "https://drive.google.com/file/d/1/view"})["open_url"] \
        == "https://drive.google.com/file/d/1/view"
    assert open_target("github", "a/r/pr/1", {"url": "https://github.com/o/r/pull/1"})["open_label"] == "Open on GitHub"
    assert open_target("drive", "a/1", {})["open_url"] is None


def test_non_https_links_are_dropped():
    assert open_target("github", "x", {"url": "javascript:alert(1)"})["open_url"] is None
    assert open_target("drive", "x", {"web_view_link": "file:///etc/passwd"})["open_url"] is None


def test_local_and_in_app_only_sources():
    t = open_target("fs", "abc/notes/a.md", {"path": "abc/notes/a.md"})
    assert t["local_path"] == "abc/notes/a.md" and t["open_url"] is None
    for source in ("imessage", "ai_chats", "takeout_chrome", "notes", "email"):
        assert open_target(source, "x", {"path": "notes/x"}) == {"open_url": None, "open_label": None,
                                                                  "local_path": None}


def test_safe_meta_never_returns_secrets_or_paths():
    meta = {"account": "me@example.com", "access_token": "t", "refresh_token": "r", "path": "abc/x",
            "labels": ["INBOX"], "from": [{"email": "a@example.com"}]}
    assert safe_meta(meta) == {"labels": ["INBOX"], "from": [{"email": "a@example.com"}]}


def test_resolve_local_stays_under_root(tmp_path: Path):
    root = tmp_path / "docs"
    root.mkdir()
    (root / "a.md").write_text("hi")
    (tmp_path / "secret.txt").write_text("no")
    key = _root_key(root.resolve())
    assert resolve_local(f"{key}/a.md", [root]) == (root / "a.md").resolve()
    assert resolve_local(f"{key}/../secret.txt", [root]) is None
    assert resolve_local(f"{key}/missing.md", [root]) is None
    assert resolve_local("otherkey/a.md", [root]) is None
    assert resolve_local(None, [root]) is None


def test_item_card_shape():
    card = item_card({"id": 7, "source": "gmail", "external_id": "m1", "title": "Hello", "occurred_at": WHEN,
                      "raw_meta": {"account": "me@example.com"}, "body_text": "Line one\n\n  line   two"})
    assert card["snippet"] == "Line one line two" and card["open_url"].endswith("#all/m1")
    assert set(card) == {"id", "source", "title", "date", "snippet", "open_url", "open_label", "local_file"}


def test_edge_view_carries_verifier_fields_and_degrades_without_them():
    row = {"id": 1, "type": "RELATED_TO", "extractor": "verify", "src": 1, "dst": 2, "confidence": 0.6,
           "props": {"verdict": "unsure", "reason": "Both appear in the same planning thread.", "confidence": 0.4}}
    v = edge_view(row, [])
    assert (v["verdict"], v["reason"], v["confidence"]) == ("unsure", "Both appear in the same planning thread.", 0.4)
    bare = edge_view({"id": 2, "type": "PART_OF", "extractor": "spine", "src": 1, "dst": 2, "confidence": None,
                      "props": {}}, [])
    assert bare["verdict"] is None and bare["reason"] is None and bare["confidence"] is None


def test_dropped_edges_are_not_visible():
    assert not visible_edge({"props": {"verdict": "drop"}})
    assert visible_edge({"props": {"verdict": "keep"}}) and visible_edge({"props": {}}) and visible_edge({})


def test_shortest_path_prefers_fewest_hops_and_caps_length():
    adj = {1: [(2, 10), (5, 14)], 2: [(1, 10), (3, 11)], 3: [(2, 11), (4, 12)], 4: [(3, 12)],
           5: [(1, 14), (4, 15)]}
    assert shortest_path(adj, {1}, {4}) == [(1, 5, 14), (5, 4, 15)]
    long = {1: [(2, 1)], 2: [(1, 1), (3, 2)], 3: [(2, 2), (4, 3)], 4: [(3, 3), (5, 4)], 5: [(4, 4)]}
    assert shortest_path(long, {1}, {4}) == [(1, 2, 1), (2, 3, 2), (3, 4, 3)]
    assert shortest_path(long, {1}, {5}) is None  # 4 hops
    assert shortest_path(adj, {1}, {9}) is None
