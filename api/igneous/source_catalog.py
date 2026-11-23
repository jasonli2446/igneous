"""Every value `source_items.source` takes, which connector writes it, and what kind of record it holds.

A connector can store more than one value (Google Takeout writes one per product), and the Sources page entry that
describes a connector (`sources.SOURCES`) is also its id in IGNEOUS_LLM_SOURCES. Code that needs "the sources of a
kind" asks here instead of keeping its own list.
"""
from dataclasses import dataclass

# Kinds of record. "activity" is what you browsed and searched; "places" what Maps saved or saw you visit.
KINDS = ("mail", "chat", "ai_chat", "calendar", "contacts", "document", "code", "tasks", "profile", "activity",
         "places")


@dataclass(frozen=True)
class Stored:
    value: str             # source_items.source
    connector: str         # the Sources page entry that writes it
    kind: str              # one of KINDS
    local_text: bool = False  # text from files on this computer: hosted models also need the local-sources switch


CATALOG: tuple[Stored, ...] = (
    Stored("email", "email", "mail"),
    Stored("gmail", "gmail", "mail"),
    Stored("imessage", "imessage", "chat"),
    Stored("takeout_chat", "takeout", "chat"),
    Stored("ai_chats", "ai_chats", "ai_chat"),
    Stored("gcal", "gcal", "calendar"),
    Stored("takeout_meet", "takeout", "calendar"),
    Stored("gcontacts", "gcontacts", "contacts"),
    Stored("maccontacts", "maccontacts", "contacts"),
    Stored("vcard", "vcard", "contacts"),
    Stored("drive", "drive", "document"),
    Stored("fs", "fs", "document", local_text=True),
    Stored("notes", "notes", "document", local_text=True),
    Stored("github", "github", "code"),
    Stored("takeout_tasks", "takeout", "tasks"),
    Stored("linkedin", "linkedin", "profile"),
    Stored("takeout_activity", "takeout", "activity"),
    Stored("takeout_chrome", "takeout", "activity"),
    Stored("takeout_maps", "takeout", "places"),
)
BY_VALUE = {s.value: s for s in CATALOG}

# What you wrote, said and planned: everything except contact cards, browsing and places.
CONTENT_KINDS = frozenset({"mail", "chat", "ai_chat", "calendar", "document", "code", "tasks", "profile"})
# Where the threads of your life (trips, classes, projects) show up: content and browsing, not cards or profiles.
LIFE_KINDS = frozenset({"mail", "chat", "ai_chat", "calendar", "document", "code", "tasks", "activity"})


def values(kinds) -> tuple[str, ...]:
    """Stored values whose kind is in `kinds`."""
    return tuple(s.value for s in CATALOG if s.kind in kinds)


def of_connector(connector: str) -> tuple[str, ...]:
    """Stored values a connector writes."""
    return tuple(s.value for s in CATALOG if s.connector == connector)


CONTENT = values(CONTENT_KINDS)
LIFE = values(LIFE_KINDS)
LOCAL_TEXT = tuple(s.value for s in CATALOG if s.local_text)
CONTACTS = values({"contacts"})  # contact cards, whichever source they came from: the spine builds people from them
