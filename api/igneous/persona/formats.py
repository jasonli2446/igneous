"""The persona's records written in the formats the real importers read, byte for byte the same for one seed.

  maildir/mira/{INBOX,Sent,Archive,Travel}/cur/...   one RFC 822 file per message (connectors/email_maildir.py)
  takeout-<date>-001.zip                            Google Takeout: Chat, Tasks, Meet, Maps (your places), Saved
                                                    lists, Location History, My Activity (Search, Maps, YouTube,
                                                    Gemini Apps), Chrome, and Calendar (connectors/takeout.py)
  Complete_LinkedInDataExport_<date>.zip            Profile, Positions, Education, Projects, Skills, Languages,
                                                    Connections (connectors/linkedin.py)
  chatgpt-export-<date>.zip                         conversations.json and the rest of OpenAI's export
                                                    (connectors/ai_exports.py)
  Documents/                                        notes, resumes, spreadsheets, slides and a git checkout
                                                    (connectors/filesystem.py, parsers.py)

Takeout's Calendar folder holds the calendar as Takeout writes it (one .ics file per calendar); no importer reads
ICS yet, so the calendar reaches the graph through the invitations in the mailbox and the Meet history.

Zip members carry fixed timestamps, Office files fixed document properties, and every file its record's date as its
modification time, so the same seed writes the same bytes.
"""
import csv
import hashlib
import io
import json
import os
import re
import zipfile
from datetime import UTC, date, datetime, timedelta
from email.message import EmailMessage
from email.utils import format_datetime, formataddr
from pathlib import Path

from igneous.persona.cast import EXPORTED, LAST_DAY, LINKEDIN_ONLY, ME, PEOPLE, PLACES, Person, local
from igneous.persona.life import Life, Mail, address_of, name_of
from igneous.persona.profile import EDUCATION, HEADLINE, LANGUAGES, POSITIONS, PROJECTS, SKILLS, SUMMARY

MAILBOX = "mira"
DOCUMENTS = "Documents"
STAMP = EXPORTED.strftime("%Y%m%dT%H%M%SZ")
TAKEOUT = f"takeout-{STAMP}-001.zip"
LINKEDIN = f"Complete_LinkedInDataExport_{EXPORTED:%m-%d-%Y}.zip"
CHATGPT = f"chatgpt-export-{EXPORTED:%Y-%m-%d}.zip"
TRAVEL_SENDERS = ("atlantica-air.example.com", "wanderstay.example.com", "nordlys-rental.example.com",
                  "lisboa-rail.example.com")


def _slug(s: str) -> str:
    """How connectors/takeout.py turns a folder or list name into an id."""
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "x"


def _iso(t: datetime, ms: bool = True) -> str:
    t = t.astimezone(UTC)
    return t.strftime("%Y-%m-%dT%H:%M:%S") + (f".{t.microsecond // 1000:03d}Z" if ms else "Z")


def _hash(*parts) -> str:
    return hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()


