"""Every value `source_items.source` takes, which connector writes it, and what kind of record it holds.

A connector can store more than one value (Google Takeout writes one per product), and the Sources page entry that
describes a connector (`sources.SOURCES`) is also its id in IGNEOUS_LLM_SOURCES. Code that needs "the sources of a
kind" asks here instead of keeping its own list. What a record is called where it is shown is the name of the entry
that writes it, unless its value has a label of its own (`sources.labels`, which GET /sources serves).
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
    # What a record from it is called where it is shown, when the name of the entry that writes it would say less
    # (one of Takeout's products) or the wrong thing (a value more than one source stores).
    label: str | None = None


CATALOG: tuple[Stored, ...] = (
    Stored("email", "email", "mail", label="Email"),  # the Enron demo and the persona's mailbox
    Stored("gmail", "gmail", "mail"),
    Stored("imessage", "imessage", "chat"),
    Stored("takeout_chat", "takeout", "chat", label="Google Chat"),
    Stored("ai_chats", "ai_chats", "ai_chat", label="AI chats"),  # coding sessions and chat exports
    Stored("gcal", "gcal", "calendar"),
    Stored("takeout_meet", "takeout", "calendar", label="Google Meet"),
    Stored("gcontacts", "gcontacts", "contacts"),
    Stored("maccontacts", "maccontacts", "contacts"),
    Stored("drive", "drive", "document"),
    Stored("fs", "fs", "document", local_text=True),
    Stored("notes", "notes", "document", local_text=True),
    Stored("github", "github", "code"),
    Stored("takeout_tasks", "takeout", "tasks", label="Google Tasks"),
    Stored("linkedin", "linkedin", "profile"),
    Stored("takeout_activity", "takeout", "activity", label="My Activity"),
    Stored("takeout_chrome", "takeout", "activity", label="Chrome"),
    Stored("takeout_maps", "takeout", "places", label="Google Maps"),
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
