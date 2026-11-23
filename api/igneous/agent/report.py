"""The briefing: one schema-constrained model call over the evidence ledger, then every citation checked against it.

The writer sees the question, the records the agent read (as much of each as it saw, under an id like [i123]), the
people and things the tools named, and the agent's notes: all of it came through igneous/disclosure.py, so the prompt
follows the rule every prompt follows. Each statement it writes cites record ids. `check` keeps a citation only when it
names a record the writer was shown, which is a record the agent read, and drops a statement left with none: every
citation in a briefing is valid by construction, and nothing in it is uncited.
"""
from igneous import llm
from igneous.agent.tools import Ledger, Read

PER_RECORD = 4000  # characters of one record the writer sees
SHOWN = 120_000  # characters of records in all; records read after that are left out of the prompt, and uncitable

SYSTEM = """You write a briefing for the person whose records these are, answering what they asked, from the records
below and nothing else. Each record has an id like [i123].

- summary: two to four sentences that answer the question.
- people: who is involved and the part each plays, as the records show it. Give the node_id the list of people and
  things gives them, or null.
- timeline: what happened when, oldest first, one event per entry, dated YYYY-MM-DD (YYYY-MM or YYYY when that is all
  the records say).
- open_items: what is still open as of the latest record: promises not yet kept, decisions or answers pending, next
  steps; who owes each and when it is due, when the records say.

Every entry cites the ids of the records that show it, as numbers (cites: [123]). Leave out what no record shows, and
add nothing from what you know otherwise. Call the person whose records these are "you". An empty section is better
than a padded one."""

CITES = {"type": "array", "items": {"type": "integer"}, "description": "ids of the records that show it"}
NULLABLE = {"type": ["string", "null"]}


def _entries(**fields) -> dict:
    fields["cites"] = CITES
    return {"type": "array", "items": {"type": "object", "properties": fields, "required": list(fields),
                                       "additionalProperties": False}}


SCHEMA = {
    "type": "object",
    "properties": {
        "summary": _entries(text={"type": "string"}),
        "people": _entries(node_id={"type": ["integer", "null"]}, name={"type": "string"}, role={"type": "string"}),
        "timeline": _entries(date={"type": "string"}, event={"type": "string"}),
        "open_items": _entries(item={"type": "string"}, owner=NULLABLE, due=NULLABLE),
    },
    "required": ["summary", "people", "timeline", "open_items"],
    "additionalProperties": False,
}
SECTIONS = {"summary": "text", "people": "role", "timeline": "event", "open_items": "item"}  # each entry's statement
# What `check` counts: statements written, kept, dropped for want of a valid citation; citations naming records not
# shown to the writer.
COUNTS = ("statements", "kept", "dropped", "invalid_citations")


def shown(ledger: Ledger) -> list[Read]:
    """The records the writer sees, in the order they were read, until SHOWN characters."""
    out, size = [], 0
    for read in ledger.items.values():
        size += min(sum(len(t) for t in read.texts), PER_RECORD)
        if size > SHOWN:
            break
        out.append(read)
    return out


def _record(read: Read) -> str:
    head = ", ".join(x for x in (read.source, read.date) if x)
    return f"[i{read.item_id}] ({head}) {read.title or ''}".rstrip() + "\n" + "\n...\n".join(read.texts)[:PER_RECORD]


def prompt(question: str, records: list[Read], nodes: dict[int, dict], notes: str) -> str:
    parts = [f"Question: {question}", "Records read:\n\n" + "\n\n---\n\n".join(_record(r) for r in records)]
    if nodes:
        parts.append("People and things named:\n" + "\n".join(
            f"node_id {i}: {n['name']}" + (f" ({n['type']})" if n.get("type") else "") for i, n in nodes.items()))
    if notes.strip():
        parts.append(f"Research notes:\n{notes.strip()}")
    return "\n\n".join(parts)


