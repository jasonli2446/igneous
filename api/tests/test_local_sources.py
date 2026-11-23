"""Sources read from files on this computer: the Enron maildir, Messages, Contacts, Notes and AI coding chats."""
from pathlib import Path

import pytest
from sqlalchemy import text

from igneous.connectors import email_maildir
from igneous.connectors.base import run_sync
from igneous.connectors.email_maildir import MaildirConnector

MAIL = "Message-ID: <{0}@x>\nFrom: a@enron.com\nTo: b@enron.com\nSubject: {0}\n\nbody {0}\n"


def maildir(root: Path, *paths: str) -> Path:
    for rel in paths:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(MAIL.format(rel))
    return root


def stored(s, source: str) -> list[str]:
    return sorted(s.execute(text("SELECT external_id FROM source_items WHERE source = :s"), {"s": source}).scalars())


def test_each_mailbox_keeps_its_own_place(db, tmp_path):
    root = maildir(tmp_path, "allen-p/inbox/1.", "allen-p/inbox/2.", "lay-k/inbox/1.")
    run_sync(db, MaildirConnector(root, ["lay-k"]))
    run_sync(db, MaildirConnector(root, ["allen-p"]))  # sorts before lay-k's position: still read in full
    assert stored(db, "email") == ["allen-p/inbox/1.", "allen-p/inbox/2.", "lay-k/inbox/1."]
    cursor = db.execute(text("SELECT cursor FROM sync_state WHERE connector = 'email'")).scalar_one()
    assert cursor == {"mailboxes": {"allen-p": "allen-p/inbox/2.", "lay-k": "lay-k/inbox/1."}}


def test_a_shared_position_holds_for_its_own_mailbox(tmp_path):
    root = maildir(tmp_path, "allen-p/inbox/1.", "allen-p/inbox/2.", "lay-k/inbox/1.")
    events = list(MaildirConnector(root).sync({"last_path": "allen-p/inbox/1."}))
    assert [e.external_id for e in events if hasattr(e, "external_id")] == ["allen-p/inbox/2.", "lay-k/inbox/1."]


def test_a_resumed_mailbox_skips_nothing_whatever_the_folder_names(db, tmp_path, monkeypatch):
    monkeypatch.setattr(email_maildir, "CHECKPOINT_EVERY", 1)
    root = maildir(tmp_path, "x/a/1.", "x/a-b/1.")  # as Paths "a" sorts before "a-b"; as strings after it
    run_sync(db, MaildirConnector(root, ["x"]), limit=1)
    run_sync(db, MaildirConnector(root, ["x"]))
    assert stored(db, "email") == ["x/a-b/1.", "x/a/1."]


def test_an_unknown_mailbox_is_an_error(tmp_path):
    root = maildir(tmp_path, "allen-p/inbox/1.")
    with pytest.raises(ValueError, match="No mailbox nobody-x"):
        list(MaildirConnector(root, ["nobody-x"]).sync({}))
