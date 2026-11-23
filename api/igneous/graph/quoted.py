"""Recover REPLIES_TO from quoted headers. Enron has no In-Reply-To, but replies quote the original:

    -----Original Message-----
    From: Rice, Ken
    Sent: Mon 8/20/2001 10:20 AM
    Subject: ...

The quoted Sent time pins the parent to the minute; a subject or sender-name check confirms it. Precision over
recall: no link without both, when both are known, and none when two different messages match equally well.

Incremental: the spine stores each message's outermost quote in `spine_quotes` (`store`) and calls `run` for the
messages whose quote or candidate parents changed: those it just built, and those whose quoted time window covers a
message it just built or deleted (`quoting`). `run` replaces each one's previous link.
"""
import re
from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

from igneous.graph.resolve import same_first

EXTRACTOR = "spine-quoted"
ORIGINAL = re.compile(r"-+\s*Original Message\s*-+\s*\n(?P<hdr>(?:[^\n]*\n){1,8})", re.I)
FIELD = re.compile(r"^\s*(From|Sent|Date|Subject)\s*:\s*(.*)$", re.I | re.M)
PREFIX = re.compile(r"^\s*((re|fw|fwd)\s*:\s*)+", re.I)
SENT_FORMATS = ("%A, %B %d, %Y %I:%M %p", "%a %m/%d/%Y %I:%M %p", "%m/%d/%Y %I:%M %p", "%A, %B %d, %Y %H:%M")
# Enron Sent: times are local US time with no zone: the parent's UTC time is 4 to 8 hours later (EDT to PST).
EARLIEST, LATEST = timedelta(hours=4), timedelta(hours=8, minutes=1)


def parse_quotes(body: str) -> list[dict]:
    """Each quoted 'Original Message' header as {from, sent (naive local datetime), subject}, outermost first."""
    out = []
    for m in ORIGINAL.finditer(body or ""):
        f = {k.lower(): v.strip() for k, v in FIELD.findall(m.group("hdr"))}
        sent_raw = re.sub(r"\s+", " ", f.get("sent") or f.get("date") or "")
        sent = None
        for fmt in SENT_FORMATS:
            try:
                sent = datetime.strptime(sent_raw, fmt)
                break
            except ValueError:
                continue
        if sent:
            out.append({"from": f.get("from", ""), "sent": sent, "subject": PREFIX.sub("", f.get("subject", "")).strip()})
    return out


def _first(from_field: str) -> str:
    """'Rice, Ken' -> ken; 'Brad Schaeffer <x@y>' -> brad. Empty when the field has only one name."""
    f = re.sub(r"<.*?>|\[.*?\]|\".*?\"|@ENRON", "", from_field).strip()
    parts = f.split(",", 1)[1].split() if "," in f else f.split()[:-1]
    return re.sub(r"[^a-z]", "", parts[0].lower()) if parts else ""


def _surname(from_field: str) -> str:
    """'Rice, Ken' -> rice; 'Brad Schaeffer <x@y>' -> schaeffer."""
    f = re.sub(r"<.*?>|\[.*?\]|\".*?\"|@ENRON", "", from_field).strip()
    if "," in f:
        return f.split(",")[0].strip().lower()
    parts = f.split()
    return parts[-1].lower() if parts else ""


def store(s: Session, bodies: dict[int, str | None]) -> None:
    """Record the outermost quote of each Message node's body (or that it quotes nothing)."""
    rows, none = [], []
    for node, body in bodies.items():
        quotes = parse_quotes(body or "")
        if quotes:  # the outermost quote is the direct parent
            q = quotes[0]
            rows.append({"n": node, "t": q["sent"], "f": q["from"], "s": q["subject"]})
        else:
            none.append(node)
    if none:
        s.execute(text("DELETE FROM spine_quotes WHERE node_id = ANY(:ids)"), {"ids": none})
    if rows:
        s.execute(text("""
            INSERT INTO spine_quotes (node_id, sent, sender, subject) VALUES (:n, :t, :f, :s)
            ON CONFLICT (node_id) DO UPDATE SET sent = EXCLUDED.sent, sender = EXCLUDED.sender, subject = EXCLUDED.subject
        """), rows)


def quoting(s: Session, times: Iterable[datetime]) -> set[int]:
    """Messages whose quoted Sent time could name a message at one of these (UTC) times."""
    local = sorted({t.astimezone(UTC).replace(tzinfo=None) for t in times if t is not None})
    if not local:
        return set()
    return set(s.execute(text("""
        SELECT DISTINCT q.node_id FROM unnest(CAST(:t AS timestamp[])) AS t(at)
        JOIN spine_quotes q ON q.sent > t.at - CAST(:latest AS interval) AND q.sent <= t.at - CAST(:earliest AS interval)
    """), {"t": local, "latest": LATEST, "earliest": EARLIEST}).scalars())


