"""The text format iCalendar (RFC 5545) and vCard (RFC 2426, RFC 6350) share: content lines grouped into components.

A content line is `[group.]NAME;PARAM=value;...:value`, folded across physical lines that continue with a space or
a tab. Components nest between `BEGIN:X` and `END:X`. Values keep their escapes until a reader asks for text
(`Prop.text`, `split`), because which characters separate a structured value (N, ADR, ORG) depends on the property.

Read defensively, as exports from many programs are: bytes are UTF-8 (a byte that isn't is replaced), a line that
isn't a content line is skipped, a component left open is closed at the end of the file, and vCard 2.1's bare
parameters (`TEL;CELL:`) and quoted-printable values (with their `=` soft line breaks) are understood.
"""
import quopri
import re
from dataclasses import dataclass, field

NAME = re.compile(r"[A-Za-z0-9-]+")
ADDRESS = re.compile(r"[^@\s/]+@[^@\s/]+\.[^@\s/]+")


def address(value: str | None) -> str | None:
    """An email address as both formats write one ('mailto:Jane@Example.com' or 'Jane@Example.com'), lower-case, or
    None for anything that isn't one."""
    v = re.sub(r"^mailto:", "", (value or "").strip(), flags=re.I).strip().lower()
    return v if ADDRESS.fullmatch(v) else None


@dataclass
class Prop:
    name: str                                   # upper-case, without its group: TEL in item1.TEL
    params: dict[str, list[str]] = field(default_factory=dict)  # upper-case names -> values, quotes removed
    value: str = ""                             # as written, escapes kept
    group: str = ""                             # item1 in item1.TEL (Apple groups a value with its label)

    def param(self, name: str) -> str | None:
        values = self.params.get(name.upper())
        return values[0] if values else None

    def types(self) -> set[str]:
        """The TYPE values, lower-case: {'cell', 'pref'} for TEL;TYPE=CELL,pref and for vCard 2.1's TEL;CELL;PREF."""
        return {t.strip().lower() for v in self.params.get("TYPE", []) for t in v.split(",") if t.strip()}

    @property
    def text(self) -> str:
        """The value as text: escapes undone."""
        return unescape(self.value)


@dataclass
class Component:
    name: str
    props: list[Prop] = field(default_factory=list)
    children: list["Component"] = field(default_factory=list)

    def all(self, name: str) -> list[Prop]:
        return [p for p in self.props if p.name == name]

    def first(self, name: str) -> Prop | None:
        return next((p for p in self.props if p.name == name), None)

    def text(self, name: str) -> str:
        p = self.first(name)
        return p.text.strip() if p else ""


def unescape(value: str) -> str:
    """\\n and \\N are a line break; \\, \\; and \\\\ the character itself."""
    return re.sub(r"\\(.)", lambda m: "\n" if m.group(1) in "nN" else m.group(1), value)


def split(value: str, sep: str = ";") -> list[str]:
    """A structured value's parts, split where `sep` isn't escaped, each unescaped (N, ADR, ORG; categories by ',')."""
    parts, cur, i = [], [], 0
    while i < len(value):
        ch = value[i]
        if ch == "\\" and i + 1 < len(value):
            cur.append(value[i:i + 2])
            i += 2
            continue
        if ch == sep:
            parts.append(unescape("".join(cur)))
            cur = []
        else:
            cur.append(ch)
        i += 1
    parts.append(unescape("".join(cur)))
    return parts


def _quoted_printable(head: str) -> bool:
    return "QUOTED-PRINTABLE" in head.upper()


def unfold(data: bytes | str) -> list[str]:
    """The logical lines: folds joined, and vCard 2.1's quoted-printable soft breaks (a line ending in '=')."""
    text = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else data
    text = text.removeprefix("﻿")
    out: list[str] = []
    for raw in re.split(r"\r\n|\r|\n", text):
        if raw[:1] in (" ", "\t") and out:
            out[-1] += raw[1:]
        elif out and out[-1].endswith("=") and _quoted_printable(out[-1].split(":", 1)[0]):
            out[-1] = out[-1][:-1] + raw
        elif raw.strip():
            out.append(raw)
    return out


def _params(text: str) -> dict[str, list[str]]:
    """`TYPE=cell,pref;CN="Lee, Jordan";PREF` -> {'TYPE': ['cell,pref', 'pref'], 'CN': ['Lee, Jordan']}."""
    out: dict[str, list[str]] = {}
    for part in re.findall(r'(?:[^;"]|"[^"]*")+', text):
        name, eq, value = part.partition("=")
        if not eq:  # vCard 2.1: a bare parameter is a type
            name, value = "TYPE", part
        values = [v[1:-1] if len(v) >= 2 and v[0] == v[-1] == '"' else v
                  for v in re.findall(r'"[^"]*"|[^,]+', value)] or [""]
        out.setdefault(name.strip().upper(), []).extend(values)
    return out


def parse_line(line: str) -> Prop | None:
    """One logical line as a property, or None when it isn't a content line."""
    m = re.match(r'((?:[^:"]|"[^"]*")*):(.*)$', line)
    if not m:
        return None
    head, value = m.group(1), m.group(2)
    name, _, params = head.partition(";")
    group, _, name = name.rpartition(".")
    if not NAME.fullmatch(name):
        return None
    prop = Prop(name.upper(), _params(params) if params else {}, value, group)
    if (prop.param("ENCODING") or "").upper() == "QUOTED-PRINTABLE":
        raw = quopri.decodestring(value.encode("ascii", errors="replace"))
        prop.value = raw.decode(prop.param("CHARSET") or "utf-8", errors="replace")
        prop.params.pop("ENCODING", None)
    return prop


def parse(data: bytes | str) -> list[Component]:
    """The top-level components of a file (a calendar file holds one VCALENDAR; a .vcf one VCARD per contact)."""
    top: list[Component] = []
    stack: list[Component] = []
    for line in unfold(data):
        prop = parse_line(line)
        if prop is None:
            continue
        if prop.name == "BEGIN":
            comp = Component(prop.value.strip().upper())
            (stack[-1].children if stack else top).append(comp)
            stack.append(comp)
        elif prop.name == "END":
            name = prop.value.strip().upper()
            # Close up to the matching BEGIN; an END that matches nothing open is ignored.
            if any(c.name == name for c in stack):
                while stack and stack.pop().name != name:
                    pass
        elif stack:
            stack[-1].props.append(prop)
    return top
