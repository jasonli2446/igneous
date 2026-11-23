"""Who and where: the persona's people, organizations, courses, places and calendar. Facts only, no randomness.

Everything here is invented. Organizations and mail providers live under the reserved example domains (RFC 2606),
phone numbers in the 555-01xx range kept for fiction, and businesses and places at home are made up. The trips go
to real cities and their public landmarks.

Personal mail providers are invented too, so nothing here knows them as consumer mail (organizations.PERSONAL_DOMAINS
lists the real ones). Each carries at most two of Mira's correspondents: three people writing from one domain look like
an organization.
"""
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, timezone

# ---- time ----

FIRST_DAY = date(2024, 8, 19)   # the persona's records start here...
LAST_DAY = date(2026, 10, 30)   # ...and end here
EXPORTED = datetime(2026, 11, 2, 15, 15, 2, tzinfo=UTC)  # when the exports were downloaded


def _nth_sunday(year: int, month: int, n: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(6 - first.weekday()) % 7 + 7 * (n - 1))


def _last_sunday(year: int, month: int) -> date:
    last = date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)
    return last - timedelta(days=(last.weekday() + 1) % 7)


def offset(zone: str, local: datetime) -> timedelta:
    """The UTC offset of a zone at a naive local time: home is US Eastern, Lisbon is WET/WEST, Iceland is UTC."""
    if zone == "home":
        start = datetime.combine(_nth_sunday(local.year, 3, 2), time(2))
        end = datetime.combine(_nth_sunday(local.year, 11, 1), time(2))
        return timedelta(hours=-4 if start <= local < end else -5)
    if zone == "lisbon":
        start = datetime.combine(_last_sunday(local.year, 3), time(1))
        end = datetime.combine(_last_sunday(local.year, 10), time(2))
        return timedelta(hours=1 if start <= local < end else 0)
    return timedelta(0)


def at(day: date, hhmm: str = "12:00", zone: str = "home") -> datetime:
    """A local wall-clock time as an aware UTC datetime."""
    h, m = (int(x) for x in hhmm.split(":"))
    local = datetime.combine(day, time(h, m))
    return (local - offset(zone, local)).replace(tzinfo=UTC)


def local(moment: datetime, zone: str = "home") -> datetime:
    """An aware datetime in the zone's local time, with its fixed offset."""
    naive = moment.astimezone(UTC).replace(tzinfo=None)
    for hours in (-4, -5, 1, 0):  # the offsets the zones use: the one whose local time agrees with it
        off = timedelta(hours=hours)
        if offset(zone, naive + off) == off:
            return (naive + off).replace(tzinfo=timezone(off))
    off = offset(zone, naive)
    return (naive + off).replace(tzinfo=timezone(off))


def days(start: date, end: date, weekdays: str = "MTWRFSU") -> list[date]:
    """Every date from start to end (inclusive) on the given weekdays (MTWRFSU)."""
    out, d = [], start
    while d <= end:
        if "MTWRFSU"[d.weekday()] in weekdays:
            out.append(d)
        d += timedelta(days=1)
    return out


@dataclass(frozen=True)
class Term:
    name: str
    start: date
    end: date
    breaks: tuple[tuple[date, date], ...] = ()

    def teaching(self, day: date) -> bool:
        return self.start <= day <= self.end and not any(a <= day <= b for a, b in self.breaks)


FALL_2024 = Term("Fall 2024", date(2024, 8, 26), date(2024, 12, 13), ((date(2024, 11, 27), date(2024, 12, 1)),))
SPRING_2025 = Term("Spring 2025", date(2025, 1, 13), date(2025, 5, 9), ((date(2025, 3, 8), date(2025, 3, 16)),))
FALL_2025 = Term("Fall 2025", date(2025, 8, 25), date(2025, 12, 12), ((date(2025, 11, 26), date(2025, 11, 30)),))
SPRING_2026 = Term("Spring 2026", date(2026, 1, 12), date(2026, 5, 8), ((date(2026, 3, 7), date(2026, 3, 15)),))
TERMS = (FALL_2024, SPRING_2025, FALL_2025, SPRING_2026)
GRADUATION = date(2026, 5, 16)

# The job: part-time during the school year, full-time in summer 2025, full-time for good after graduation.
JOB_START = date(2025, 1, 21)
SUMMER_START, SUMMER_END = date(2025, 6, 2), date(2025, 8, 15)
FULL_TIME_START = date(2026, 7, 6)

