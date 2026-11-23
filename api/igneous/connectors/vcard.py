"""Contacts exports in vCard (.vcf): Google Contacts' export, Apple Contacts' and iCloud's, and the Contacts folder of
a Google Takeout export. vCard 3.0 and 4.0, and 2.1 as older phones write it (contentlines.py reads all three).

One record per card (source `vcard`), shaped like the cards Google Contacts and Contacts.app sync (raw_meta emails,
phones, orgs), so the spine builds people from it the same way (graph/spine.py build_contact): every address and phone
number on a card is a person keyed as mail addresses and phone handles are (identity.card_keys: a number written any
way is one key), named by the card and tied to the card's other identifiers as one person (SAME_AS, 'contacts'). A
card Google Contacts or Contacts.app syncs later for the same person names the same people, so they stay one. A card
with a phone number and a name only gives a phone-keyed person that entity resolution joins to the same name's mail
identity (graph/resolve.py). A company's card (Apple's X-ABShowAs:COMPANY, or one named only by its organization)
names no one. The card's organization, title and postal addresses are in its text, which the reader shows; cards are
never chunked or searched.

A card is keyed by its UID (vCard's UID, or Apple's X-ABUID), else by its name and identifiers, so importing the same
export again changes nothing, and a card in Takeout's Contacts and in Google Contacts' own export is one record. An
import adds and updates cards and never removes one: an export doesn't say which address book it is, so a card that
isn't in it may come from another one you imported. A card with no phone number and no email address names no
identity, and is counted, not stored.
"""
import hashlib
import re
import zipfile
from collections.abc import Callable, Iterator
from pathlib import Path

from igneous.connectors import contentlines
from igneous.connectors.base import Checkpoint, SourceItem
from igneous.connectors.contentlines import Component, Prop
from igneous.connectors.mac_contacts import norm_phone


class VCardError(ValueError):
    """The input holds no contact cards; the message is safe to show to the user."""


def _phone(p: Prop) -> str:
    """A TEL value as written, without vCard 4's tel: scheme or an extension."""
    return re.sub(r"^tel:", "", p.text.strip(), flags=re.I).split(";ext=")[0].strip()


def _name(c: Component, org: str) -> str | None:
    """The person a card names: its formatted name (FN), else its structured name (N), or None for a card with neither
    or a company's card."""
    if c.text("X-ABSHOWAS").upper() == "COMPANY":
        return None
    n = c.first("N")
    family, given, additional, *_ = [*(contentlines.split(n.value) if n else []), "", "", ""]
    structured = " ".join(x.replace(",", " ").strip() for x in (given, additional, family) if x.strip())
    formatted = " ".join(c.text("FN").split())
    if formatted and not structured and formatted == org:
        return None  # named only by its organization: Google writes a company's contact that way
    return formatted or " ".join(structured.split()) or None


def _types(p: Prop) -> str:
    labels = sorted(p.types() - {"pref", "voice", "internet", "x400"})
    return f" ({', '.join(labels)})" if labels else ""


def card(c: Component) -> SourceItem | None:
    """One VCARD as a record, or None when it names no phone number or email address."""
    emails = list(dict.fromkeys(a for p in c.all("EMAIL") if (a := contentlines.address(p.value))))
    tels = [(p, _phone(p)) for p in c.all("TEL")]
    phones, seen = [], set()
    for _p, number in tels:
        key = norm_phone(number) or number
        if number and key not in seen:
            seen.add(key)
            phones.append(number)
    if not emails and not phones:
        return None
    orgs = [o for p in c.all("ORG") if (o := contentlines.split(p.value)[0].strip())]
    name = _name(c, orgs[0] if orgs else "")
    titles = [t for p in c.all("TITLE") if (t := p.text.strip())]
    addresses = []
    for p in c.all("ADR"):
        box, ext, street, city, region, code, country, *_ = [*contentlines.split(p.value), *[""] * 7]
        place = ", ".join(x for x in (street, ext, box, city, " ".join(y for y in (region, code) if y), country)
                          if x.strip())
        if place:
            addresses.append((place.replace("\n", ", "), _types(p)))
    lines = [name or "(no name)"]
    if orgs or titles:
        lines.append(", ".join(dict.fromkeys([*orgs[:1], *titles[:1]])))
    lines += [f"Phone{_types(p)}: {number}" for p, number in tels if number in phones]
    lines += [f"Email: {e}" for e in emails]
    lines += [f"Address{kind}: {place}" for place, kind in addresses]
    uid = c.text("UID") or c.text("X-ABUID")
    keys = sorted({*emails, *(norm_phone(p) or p for p in phones)})
    external_id = f"uid/{uid}" if uid else "card/" + hashlib.sha1(
        f"{(name or '').lower()}|{','.join(keys)}".encode()).hexdigest()[:20]
    return SourceItem(source="vcard", external_id=external_id, title=name, body_text="\n".join(dict.fromkeys(lines)),
                      mime="text/vcard",
                      raw_meta={"emails": emails, "phones": phones, "orgs": orgs, "titles": titles,
                                "addresses": [place for place, _ in addresses]})