# Each quote's window is one range scan of source_items(occurred_at) (OFFSET 0 keeps the planner from flattening it
# into a join of every message with every quote); the senders' names come along.
CANDIDATES = text("""
SELECT c.child, m.id, si.title, si.occurred_at,
       ARRAY(SELECT sender.name FROM edges snd JOIN nodes sender ON sender.id = snd.src
             WHERE snd.dst = m.id AND snd.type = 'SENT' ORDER BY sender.name) AS senders
FROM unnest(CAST(:child AS bigint[]), CAST(:t0 AS timestamptz[]), CAST(:t1 AS timestamptz[]),
            CAST(:before AS timestamptz[])) AS c(child, t0, t1, before)
CROSS JOIN LATERAL (SELECT si.id, si.title, si.occurred_at FROM source_items si
                    WHERE si.occurred_at >= c.t0 AND si.occurred_at < LEAST(c.t1, c.before) OFFSET 0) si
JOIN spine_node_support ns ON ns.source_item_id = si.id AND ns.represents
JOIN nodes m ON m.id = ns.node_id AND m.type = 'Message' AND m.id <> c.child
""")


def _confidence(q: dict, cand) -> float | None:
    """1.0 when subject and sender both match, 0.7 when the one that is known does; None when it isn't the parent."""
    # A real timezone shift is a whole number of hours; anything else is a coincidence in the window.
    offset_min = (cand.occurred_at.astimezone(UTC).replace(tzinfo=None) - q["sent"]).total_seconds() / 60
    if abs(offset_min - 60 * round(offset_min / 60)) > 1:
        return None
    subj, surname, first = q["subject"].lower(), _surname(q["sender"]), _first(q["sender"])
    c_subj = PREFIX.sub("", cand.title or "").strip().lower()
    subj_ok = bool(subj) and bool(c_subj) and subj == c_subj  # empty subjects prove nothing
    sender_ok = False
    for name in (n.lower() for n in cand.senders):
        words = (name.split(",", 1)[1] if "," in name else name).split()
        cand_first = re.sub(r"[^a-z]", "", words[0]) if words else ""
        # Surname must appear; if both first names are known they must agree (John Arnold != Matthew Arnold).
        if surname and surname in name and (not first or not cand_first or same_first(first, cand_first)):
            sender_ok = True
            break
    # Both known -> both must match. Only one known -> that one must match, and the minute already did.
    if (subj and surname and subj_ok and sender_ok) or (subj_ok and not surname) or (sender_ok and not subj
                                                                                       and not c_subj):
        return 1.0 if subj_ok and sender_ok else 0.7
    return None


def run(s: Session, children: Iterable[int]) -> set[int]:
    """Link each of these messages to the one message its outermost quote names, replacing any earlier link. A
    quote two messages match equally well links nothing. Returns the messages whose link changed."""
    ids = sorted(set(children))
    if not ids:
        return set()
    quotes = {r.node_id: {"sent": r.sent, "sender": r.sender, "subject": r.subject} for r in s.execute(text(
        "SELECT node_id, sent, sender, subject FROM spine_quotes WHERE node_id = ANY(:ids)"), {"ids": ids})}
    when = dict(s.execute(text("""
        SELECT ns.node_id, min(si.occurred_at) FROM spine_node_support ns JOIN source_items si ON si.id = ns.source_item_id
        WHERE ns.node_id = ANY(:ids) AND ns.represents GROUP BY ns.node_id"""), {"ids": ids}).all())
    asked = [c for c in ids if c in quotes]
    found: dict[int, dict[int, float]] = defaultdict(dict)
    if asked:
        sent = [quotes[c]["sent"].replace(tzinfo=UTC) for c in asked]
        for cand in s.execute(CANDIDATES, {
                "child": asked, "t0": [t + EARLIEST for t in sent], "t1": [t + LATEST for t in sent],
                "before": [when.get(c) or datetime.max.replace(tzinfo=UTC) for c in asked]}):
            conf = _confidence(quotes[cand.child], cand)
            if conf is not None:
                found[cand.child][cand.id] = max(conf, found[cand.child].get(cand.id, 0.0))
    want: dict[int, tuple[int, float]] = {}
    for child, matches in found.items():
        best = max(matches.values())
        winners = [m for m, conf in matches.items() if conf == best]
        if len(winners) == 1:
            want[child] = (winners[0], round(best, 3))
    have = {r.src: (r.dst, round(r.confidence, 3)) for r in s.execute(text("""
        SELECT src, dst, confidence FROM edges WHERE src = ANY(:ids) AND type = 'REPLIES_TO' AND extractor = :ex
    """), {"ids": ids, "ex": EXTRACTOR})}
    extra = s.execute(text("""
        SELECT src FROM edges WHERE src = ANY(:ids) AND type = 'REPLIES_TO' AND extractor = :ex
        GROUP BY src HAVING count(*) > 1"""), {"ids": ids, "ex": EXTRACTOR}).scalars().all()
    changed = {c for c in ids if have.get(c) != want.get(c)} | set(extra)
    if changed:
        s.execute(text("DELETE FROM edges WHERE src = ANY(:ids) AND type = 'REPLIES_TO' AND extractor = :ex"),
                  {"ids": sorted(changed), "ex": EXTRACTOR})
        rows = [{"c": c, "p": want[c][0], "conf": want[c][1], "at": when.get(c)} for c in sorted(changed) if c in want]
        if rows:
            s.execute(text("""
                INSERT INTO edges (src, dst, type, valid_from, valid_to, extractor, confidence)
                VALUES (:c, :p, 'REPLIES_TO', :at, :at, :ex, :conf)
            """), [{**r, "ex": EXTRACTOR} for r in rows])
    return changed