LISBON = (date(2025, 3, 8), date(2025, 3, 15))
ICELAND = (date(2026, 6, 6), date(2026, 6, 14))


# ---- people ----

@dataclass(frozen=True)
class Person:
    key: str
    name: str
    email: str
    others: tuple[str, ...] = ()       # other addresses they write from
    org: str = ""                      # lakeshore | northwind | family | friend | city | ...
    role: str = ""
    company: str = ""                  # LinkedIn: current company and position
    position: str = ""
    linkedin: str = ""                 # LinkedIn profile slug ("" = not connected)
    connected: date | None = None      # LinkedIn: connected on
    shares_email: bool = False         # LinkedIn: their address is in the export
    phone: str = ""

    @property
    def first(self) -> str:
        return self.name.split()[0]

    @property
    def addresses(self) -> tuple[str, ...]:
        return (self.email, *self.others)


LAKESHORE = "lakeshore.example.edu"
NORTHWIND = "northwind.example.com"

ME = Person("mira", "Mira Castellanos", f"mcastellanos@{LAKESHORE}", org="lakeshore", role="student",
            linkedin="mira-castellanos-0b7e1d", phone="+1 (440) 555-0147")

PEOPLE: dict[str, Person] = {p.key: p for p in [
    ME,
    # Lakeshore University: faculty and staff
    Person("ferreira", "Daniel Ferreira", f"dferreira@{LAKESHORE}", org="lakeshore", role="professor",
           company="Lakeshore University", position="Professor of Computer Science"),
    Person("osei", "Kevin Osei", f"kosei@{LAKESHORE}", org="lakeshore", role="ta",
           company="Lakeshore University", position="PhD Student, Computer Science", linkedin="kevin-osei-3c8a10",
           connected=date(2024, 12, 16)),
    Person("achterberg", "Helen Achterberg", f"hachterberg@{LAKESHORE}", org="lakeshore", role="professor",
           company="Lakeshore University", position="Associate Professor of Environmental Science"),
    Person("wli", "Wen Li", f"wli@{LAKESHORE}", org="lakeshore", role="professor"),
    Person("adeyemi", "Ruth Adeyemi", f"radeyemi@{LAKESHORE}", org="lakeshore", role="professor",
           company="Lakeshore University", position="Associate Professor of Computer Science",
           linkedin="ruth-adeyemi-7f3k2q", connected=date(2024, 11, 14), shares_email=True),
    Person("hale", "Marcus Hale", f"mhale@{LAKESHORE}", org="lakeshore", role="professor"),
    Person("kowalski", "Martin Kowalski", f"mkowalski@{LAKESHORE}", org="lakeshore", role="professor",
           company="Lakeshore University", position="Professor of Computer Science",
           linkedin="martin-kowalski-p2x9", connected=date(2026, 5, 20), shares_email=True),
    Person("gliu", "Grace Liu", f"gliu@{LAKESHORE}", org="lakeshore", role="ta"),
    Person("weiss", "Jonathan Weiss", f"jweiss@{LAKESHORE}", org="lakeshore", role="professor"),
    # Lakeshore students
    Person("dana", "Dana Kimura", f"dkimura@{LAKESHORE}", others=("dana.kimura@inbox.example.net",), org="friend",
           role="roommate", company="Great Lakes Water Trust", position="Field Technician (incoming)",
           linkedin="dana-kimura-wq41", connected=date(2023, 2, 2)),
    Person("rafael", "Rafael Ortiz", f"rortiz@{LAKESHORE}", org="friend", role="classmate",
           company="Cobalt Health", position="Software Engineer", linkedin="rafael-ortiz-dev",
           connected=date(2023, 9, 12)),
    Person("sofia", "Sofia Lindgren", f"slindgren@{LAKESHORE}", org="friend", role="climber",
           company="Lakeshore University", position="Undergraduate Researcher", linkedin="sofia-lindgren-8b",
           connected=date(2024, 1, 30)),
    Person("achen", "Alex Chen", f"achen7@{LAKESHORE}", org="lakeshore", role="classmate",
           company="Harborview Bank", position="Technology Analyst Intern", linkedin="alex-chen-a7c21e",
           connected=date(2025, 12, 15)),
    Person("jordan", "Jordan Pike", f"jpike@{LAKESHORE}", org="lakeshore", role="classmate",
           linkedin="jordan-pike-22", connected=date(2025, 10, 1)),
    Person("leah", "Leah Goldberg", f"lgoldberg@{LAKESHORE}", org="lakeshore", role="classmate"),
    Person("sam", "Sam Whitfield", f"swhitfield@{LAKESHORE}", org="lakeshore", role="classmate"),
    Person("ella", "Ella Novak", f"enovak@{LAKESHORE}", org="lakeshore", role="classmate",
           company="Lakeshore University", position="Undergraduate Student", linkedin="ella-novak-22",
           connected=date(2022, 9, 5)),
    # Northwind Robotics
    Person("owen", "Owen Brandt", f"owen@{NORTHWIND}", org="northwind", role="cto", company="Northwind Robotics",
           position="Co-founder & CTO", linkedin="owen-brandt-nw", connected=date(2024, 12, 4),
           phone="+1 (440) 555-0181"),
    Person("priya", "Priya Raman", f"priya@{NORTHWIND}", org="northwind", role="mentor",
           company="Northwind Robotics", position="Senior Software Engineer", linkedin="priya-raman-5d2e",
           connected=date(2025, 1, 24), shares_email=True, phone="+1 (440) 555-0162"),
    Person("jonah", "Jonah Feld", f"jonah@{NORTHWIND}", org="northwind", role="field lead",
           company="Northwind Robotics", position="Field Operations Lead", linkedin="jonah-feld-ops",
           connected=date(2025, 2, 11), shares_email=True),
    Person("lucia", "Lucia Moretti", f"lucia@{NORTHWIND}", org="northwind", role="people & operations",
           company="Northwind Robotics", position="People & Operations Manager", linkedin="lucia-moretti-hr",
           connected=date(2025, 1, 22)),
    Person("nwchen", "Alex Chen", f"alex@{NORTHWIND}", org="northwind", role="firmware",
           company="Northwind Robotics", position="Firmware Engineer", linkedin="alexchen-firmware",
           connected=date(2025, 6, 20), shares_email=True),
    # Friends and family outside school
    Person("theo", "Theo Marsh", "theo.marsh@inbox.example.net", org="friend", role="friend",
           company="Brightline Analytics", position="Data Analyst", linkedin="theo-marsh-data",
           connected=date(2023, 5, 3)),
    Person("ama", "Ama Boateng", "ama.boateng@mailbox.example.com", org="friend", role="dana's cousin"),
    Person("mom", "Elena Castellanos", "elena.castellanos@post.example.org", org="family", role="mother"),
    Person("dad", "Roberto Castellanos", "roberto.castellanos@post.example.org", org="family", role="father"),
    Person("nico", "Nico Castellanos", "nico.c@mailbox.example.com", org="family", role="brother"),
    # Port Avalon
    Person("greg", "Greg Ferris", "greg@lindenproperties.example.com", org="landlord", role="landlord"),
    Person("hannah", "Hannah Brooks", "hbrooks@parks.portavalon.example.org", org="city",
           role="water quality coordinator", company="City of Port Avalon",
           position="Beach Water Quality Coordinator", linkedin="hannah-brooks-pa", connected=date(2025, 10, 9)),
]}


