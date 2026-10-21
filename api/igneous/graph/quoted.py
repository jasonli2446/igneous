"""Recover REPLIES_TO from quoted headers. Enron has no In-Reply-To, but replies quote the original:

    -----Original Message-----
    From: Rice, Ken
    Sent: Mon 8/20/2001 10:20 AM
    Subject: ...

The quoted Sent time pins the parent to the minute; a subject or sender-name check confirms it. Precision
over recall: no match without both. Re-derived on every spine run (cheap: one indexed lookup per quote).
"""
import re
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from igneous.graph.resolve import same_first

ORIGINAL = re.compile(r"-+\s*Original Message\s*-+\s*\n(?P<hdr>(?:[^\n]*\n){1,8})", re.I)
FIELD = re.compile(r"^\s*(From|Sent|Date|Subject)\s*:\s*(.*)$", re.I | re.M)
PREFIX = re.compile(r"^\s*((re|fw|fwd)\s*:\s*)+", re.I)
SENT_FORMATS = ("%A, %B %d, %Y %I:%M %p", "%a %m/%d/%Y %I:%M %p", "%m/%d/%Y %I:%M %p", "%A, %B %d, %Y %H:%M")


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


FIND_PARENT = text("""
SELECT m.id, si.title, sender.name AS sender_name, si.occurred_at
FROM source_items si JOIN nodes m ON m.source_item_id = si.id AND m.type = 'Message'
LEFT JOIN edges snd ON snd.dst = m.id AND snd.type = 'SENT'
LEFT JOIN nodes sender ON sender.id = snd.src
WHERE si.occurred_at >= :t0 AND si.occurred_at < :t1 AND m.id <> :child
""")


def run(s: Session) -> int:
    """Link each reply to the message it quotes. Enron Sent: times are local US time with no zone, so search a
    window covering US zones (UTC-4..-8) at minute precision, then require a subject or sender match."""
    from datetime import timedelta

    rows = s.execute(text("""
        SELECT m.id, si.body_text, si.occurred_at FROM nodes m JOIN source_items si ON si.id = m.source_item_id
        WHERE m.type = 'Message' AND si.body_text ~* 'Original Message'
    """)).all()
    added = 0
    for child, body, occurred_at in rows:
        quotes = parse_quotes(body)
        if not quotes:
            continue
        q = quotes[0]  # the outermost quote is the direct parent
        # Interpret the naive local time as each plausible US offset; one SQL window covers them all.
        t0 = q["sent"] + timedelta(hours=4)
        t1 = q["sent"] + timedelta(hours=8, minutes=1)
        subj, surname = q["subject"].lower(), _surname(q["from"])
        for cand in s.execute(FIND_PARENT, {"t0": t0, "t1": t1, "child": child}).all():
            # A real timezone shift is a whole number of hours; anything else is a coincidence in the window.
            offset_min = (cand.occurred_at.replace(tzinfo=None) - q["sent"]).total_seconds() / 60
            if abs(offset_min - 60 * round(offset_min / 60)) > 1:
                continue
            c_subj = PREFIX.sub("", cand.title or "").strip().lower()
            subj_ok = bool(subj) and bool(c_subj) and subj == c_subj  # empty subjects prove nothing
            cand_name = (cand.sender_name or "").lower()
            cand_first = re.sub(r"[^a-z]", "", (cand_name.split(",", 1)[1] if "," in cand_name else cand_name).split()[0]
                                if cand_name.split() else "")
            first = _first(q["from"])
            # Surname must appear; if both first names are known they must agree (John Arnold != Matthew Arnold).
            sender_ok = (bool(surname) and surname in cand_name
                         and (not first or not cand_first or same_first(first, cand_first)))
            # Both known -> both must match. Only one known -> that one must match, and the minute already did.
            if (subj and surname and subj_ok and sender_ok) or (subj_ok and not surname) or (sender_ok and not subj
                                                                                               and not c_subj):
                s.execute(text("""
                    INSERT INTO edges (src, dst, type, valid_from, valid_to, extractor, confidence)
                    VALUES (:c, :p, 'REPLIES_TO', :at, :at, 'spine-quoted', :conf)
                    ON CONFLICT (src, dst, type, extractor) DO UPDATE SET
                        valid_from = COALESCE(edges.valid_from, EXCLUDED.valid_from),
                        valid_to = COALESCE(edges.valid_to, EXCLUDED.valid_to)
                """), {"c": child, "p": cand.id, "at": occurred_at,
                           "conf": 1.0 if subj_ok and sender_ok else 0.7})
                added += 1
                break
    s.commit()
    return added