def cards(data: bytes | str) -> tuple[list[SourceItem], int]:
    """The records of every card in one .vcf file, and how many cards named no phone number or email address."""
    out, skipped = [], 0
    for c in contentlines.parse(data):
        if c.name != "VCARD":
            continue
        item = card(c)
        if item is None:
            skipped += 1
        else:
            out.append(item)
    return out, skipped


def _files(paths: list[Path]) -> Iterator[tuple[str, bytes]]:
    """(name, bytes) of every .vcf among `paths`: files, folders (searched), and zips (an iCloud or Takeout download)."""
    for p in paths:
        p = Path(p).expanduser()
        if not p.exists():
            raise VCardError(f"{p} does not exist.")
        if p.is_dir():
            for f in sorted(p.rglob("*")):
                if f.is_file() and f.suffix.lower() == ".vcf":
                    yield f.name, f.read_bytes()
        elif zipfile.is_zipfile(p):
            with zipfile.ZipFile(p) as zf:
                for name in sorted(zf.namelist()):
                    if name.lower().endswith(".vcf") and not name.startswith("__MACOSX"):
                        yield name.rsplit("/", 1)[-1], zf.read(name)
        elif p.suffix.lower() in (".vcf", ".vcard"):
            yield p.name, p.read_bytes()
        else:
            raise VCardError(f"{p.name} is not a contacts export: expected a .vcf file, a folder of them, or a zip.")


def read(paths: list[Path]) -> tuple[list[SourceItem], dict]:
    """(records, report): every card of every file, each once (a card in two files, as Takeout writes one per label, is
    one record)."""
    found: dict[str, SourceItem] = {}
    skipped, files = 0, []
    for name, data in _files(paths):
        items, n = cards(data)
        files.append(name)
        skipped += n
        for item in items:
            found.setdefault(item.external_id, item)
    if not files:
        raise VCardError("No .vcf file found. Export your contacts as vCard and give that file.")
    items = list(found.values())
    if not items and not skipped:
        raise VCardError("No contact cards found in the .vcf file.")
    report = {"cards": len(items), "without_phone_or_email": skipped, "files": files,
              "phones": sum(len(i.raw_meta["phones"]) for i in items),
              "emails": sum(len(i.raw_meta["emails"]) for i in items)}
    return items, report


class VCardConnector:
    """Adapter for run_sync: reads every file first (a card can be in several), then emits."""
    name = "vcard"

    def __init__(self, paths: list[Path], progress: Callable[..., None] | None = None):
        self.paths, self.progress, self.report = paths, progress, {}

    def sync(self, cursor: dict) -> Iterator[SourceItem | Checkpoint]:
        items, self.report = read(self.paths)
        for n, item in enumerate(items, 1):
            if self.progress:
                self.progress({"items": n, "items_total": len(items)})
            yield item
        yield Checkpoint({"cards": len(items)})


def resolve_paths(path: str | list[str] | None) -> list[Path]:
    raw = [path] if isinstance(path, str) else list(path or [])
    paths = [Path(p).expanduser() for p in raw if p]
    if not paths:
        raise VCardError("Give the path to a .vcf file (or a folder of them), or upload it.")
    return paths


def preflight(paths: list[Path]) -> dict:
    """Cheap synchronous check: fail fast with a readable reason when there's nothing to read."""
    names = [name for name, _ in _files(paths)]
    if not names:
        raise VCardError("No .vcf file found. Export your contacts as vCard and give that file.")
    return {"files": names}


def run_import(s, paths: list[Path], progress: Callable[..., None] | None = None) -> dict:
    """Body of `igneous import vcard` and POST /import/vcard. Re-running on the same export changes nothing."""
    from igneous.connectors.base import run_sync

    conn = VCardConnector(paths, progress)
    stats = run_sync(s, conn, account="default")
    return {**conn.report, **stats}