def p(key: str) -> Person:
    """A person by key; "me" is Mira."""
    return ME if key == "me" else PEOPLE[key]


# LinkedIn connections who appear in no other record: classmates and acquaintances from before these two years.
LINKEDIN_ONLY = [
    ("Marisol", "Vega", "marisol-vega-ux", "Cobalt Health", "Product Designer", date(2023, 3, 14)),
    ("Ben", "Okafor", "ben-okafor-swe", "Brightline Analytics", "Software Engineer", date(2023, 6, 1)),
    ("Hailey", "Dunn", "hailey-dunn-4e", "Lakeshore University", "Resident Advisor", date(2022, 10, 9)),
    ("Arjun", "Mehta", "arjun-mehta-ml", "Juniper Mobility", "Machine Learning Intern", date(2024, 4, 18)),
    ("Clara", "Fischer", "clara-fischer-gis", "Great Lakes Water Trust", "GIS Specialist", date(2025, 11, 20)),
    ("Tomás", "Reyes", "tomas-reyes-1f", "Harbor Street Coffee", "Shift Lead", date(2022, 7, 30)),
    ("Nadia", "Petrov", "nadia-petrov-cs", "Cobalt Health", "Data Engineer", date(2024, 2, 8)),
    ("Isaac", "Moore", "isaac-moore-robotics", "Tern Marine", "Robotics Engineer", date(2025, 9, 3)),
    ("Yuki", "Tanaka", "yuki-tanaka-design", "Studio Lantern", "Designer", date(2023, 11, 22)),
    ("Grant", "Holloway", "grant-holloway-pa", "City of Port Avalon", "Parks Analyst", date(2025, 10, 9)),
    ("Victor", "Alvarez", "victor-alvarez-eng", "Northwind Robotics", "Mechanical Engineer", date(2025, 6, 9)),
    ("Priscilla", "Wong", "priscilla-wong-recruits", "Rivet", "Technical Recruiter", date(2024, 10, 2)),
    ("Omar", "Haddad", "omar-haddad-ds", "Juniper Mobility", "Data Scientist", date(2026, 2, 17)),
]