def zip_bytes(members: list[tuple[str, bytes]], when: datetime) -> bytes:
    """A zip whose every member carries `when` as its timestamp: the same members give the same bytes."""
    buf = io.BytesIO()
    stamp = local(when).timetuple()[:6]
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members:
            info = zipfile.ZipInfo(name, stamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            info.create_system = 3
            zf.writestr(info, data)
    return buf.getvalue()


def write_file(path: Path, data: bytes, when: datetime) -> None:
    """Write `data` with `when` as its modification time."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    ts = when.timestamp()
    os.utime(path, (ts, ts))


# ---- mail ----

def _folder(m: Mail, rng_value: float) -> str:
    address = address_of(m.sender)
    if address == ME.email:
        return "Sent"
    if address.rsplit("@", 1)[1] in TRAVEL_SENDERS:
        return "Travel"
    if m.at.date() >= LAST_DAY - timedelta(days=45) or rng_value < 0.2:
        return "INBOX"
    return "Archive"


def _addr(who) -> str:
    return formataddr((name_of(who), address_of(who)))


def message_bytes(m: Mail) -> bytes:
    msg = EmailMessage()
    msg["Return-Path"] = f"<{address_of(m.sender)}>"
    msg["Delivered-To"] = ME.email
    msg["Message-ID"] = m.message_id
    msg["Date"] = format_datetime(local(m.at))
    msg["From"] = _addr(m.sender)
    msg["To"] = ", ".join(_addr(x) for x in m.to)
    if m.cc:
        msg["Cc"] = ", ".join(_addr(x) for x in m.cc)
    msg["Subject"] = m.subject
    if m.in_reply_to:
        msg["In-Reply-To"] = m.in_reply_to
        msg["References"] = " ".join(m.references)
    for k, v in m.headers.items():
        msg[k] = v
    msg.set_content(m.body)
    if m.calendar:
        msg.add_alternative(m.calendar, subtype="calendar", params={"method": "REQUEST"})
    elif "List-Unsubscribe" in m.headers:
        paragraphs = "".join(f"<p>{line}</p>" for line in m.body.strip().split("\n\n"))
        msg.add_alternative(f"<html><body>{paragraphs}</body></html>", subtype="html")
    if msg.is_multipart():
        msg.set_boundary("=_" + _hash("boundary", m.message_id)[:24])  # the library's default is random
    return msg.as_bytes()


def write_maildir(life: Life, root: Path) -> dict[str, dict]:
    """Every message as a file under maildir/mira/<folder>/cur, named the way mail clients name them. Returns the
    tagged messages' keys: {tag: {source, external_id, fingerprint}}."""
    from igneous.connectors.email_maildir import parse_message

    keys = {}
    for m in sorted(life.mails, key=lambda x: (x.at, x.message_id)):
        h = _hash(m.message_id)
        m.folder = _folder(m, int(h[:8], 16) / 0xFFFFFFFF)
        raw = message_bytes(m)
        name = f"{int(m.at.timestamp())}.M{int(h[8:14], 16)}P{int(h[14:18], 16)}.mira-laptop,S={len(raw)}:2,S"
        rel = f"{MAILBOX}/{m.folder}/cur/{name}"
        write_file(root / rel, raw, m.at)
        if m.tag:
            item = parse_message(raw, external_id=rel)
            keys[m.tag] = {"source": "email", "external_id": rel, "fingerprint": item.raw_meta["fingerprint"]}
    return keys


# ---- Google Takeout ----

def _chat_date(t: datetime) -> str:
    """'Tuesday, March 11, 2025 at 8:30:12\u202fAM UTC', as Google Chat's export writes dates."""
    t = t.astimezone(UTC)
    hour = t.strftime("%I").lstrip("0") or "12"
    return f"{t:%A}, {t:%B} {t.day}, {t.year} at {hour}:{t:%M:%S}\u202f{t:%p} UTC"


def _member(p: Person) -> dict:
    return {"name": p.name, "email": p.email, "user_type": "Human"}


def takeout_chat(life: Life) -> tuple[list[tuple[str, bytes]], dict[str, dict]]:
    members, keys = [], {}
    for key, space in sorted(life.spaces.items()):
        lines = sorted((x for x in life.lines if x.space == key), key=lambda x: x.at)
        if not lines:
            continue
        info = {"members": [_member(x) for x in space.members]}
        if space.name:
            info = {"name": space.name, **info}
        group = f"Google Chat/Groups/{space.space_id}"
        msgs = []
        for i, x in enumerate(lines):
            topic = _hash(space.space_id, x.at.date())[:11]
            msgs.append({"creator": _member(x.sender), "created_date": _chat_date(x.at), "text": x.text,
                         "topic_id": topic, "message_id": f"{space.space_id.split()[-1]}/{topic}/{_hash(i, x.at)[:11]}"})
            if x.tag:
                keys[x.tag] = {"source": "takeout_chat",
                               "external_id": f"chat/{_slug(space.space_id)}/{x.at.astimezone(UTC).date()}"}
        members += [(f"{group}/group_info.json", _json(info)), (f"{group}/messages.json", _json({"messages": msgs}))]
    user = {"user": _member(ME), "membership_info": [
        {"group_name": s.name or "", "group_id": s.space_id.split()[-1], "membership_state": "MEMBER"}
        for _k, s in sorted(life.spaces.items())]}
    members.append((f"Google Chat/Users/User {int(_hash('gaia', ME.email)[:15], 16)}/user_info.json", _json(user)))
    return members, keys


def _json(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False, indent=2).encode()


def takeout_tasks(life: Life) -> tuple[bytes, dict[str, dict]]:
    lists, keys = [], {}
    for tl in life.task_lists:
        list_id = _hash("tasklist", tl.title)[:22].upper()
        updated = max([t.done or t.created for t in tl.tasks])
        items = []
        for t in tl.tasks:
            task = {"kind": "tasks#task", "id": _hash("task", tl.title, t.title)[:22], "title": t.title,
                    "updated": _iso(t.done or t.created), "position": f"{len(items):020d}",
                    "status": "completed" if t.done else "needsAction", "links": []}
            if t.due:
                task["due"] = f"{t.due.isoformat()}T00:00:00.000Z"
            if t.done:
                task["completed"] = _iso(t.done)
            if t.notes:
                task["notes"] = t.notes
            items.append(task)
        lists.append({"kind": "tasks#taskList", "id": list_id, "title": tl.title, "updated": _iso(updated),
                      "items": items})
        if tl.tag:
            keys[tl.tag] = {"source": "takeout_tasks", "external_id": f"tasks/{_slug(list_id)}"}
    return _json({"kind": "tasks#taskLists", "items": lists}), keys


def takeout_meet(life: Life) -> bytes:
    cols = ["Owner Gaia Id", "Item Id", "Conference Id", "Item Type", "Meeting Code", "Event Id", "Thread Id",
            "Inbox Id", "Call Direction", "Start Time", "End Time", "Duration", "Direct Call Result",
            "Participation State", "Call Counterparts", "Meeting Media Type"]
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(cols)
    owner = int(_hash("gaia", ME.email)[:15], 16)
    for m in sorted(life.meetings, key=lambda x: x.start):
        end = m.start + timedelta(minutes=m.minutes)
        h = _hash("meet", m.start)
        stamp = lambda t: t.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")  # noqa: E731
        call = not m.code
        w.writerow([owner, h[:20], h[20:44], "meet:call" if call else "meet:meeting", m.code, m.event_id, "", "",
                    "outgoing" if call else "", stamp(m.start), stamp(end),
                    f"{m.minutes // 60}:{m.minutes % 60:02d}:00", "completed" if call else "", "joined",
                    "[" + ", ".join(x.name for x in m.counterparts) + "]" if m.counterparts else "", "video"])
    return out.getvalue().encode()


def _ics_text(s: str) -> str:
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _fold(line: str) -> str:
    """RFC 5545 line folding: lines of at most 75 octets, continued with a space."""
    out, cur = [], ""
    for ch in line:
        if len((cur + ch).encode()) > 75:
            out.append(cur)
            cur = " " + ch
        else:
            cur += ch
    return "\r\n".join([*out, cur])


def calendar_ics(life: Life) -> bytes:
    stamp = lambda t: t.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")  # noqa: E731
    lines = ["BEGIN:VCALENDAR", "PRODID:-//Google Inc//Google Calendar 70.9054//EN", "VERSION:2.0",
             "CALSCALE:GREGORIAN", f"X-WR-CALNAME:{ME.email}", "X-WR-TIMEZONE:America/New_York"]
    for e in sorted(life.events, key=lambda x: (x.start, x.uid)):
        lines += ["BEGIN:VEVENT", f"DTSTART:{stamp(e.start)}", f"DTEND:{stamp(e.end)}"]
        if e.rrule:
            lines.append(f"RRULE:{e.rrule}")
        lines += [f"DTSTAMP:{stamp(EXPORTED)}", f"ORGANIZER;CN={e.organizer.name}:mailto:{e.organizer.email}",
                  f"UID:{e.uid}@google.com"]
        for a in e.attendees:
            lines.append(f"ATTENDEE;CUTYPE=INDIVIDUAL;ROLE=REQ-PARTICIPANT;PARTSTAT=ACCEPTED;CN={a.name}:mailto:{a.email}")
        lines += [f"CREATED:{stamp(e.start - timedelta(days=10))}", f"DESCRIPTION:{_ics_text(e.description)}",
                  f"LAST-MODIFIED:{stamp(e.start - timedelta(days=2))}", f"LOCATION:{_ics_text(e.location)}",
                  "SEQUENCE:0", "STATUS:CONFIRMED", f"SUMMARY:{_ics_text(e.summary)}", "TRANSP:OPAQUE",
                  "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return ("\r\n".join(_fold(x) for x in lines) + "\r\n").encode()


def takeout_maps(life: Life) -> list[tuple[str, bytes]]:
    members = []
    by_month: dict[tuple[int, int], list] = {}
    rng_seed = 0
    for v in sorted(life.visits, key=lambda x: x.start):
        pl = PLACES[v.place]
        rng_seed += 1
        visit = {"placeVisit": {
            "location": {"latitudeE7": round(pl.lat * 1e7), "longitudeE7": round(pl.lng * 1e7),
                         "placeId": pl.place_id, "address": pl.address, "name": pl.name,
                         "sourceInfo": {"deviceTag": 1194523409}, "locationConfidence": 80 + rng_seed % 19},
            "duration": {"startTimestamp": _iso(v.start), "endTimestamp": _iso(v.end)},
            "placeConfidence": "HIGH_CONFIDENCE", "visitConfidence": 90 + rng_seed % 9,
            "editConfirmationStatus": "NOT_CONFIRMED"}}
        month = by_month.setdefault((v.start.year, v.start.month), [])
        if month:
            month.append({"activitySegment": {"duration": {"startTimestamp": month[-1]["placeVisit"]["duration"]
                                                           ["endTimestamp"], "endTimestamp": _iso(v.start)},
                                              "activityType": "WALKING" if rng_seed % 3 else "IN_PASSENGER_VEHICLE",
                                              "confidence": "HIGH"}})
        month.append(visit)
    for (year, month), objects in sorted(by_month.items()):
        name = date(year, month, 1).strftime("%B").upper()
        members.append((f"Location History (Timeline)/Semantic Location History/{year}/{year}_{name}.json",
                        _json({"timelineObjects": objects})))
    for list_name, entries in sorted(life.saved.items()):
        out = io.StringIO()
        w = csv.writer(out, lineterminator="\n")
        w.writerow(["Title", "Note", "URL", "Tags", "Comment"])
        for key, note in entries:
            pl = PLACES[key]
            w.writerow([pl.name, note, f"https://www.google.com/maps/place/{pl.name.replace(' ', '+')}/data=!4m2!3m1"
                        f"!1s{pl.place_id}", "", ""])
        members.append((f"Saved/{list_name}.csv", out.getvalue().encode()))
    features = []
    for key, stars, text, when in sorted(life.reviews, key=lambda r: r[3]):
        pl = PLACES[key]
        features.append({"geometry": {"coordinates": [pl.lng, pl.lat], "type": "Point"}, "properties": {
            "Google Maps URL": f"http://maps.google.com/?cid={int(_hash(pl.place_id)[:16], 16)}",
            "Location": {"Address": pl.address, "Business Name": pl.name, "Country Code": "PT",
                         "Geo Coordinates": {"Latitude": f"{pl.lat}", "Longitude": f"{pl.lng}"}},
            "Published": _iso(when, ms=False), "Review Comment": text, "Star Rating": stars,
            "Updated": _iso(when, ms=False)}, "type": "Feature"})
    if features:
        members.append(("Maps (your places)/Reviews.json", _json({"type": "FeatureCollection", "features": features})))
    return members


def takeout_activity(life: Life) -> list[tuple[str, bytes]]:
    by_product: dict[str, list] = {}
    for s in life.searches:
        e = {"header": s.product, "title": s.title, "time": _iso(s.at), "products": [s.product],
             "activityControls": ["YouTube watch history" if s.product == "YouTube" else "Web & App Activity"]}
        if s.url:
            e["titleUrl"] = s.url
        if s.subtitle:
            e["subtitles"] = [{"name": s.subtitle, "url": "https://www.youtube.com/channel/UC" +
                               _hash("channel", s.subtitle)[:22]}]
        by_product.setdefault(s.product, []).append((s.at, e))
    for x in life.prompts:
        by_product.setdefault("Gemini Apps", []).append((x.at, {
            "header": "Gemini Apps", "title": f"Prompted {x.prompt}", "time": _iso(x.at), "products": ["Gemini Apps"],
            "activityControls": ["Gemini Apps Activity"], "safeHtmlItem": [{"html": x.reply}]}))
    # My Activity lists the newest first.
    return [(f"My Activity/{product}/MyActivity.json", _json([e for _t, e in sorted(rows, key=lambda r: r[0],
                                                                                   reverse=True)]))
            for product, rows in sorted(by_product.items())]


def takeout_chrome(life: Life) -> list[tuple[str, bytes]]:
    history = [{"favicon_url": "", "page_transition": "LINK", "title": x.title, "url": x.url,
                "client_id": "Pv0n3HQb2y8wZ3c1d5Q8Gw==", "time_usec": int(x.at.timestamp() * 1_000_000)}
               for x in sorted(life.pages, key=lambda x: x.at, reverse=True)]
    folders: dict[str, list] = {}
    for folder, title, url in life.bookmarks:
        folders.setdefault(folder, []).append((title, url))
    html = ["<!DOCTYPE NETSCAPE-Bookmark-file-1>", '<META HTTP-EQUIV="Content-Type" CONTENT="text/html; charset=UTF-8">',
            "<TITLE>Bookmarks</TITLE>", "<H1>Bookmarks</H1>", "<DL><p>", "<DT><H3>Bookmarks bar</H3>", "<DL><p>"]
    for folder, marks in folders.items():
        if folder:
            html += [f"<DT><H3>{folder}</H3>", "<DL><p>"]
        html += [f'<DT><A HREF="{url}">{title}</A>' for title, url in marks]
        if folder:
            html.append("</DL><p>")
    html += ["</DL><p>", "</DL><p>"]
    return [("Chrome/History.json", _json({"Browser History": history})),
            ("Chrome/Bookmarks.html", "\n".join(html).encode())]


def write_takeout(life: Life, path: Path) -> dict[str, dict]:
    chat, keys = takeout_chat(life)
    tasks, task_keys = takeout_tasks(life)
    keys.update(task_keys)
    members = [*chat, ("Tasks/Tasks.json", tasks),
               ("Google Meet/ConferenceHistory/conference_history_records.csv", takeout_meet(life)),
               (f"Calendar/{ME.email}.ics", calendar_ics(life)), *takeout_maps(life), *takeout_activity(life),
               *takeout_chrome(life)]
    index = "".join(f"<li>{name}</li>" for name in sorted({m[0].split('/')[0] for m in members}))
    members.insert(0, ("archive_browser.html", f"<html><body><h1>Your Google data</h1><ul>{index}</ul></body></html>"
                       .encode()))
    write_file(path, zip_bytes([(f"Takeout/{name}", data) for name, data in members], EXPORTED), EXPORTED)
    return keys


# ---- LinkedIn ----

def _csv(header: list[str], rows: list[list], preamble: str = "") -> bytes:
    out = io.StringIO()
    out.write(preamble)
    w = csv.writer(out, lineterminator="\n")
    w.writerow(header)
    w.writerows(rows)
    return out.getvalue().encode()


def _month(d: date | None) -> str:
    return f"{d:%b} {d.year}" if d else ""


CONNECTIONS_NOTE = ('Notes:\n"When exporting your connection data, you may notice that some of the email addresses '
                    "are missing. You will only see email addresses for connections who have allowed their "
                    'connections to see or download their email address using this setting."\n\n')


def write_linkedin(life: Life, path: Path, until: date | None) -> int:
    """The export as of `until` (default: the export date); returns how many connections it lists."""
    cut = until or EXPORTED.date()
    first, last = ME.name.split()
    files = [
        ("Profile.csv", _csv(["First Name", "Last Name", "Maiden Name", "Address", "Birth Date", "Headline", "Summary",
                              "Industry", "Zip Code", "Geo Location", "Twitter Handles", "Websites",
                              "Instant Messengers"],
                             [[first, last, "", "418 Linden Ave, Port Avalon", "Apr 14, 2004", HEADLINE, SUMMARY,
                               "Software Development", "00000", "Port Avalon", "", "[PORTFOLIO:code.example.org/"
                               "mira-c]", ""]])),
        ("Positions.csv", _csv(["Company Name", "Title", "Description", "Location", "Started On", "Finished On"],
                               [[c, t, desc, loc, _month(s), _month(e if e and e <= cut else None)]
                                for c, t, s, e, loc, desc in POSITIONS if s <= cut])),
        ("Education.csv", _csv(["School Name", "Start Date", "End Date", "Notes", "Degree Name", "Activities"],
                               [[school, str(s.year), str(e.year), notes, degree, "Lakeshore Climbing Club"]
                                for school, degree, s, e, notes in EDUCATION])),
        ("Projects.csv", _csv(["Title", "Description", "Url", "Started On", "Finished On"],
                              [[name, desc, url, _month(s), _month(e)] for name, desc, s, e, url in PROJECTS if s <= cut])),
        ("Skills.csv", _csv(["Name"], [[s] for s in SKILLS])),
        ("Languages.csv", _csv(["Name", "Proficiency"], [list(x) for x in LANGUAGES])),
        ("Email Addresses.csv", _csv(["Email Address", "Confirmed", "Primary", "Updated On"],
                                     [[ME.email, "Yes", "Yes", "9/1/22, 10:04 AM"]])),
    ]
    rows = []
    for p in sorted(PEOPLE.values(), key=lambda x: (x.connected or date.min, x.key)):
        if p.linkedin and p.connected and p.connected <= cut:
            rows.append([p.first, p.name.split(" ", 1)[1], f"https://www.linkedin.com/in/{p.linkedin}",
                         p.email if p.shares_email else "", p.company, p.position, f"{p.connected:%d %b %Y}"])
    for first_name, last_name, slug, company, position, connected in LINKEDIN_ONLY:
        if connected <= cut:
            rows.append([first_name, last_name, f"https://www.linkedin.com/in/{slug}", "", company, position,
                         f"{connected:%d %b %Y}"])
    rows.sort(key=lambda r: datetime.strptime(r[-1], "%d %b %Y"), reverse=True)
    files.append(("Connections.csv", _csv(["First Name", "Last Name", "URL", "Email Address", "Company", "Position",
                                           "Connected On"], rows, CONNECTIONS_NOTE)))
    write_file(path, zip_bytes(files, EXPORTED), EXPORTED)
    return len(rows)


# ---- ChatGPT ----

def _uuid(*parts) -> str:
    h = _hash(*parts)
    return f"{h[:8]}-{h[8:12]}-4{h[13:16]}-a{h[17:20]}-{h[20:32]}"


def write_chatgpt(life: Life, path: Path) -> dict[str, dict]:
    convs, keys = [], {}
    for c in sorted(life.conversations, key=lambda x: x.turns[0][0]):
        cid = _uuid("conversation", c.title, c.turns[0][0])
        c.conversation_id = cid
        root, system = _uuid(cid, "root"), _uuid(cid, "system")
        mapping = {root: {"id": root, "message": None, "parent": None, "children": [system]},
                   system: {"id": system, "message": {
                       "id": system, "author": {"role": "system", "name": None, "metadata": {}}, "create_time": None,
                       "update_time": None, "content": {"content_type": "text", "parts": [""]}, "status":
                       "finished_successfully", "end_turn": True, "weight": 0.0,
                       "metadata": {"is_visually_hidden_from_conversation": True}, "recipient": "all"},
                       "parent": root, "children": []}}
        parent = system
        for i, (when, role, text) in enumerate(c.turns):
            node = _uuid(cid, i)
            mapping[parent]["children"] = [node]
            mapping[node] = {"id": node, "message": {
                "id": node, "author": {"role": role, "name": None, "metadata": {}},
                "create_time": round(when.timestamp(), 3), "update_time": None,
                "content": {"content_type": "text", "parts": [text]}, "status": "finished_successfully",
                "end_turn": role == "assistant", "weight": 1.0, "metadata": {}, "recipient": "all"},
                "parent": parent, "children": []}
            parent = node
        convs.append({"title": c.title, "create_time": round(c.turns[0][0].timestamp(), 3),
                      "update_time": round(c.turns[-1][0].timestamp(), 3), "mapping": mapping,
                      "moderation_results": [], "current_node": parent, "plugin_ids": None, "conversation_id": cid,
                      "conversation_template_id": None, "gizmo_id": None, "is_archived": False, "safe_urls": [],
                      "id": cid})
        if c.tag:
            keys[c.tag] = {"source": "ai_chats",
                           "external_id": f"chatgpt/{cid}/{c.turns[0][0].astimezone(UTC).date()}"}
    convs.sort(key=lambda x: x["update_time"], reverse=True)  # the export lists the most recent first
    html = "<html><head><title>ChatGPT Data Export</title></head><body>" + "".join(
        f"<h4>{c['title']}</h4>" for c in convs) + "</body></html>"
    user = {"id": "user-" + _hash("user", ME.email)[:24], "email": ME.email, "chatgpt_plus_user": False,
            "phone_number": None}
    members = [("conversations.json", _json(convs)), ("user.json", _json(user)),
               ("message_feedback.json", b"[]"), ("shared_conversations.json", b"[]"), ("chat.html", html.encode())]
    write_file(path, zip_bytes(members, EXPORTED), EXPORTED)
    return keys


# ---- documents ----

# The created and modified dates in an Office file's properties: the opening tag (with the namespaces it may
# declare), the date, the closing tag.
FIXED_PROPS = re.compile(rb"(<dcterms:(created|modified)\b[^>]*>)[^<]*(</dcterms:\2>)")


def _office(data: bytes, when: datetime) -> bytes:
    """An Office file re-zipped with fixed member timestamps and its created/modified properties set to `when`."""
    stamp = _iso(when, ms=False).encode()
    members = []
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for info in zf.infolist():
            blob = zf.read(info)
            if info.filename == "docProps/core.xml":
                blob = FIXED_PROPS.sub(lambda m: m.group(1) + stamp + m.group(3), blob)
            members.append((info.filename, blob))
    return zip_bytes(members, when)


def _docx(paragraphs: list[tuple[str, str | None]], when: datetime) -> bytes:
    from docx import Document

    doc = Document()
    doc.core_properties.author = ME.name
    doc.core_properties.last_modified_by = ME.name
    for text, style in paragraphs:
        doc.add_paragraph(text, style=style)
    buf = io.BytesIO()
    doc.save(buf)
    return _office(buf.getvalue(), when)


def _xlsx(sheets: dict[str, list[list]], when: datetime) -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(name)
        for row in rows:
            ws.append(row)
    wb.properties.creator = ME.name
    wb.properties.created = wb.properties.modified = when.replace(tzinfo=None)
    buf = io.BytesIO()
    wb.save(buf)
    return _office(buf.getvalue(), when)


def _pptx(slides: list[tuple[str, list[str]]], when: datetime) -> bytes:
    from pptx import Presentation

    prs = Presentation()
    for title, bullets in slides:
        slide = prs.slides.add_slide(prs.slide_layouts[1])
        slide.shapes.title.text = title
        body = slide.placeholders[1].text_frame
        body.text = bullets[0] if bullets else ""
        for line in bullets[1:]:
            body.add_paragraph().text = line
    prs.core_properties.author = ME.name
    buf = io.BytesIO()
    prs.save(buf)
    return _office(buf.getvalue(), when)


def pdf(pages: list[list[str]]) -> bytes:
    """A small text-only PDF (Helvetica, one line per row), enough for any PDF reader to extract the text."""
    def esc(s: str) -> str:
        return s.encode("latin-1", "replace").decode("latin-1").replace("\\", "\\\\").replace("(", "\\(") \
            .replace(")", "\\)")
    objects = ["<< /Type /Catalog /Pages 2 0 R >>", None, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
               "/Encoding /WinAnsiEncoding >>"]
    kids = []
    for lines in pages:
        stream = "BT /F1 11 Tf 14 TL 56 760 Td " + " ".join(f"({esc(line)}) Tj T*" for line in lines) + " ET"
        objects.append(f"<< /Length {len(stream.encode('latin-1'))} >>\nstream\n{stream}\nendstream")
        objects.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> "
                       f"/Contents {len(objects)} 0 R >>")
        kids.append(f"{len(objects)} 0 R")
    objects[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>"
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{obj}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{o:010d} 00000 n \n" for o in offsets).encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def document_bytes(path: str, body, when: datetime) -> bytes:
    suffix = Path(path).suffix.lower()
    if suffix == ".docx":
        return _docx(body, when)
    if suffix == ".xlsx":
        return _xlsx(body, when)
    if suffix == ".pptx":
        return _pptx(body, when)
    if suffix == ".pdf":
        return pdf(body)
    if isinstance(body, bytes):
        return body
    return str(body).encode()


def write_documents(life: Life, root: Path) -> dict[str, dict]:
    keys = {}
    for doc in sorted(life.docs, key=lambda x: x.path):
        write_file(root / doc.path, document_bytes(doc.path, doc.body, doc.modified), doc.modified)
        if doc.tag:
            keys[doc.tag] = {"source": "fs", "path": doc.path}
    return keys