def check(raw: dict, records: list[Read], nodes: dict[int, dict], read: int) -> dict:
    """The briefing `raw` with every citation checked: a citation stands only when it names one of `records` (the ones
    the writer saw, all read by the agent), a statement left without one is dropped, and a person's node_id stands only
    when a tool named that node (it then goes by that name). Adds `sources` (the cited records, in the order first
    cited) and `checked`: what was kept and dropped, of `read` records the agent read."""
    by_id = {r.item_id: r for r in records}
    cited: list[int] = []
    counts = dict.fromkeys(COUNTS, 0)
    out: dict[str, list] = {}
    for section, statement in SECTIONS.items():
        kept, seen = [], set()
        for entry in raw.get(section) or []:
            if not isinstance(entry, dict) or not str(entry.get(statement) or "").strip():
                continue
            counts["statements"] += 1
            given = list(dict.fromkeys(c for c in entry.get("cites") or [] if isinstance(c, int)))
            valid = [c for c in given if c in by_id]
            counts["invalid_citations"] += len(given) - len(valid)
            if not valid:
                counts["dropped"] += 1
                continue
            row = {k: (v.strip() if isinstance(v, str) else v) for k, v in entry.items() if k != "cites"}
            if section == "people":
                node = row.get("node_id")
                row["node_id"] = node if isinstance(node, int) and node in nodes else None
                if row["node_id"] is not None:
                    row["name"] = nodes[node]["name"]
                who = row["node_id"] or str(row.get("name") or "").casefold()
                if who in seen:
                    continue
                seen.add(who)
            kept.append({**row, "cites": valid})
            cited += [c for c in valid if c not in cited]
        if section == "timeline":
            kept.sort(key=lambda e: str(e.get("date") or "9999"))
        counts["kept"] += len(kept)
        out[section] = kept
    out["sources"] = [{"item_id": i, "source": by_id[i].source, "title": by_id[i].title, "date": by_id[i].date}
                      for i in cited]
    out["checked"] = {"records_read": read, "records_cited": len(cited), **counts}
    return out


def empty(read: int) -> dict:
    """The briefing when the agent read no record: nothing to cite, so nothing to say."""
    return {**{section: [] for section in SECTIONS}, "sources": [],
            "checked": {"records_read": read, "records_cited": 0, **dict.fromkeys(COUNTS, 0)}}


def write(question: str, ledger: Ledger, notes: str = "") -> tuple[dict, dict]:
    """The briefing for `question` from what the agent read, and the model's usage ({} when no model was called: with
    no record read there is nothing to write from). Raises the providers' errors."""
    records = shown(ledger)
    if not records:
        return empty(len(ledger.items)), {}
    raw, usage = llm.structured(SYSTEM, prompt(question, records, ledger.nodes, notes), SCHEMA, max_tokens=16000,
                                effort="medium")
    return check(raw, records, ledger.nodes, len(ledger.items)), usage


def as_text(report: dict) -> str:
    """The briefing as plain text, each statement followed by the ids it cites."""
    def ids(entry: dict) -> str:
        return " [" + ", ".join(f"i{c}" for c in entry["cites"]) + "]"

    lines: list[str] = []
    if report["summary"]:
        lines += ["Summary"] + [f"  {e['text']}{ids(e)}" for e in report["summary"]]
    if report["people"]:
        lines += ["People"] + [f"  {e['name']}: {e['role']}{ids(e)}" for e in report["people"]]
    if report["timeline"]:
        lines += ["Timeline"] + [f"  {e['date']}  {e['event']}{ids(e)}" for e in report["timeline"]]
    if report["open_items"]:
        lines.append("Open items")
        for e in report["open_items"]:
            extra = ", ".join(x for x in (e.get("owner"), f"due {e['due']}" if e.get("due") else None) if x)
            lines.append(f"  - {e['item']}" + (f" ({extra})" if extra else "") + ids(e))
    if report["sources"]:
        lines += ["Sources"] + [f"  [i{s['item_id']}] {s['source']} {s['date'] or ''} {s['title'] or ''}".rstrip()
                                for s in report["sources"]]
    c = report["checked"]
    lines.append(f"{c['kept']} statements, every one citing records the research read ({c['records_cited']} of "
                 f"{c['records_read']} read); dropped {c['dropped']} without a valid citation and "
                 f"{c['invalid_citations']} citations of records it didn't read.")
    return "\n".join(lines)