# ---- school ----

@dataclass(frozen=True)
class Course:
    code: str
    title: str
    term: Term
    prof: str
    ta: str = ""
    meets: str = "MWF"          # weekdays
    hour: str = "10:00"
    minutes: int = 50
    room: str = ""
    weight: int = 1             # how much of the persona's semester it fills (0 light .. 3 heavy)
    classmates: tuple[str, ...] = ()


COURSES = [
    Course("CS 341", "Operating Systems", FALL_2024, "ferreira", "osei", "MWF", "10:00", 50, "Hollis Hall 214", 3,
           ("rafael", "leah")),
    Course("ENVS 210", "Freshwater Ecology", FALL_2024, "achterberg", "", "TR", "13:00", 75, "Marsh Science 108", 2,
           ("dana",)),
    Course("STAT 315", "Probability and Statistics", FALL_2024, "wli", "", "TR", "09:30", 75, "Grove Hall 120", 1),
    Course("CS 375", "Machine Learning", SPRING_2025, "adeyemi", "", "TR", "11:00", 75, "Hollis Hall 110", 3,
           ("sam", "rafael")),
    Course("CS 362", "Database Systems", SPRING_2025, "hale", "", "MWF", "14:00", 50, "Hollis Hall 214", 2,
           ("leah", "rafael")),
    Course("CS 452", "Distributed Systems", FALL_2025, "kowalski", "gliu", "TR", "14:30", 75, "Hollis Hall 110", 3,
           ("achen", "jordan")),
    Course("ENVS 410", "GIS for Water Resources", FALL_2025, "achterberg", "", "W", "13:00", 170, "Marsh Science 012",
           1, ("dana",)),
    Course("CS 481", "Senior Capstone", SPRING_2026, "adeyemi", "", "TR", "15:00", 75, "Hollis Hall 301", 3,
           ("rafael", "jordan", "leah")),
    Course("PHIL 220", "Ethics of Technology", SPRING_2026, "weiss", "", "MW", "11:00", 75, "Grove Hall 204", 1),
]


# ---- places ----

@dataclass(frozen=True)
class Place:
    key: str
    name: str
    address: str
    lat: float
    lng: float
    place_id: str
    zone: str = "home"


def _pid(key: str) -> str:
    """A Maps place id shaped like Google's ("ChIJ" and 23 more), derived from the place's key."""
    import hashlib

    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    digest = hashlib.sha256(key.encode()).digest()
    return "ChIJ" + "".join(alphabet[b % 64] for b in digest[:23])


def _place(key, name, address, lat, lng, zone="home") -> Place:
    return Place(key, name, address, lat, lng, _pid(key), zone)


