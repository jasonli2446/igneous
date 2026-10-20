"""Create a small, deterministic cross-source fixture corpus.

The corpus deliberately contains invented content only. It uses public Enron demo
persona addresses so future file and calendar importers can exercise cross-source
identity and participant links without handling personal data.
"""
import hashlib
import json
from pathlib import Path

NOTICE = "SYNTHETIC FIXTURE — invented for Igneous development; not an Enron record."
MANIFEST_NAME = "manifest.json"

FILES = {
    "files/2001-05-14-forecast-memo.md": f"""# Forecast working memo

> {NOTICE}

**Prepared for:** Phillip Allen, Tim Belden, and John Arnold
**Related personas:** phillip.allen@enron.com, tim.belden@enron.com, john.arnold@enron.com

## Purpose

Prepare a shared forecast review for the next planning cycle. The team needs one
assumption set, clear ownership, and a short list of open questions before the
review meeting.

## Proposed next steps

1. Tim will reconcile the regional assumptions.
2. John will identify the two largest variance drivers.
3. Phillip will collect decisions and circulate a one-page summary.

This memo is intentionally fictional. It is shaped to provide a document that
mentions the same demo personas as the accompanying calendar events and mail corpus.
""",
    "files/2001-05-14-forecast-assumptions.csv": """synthetic_fixture,scenario,owner_email,assumption,review_status
true,base_case,tim.belden@enron.com,Use the shared planning range,ready
true,upside_case,john.arnold@enron.com,Document variance drivers,needs_review
true,decision_log,phillip.allen@enron.com,Record owners and follow-ups,ready
""",
    "files/2001-05-15-forecast-review-slides.txt": f"""Forecast review — discussion outline
{NOTICE}

Slide 1: Review goal
Align on one set of forecast assumptions for the planning cycle.

Slide 2: Decisions needed
- Confirm the shared planning range.
- Assign owners for variance follow-up.

Slide 3: Attendees
Phillip Allen (phillip.allen@enron.com)
Tim Belden (tim.belden@enron.com)
John Arnold (john.arnold@enron.com)

Slide 4: Follow-up
Publish the decision log after the review.
""",
    "files/2001-06-04-operations-handoff.md": f"""# Operations handoff checklist

> {NOTICE}

**Prepared for:** Jeff Skilling, Ken Lay, and James Steffes
**Related personas:** jeff.skilling@enron.com, ken.lay@enron.com, james.steffes@enron.com

## Handoff objective

Make the fictional operations update easy to find across mail, a planning note,
and a calendar event. This fixture contains no real business information, customer
data, or copied email content.

## Checklist

- James consolidates action owners.
- Jeff reviews the priority list before the checkpoint.
- Ken receives the final decision summary.
""",
    "files/2001-06-04-operations-actions.csv": """synthetic_fixture,action,owner_email,due_date,status
true,Consolidate action owners,james.steffes@enron.com,2001-06-05,open
true,Review priority list,jeff.skilling@enron.com,2001-06-05,open
true,Receive decision summary,ken.lay@enron.com,2001-06-06,planned
""",
}