PLACES: dict[str, Place] = {pl.key: pl for pl in [
    _place("home", "418 Linden Ave", "418 Linden Ave, Port Avalon", 42.2716, -79.8431),
    _place("hollis", "Hollis Hall", "Lakeshore University, 30 College Rd, Port Avalon", 42.2789, -79.8352),
    _place("library", "Marrow Library", "Lakeshore University, 12 College Rd, Port Avalon", 42.2795, -79.8366),
    _place("marsh", "Marsh Science Center", "Lakeshore University, 44 College Rd, Port Avalon", 42.2781, -79.8340),
    _place("union", "Student Union", "Lakeshore University, 2 College Rd, Port Avalon", 42.2799, -79.8375),
    _place("northwind", "Northwind Robotics", "22 Pier Rd, Port Avalon", 42.2650, -79.8512),
    _place("coffee", "Harbor Street Coffee", "105 Harbor St, Port Avalon", 42.2668, -79.8477),
    _place("gym", "Boulder Barn Climbing", "9 Mill Race Ln, Port Avalon", 42.2731, -79.8299),
    _place("beach", "Avalon North Beach", "Lakefront Dr, Port Avalon", 42.2812, -79.8580),
    _place("market", "Avalon Public Market", "60 Front St, Port Avalon", 42.2672, -79.8455),
    _place("station", "Port Avalon International Airport", "1 Airport Pkwy, Port Avalon", 42.2223, -79.8941),
    _place("fieldstation", "Lakeshore Ecology Field Station", "Shore Rd, Port Avalon", 42.2903, -79.8811),
    _place("library2", "Port Avalon Public Library", "200 Main St, Port Avalon", 42.2694, -79.8462),
    _place("parents", "12 Orchard Ln", "12 Orchard Ln, Riverton", 41.8862, -80.3127),
    _place("pierlofts", "Pier Lofts", "8 Pier Rd, Port Avalon", 42.2641, -79.8526),
    # Lisbon
    _place("casalumen", "Casa Lumen Guesthouse", "Rua do Vale 14, 1200 Lisboa", 38.7139, -9.1334, "lisbon"),
    _place("belem", "Belém Tower", "Av. Brasília, 1400-038 Lisboa", 38.6916, -9.2160, "lisbon"),
    _place("lxfactory", "LX Factory", "R. Rodrigues de Faria 103, 1300-501 Lisboa", 38.7037, -9.1785, "lisbon"),
    _place("timeout", "Time Out Market Lisboa", "Av. 24 de Julho 49, 1200-479 Lisboa", 38.7071, -9.1460, "lisbon"),
    _place("pena", "Pena Palace", "Estrada da Pena, 2710-609 Sintra", 38.7876, -9.3906, "lisbon"),
    _place("miradouro", "Miradouro da Senhora do Monte", "Largo Monte, 1170-107 Lisboa", 38.7193, -9.1328,
           "lisbon"),
    _place("lis", "Lisbon Airport", "Alameda das Comunidades Portuguesas, 1700-111 Lisboa", 38.7742, -9.1342,
           "lisbon"),
    _place("tasca", "Tasca do Mar", "Rua dos Remédios 31, 1100-443 Lisboa", 38.7116, -9.1290, "lisbon"),
    # Iceland
    _place("kef", "Keflavík International Airport", "235 Keflavíkurflugvöllur", 63.9850, -22.6056, "iceland"),
    _place("hallgrims", "Hallgrímskirkja", "Hallgrímstorg 1, 101 Reykjavík", 64.1417, -21.9266, "iceland"),
    _place("thingvellir", "Þingvellir National Park", "806 Þingvellir", 64.2559, -21.1299, "iceland"),
    _place("seljalandsfoss", "Seljalandsfoss", "Þórsmerkurvegur, 861", 63.6156, -19.9886, "iceland"),
    _place("skogafoss", "Skógafoss", "Skógar, 861", 63.5321, -19.5114, "iceland"),
    _place("reynisfjara", "Reynisfjara Beach", "Reynisfjara, 871 Vík", 63.4044, -19.0451, "iceland"),
    _place("jokulsarlon", "Jökulsárlón Glacier Lagoon", "Jökulsárlón, 781", 64.0784, -16.2306, "iceland"),
    _place("myvatn", "Mývatn Nature Baths", "Jarðbaðshólar, 660 Mývatn", 65.6309, -16.8484, "iceland"),
    _place("akureyri", "Akureyri Harbour", "Strandgata, 600 Akureyri", 65.6835, -18.0878, "iceland"),
    _place("fjallakot", "Fjallakot Guesthouse", "Austurvegur 7, 870 Vík", 63.4197, -19.0060, "iceland"),
    _place("amaflat", "Ama's flat", "Bergstaðastræti 52, 101 Reykjavík", 64.1432, -21.9341, "iceland"),
]}