CALENDAR = f"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Igneous//Synthetic Cross-Source Corpus//EN
CALSCALE:GREGORIAN
X-WR-CALNAME:Igneous Synthetic Fixtures
X-IGNEOUS-SYNTHETIC:TRUE
BEGIN:VEVENT
UID:igneous-synthetic-forecast-review-20010515@example.invalid
DTSTAMP:20010501T000000Z
DTSTART:20010515T150000Z
DTEND:20010515T153000Z
SUMMARY:Synthetic forecast review
DESCRIPTION:{NOTICE}\\nReview the fictional forecast memo and assumptions.\\nRelated files: 2001-05-14-forecast-memo.md and 2001-05-14-forecast-assumptions.csv.
ORGANIZER;CN=Phillip Allen:mailto:phillip.allen@enron.com
ATTENDEE;CN=Tim Belden;ROLE=REQ-PARTICIPANT:mailto:tim.belden@enron.com
ATTENDEE;CN=John Arnold;ROLE=REQ-PARTICIPANT:mailto:john.arnold@enron.com
END:VEVENT
BEGIN:VEVENT
UID:igneous-synthetic-operations-checkpoint-20010605@example.invalid
DTSTAMP:20010501T000000Z
DTSTART:20010605T160000Z
DTEND:20010605T164500Z
SUMMARY:Synthetic operations checkpoint
DESCRIPTION:{NOTICE}\\nConfirm fictional handoff actions.\\nRelated file: 2001-06-04-operations-handoff.md.
ORGANIZER;CN=James Steffes:mailto:james.steffes@enron.com
ATTENDEE;CN=Jeff Skilling;ROLE=REQ-PARTICIPANT:mailto:jeff.skilling@enron.com
ATTENDEE;CN=Ken Lay;ROLE=REQ-PARTICIPANT:mailto:ken.lay@enron.com
END:VEVENT
END:VCALENDAR
"""


def _digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _readme() -> str:
    return f"""# Igneous synthetic cross-source corpus

{NOTICE}

This small fixture corpus is deterministic and safe to generate locally. It contains
five invented document-style files and two invented calendar events. Persona email
addresses align with the public Enron demo mailboxes used by Igneous, but no real
email text, customer data, credentials, or personal data is included.

The files are intentionally plain Markdown, CSV, and slide-outline text so a future
filesystem importer can exercise common document paths. `calendar.ics` is an RFC
5545-style iCalendar fixture for a future ICS importer. This generator does not
import anything or require a database, network connection, model, or credentials.

Regenerate with `uv run igneous synthetic-corpus <output-directory>`. Verify an
existing output with `uv run igneous verify-synthetic-corpus <output-directory>`.
"""


def _contents() -> dict[str, str]:
    return {"README.md": _readme(), **FILES, "calendar.ics": CALENDAR}


def generate(out: Path) -> dict:
    """Write the fixture corpus to *out* and return its stable manifest."""
    out = Path(out)
    contents = _contents()
    out.mkdir(parents=True, exist_ok=True)
    for relative, content in contents.items():
        path = out / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")

    manifest = {
        "format": 1,
        "name": "igneous-synthetic-cross-source-corpus",
        "synthetic": True,
        "notice": NOTICE,
        "document_count": len(FILES),
        "calendar_event_count": CALENDAR.count("BEGIN:VEVENT"),
        "files": {relative: _digest(content) for relative, content in sorted(contents.items())},
    }
    (out / MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )
    return manifest


def verify(out: Path) -> dict:
    """Validate a generated corpus without requiring an importer or external package."""
    out = Path(out)
    manifest_path = out / MANIFEST_NAME
    if not manifest_path.is_file():
        raise ValueError(f"missing {MANIFEST_NAME}: generate the corpus first")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected = _contents()
    if manifest.get("files") != {k: _digest(v) for k, v in sorted(expected.items())}:
        raise ValueError("manifest does not match this generator version")
    for relative, content in expected.items():
        path = out / relative
        if not path.is_file() or path.read_text(encoding="utf-8") != content:
            raise ValueError(f"invalid generated file: {relative}")
    calendar = out / "calendar.ics"
    lines = calendar.read_text(encoding="utf-8").splitlines()
    if lines[0] != "BEGIN:VCALENDAR" or lines[-1] != "END:VCALENDAR":
        raise ValueError("calendar.ics is not a complete VCALENDAR")
    if lines.count("BEGIN:VEVENT") != lines.count("END:VEVENT") or lines.count("BEGIN:VEVENT") != 2:
        raise ValueError("calendar.ics does not contain the expected events")
    return {"documents": manifest["document_count"], "calendar_events": manifest["calendar_event_count"]}
