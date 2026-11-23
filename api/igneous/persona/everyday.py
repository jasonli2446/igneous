"""Everything between the storylines: family and friends, the apartment, the climbing club, the bank, deliveries
and newsletters, where Mira's phone saw her each day, what she searched, watched and browsed, and the odd question
to ChatGPT or Gemini. Most of it belongs to no thread, which is what makes the threads stand out.
"""
from datetime import date, timedelta

from igneous.persona import work
from igneous.persona.cast import FIRST_DAY, ICELAND, LAKESHORE, LAST_DAY, LISBON, ME, TERMS, at, days, p
from igneous.persona.life import Life, PageVisit, Prompt, Task, TaskList

BANK = ("Lakeshore Credit Union", "alerts@lakeshorecu.example.com")
PARCEL = ("QuickParcel", "updates@quickparcel.example.com")
TRANSIT = ("Port Avalon Transit", "alerts@pat.example.org")
PHONE = ("Lumen Mobile", "billing@lumenmobile.example.com")
WATERSHED = ("The Watershed Weekly", "newsletter@watershedweekly.example.org")
GYM = ("Boulder Barn", "news@boulderbarn.example.com")
DEALS = ("Atlantica Air", "deals@atlantica-air.example.com")
CAREERS = ("Lakeshore Career Center", f"careers@{LAKESHORE}")
CLUB = ("Lakeshore Climbing Club", f"climbing-club@{LAKESHORE}")

HOLIDAYS = [(date(2024, 11, 27), date(2024, 12, 1)), (date(2024, 12, 20), date(2025, 1, 5)),
            (date(2025, 11, 26), date(2025, 11, 30)), (date(2025, 12, 19), date(2026, 1, 4))]


def away(day: date) -> str | None:
    """Where Mira is when not in Port Avalon: a trip, or her parents' for the holidays."""
    if LISBON[0] < day <= LISBON[1]:
        return "lisbon"
    if ICELAND[0] < day <= ICELAND[1]:
        return "iceland"
    if any(a <= day <= b for a, b in HOLIDAYS):
        return "parents"
    return None


def d(y: int, m: int, day: int) -> date:
    return date(y, m, day)


# ---- people ----

FAMILY = [
    (d(2024, 8, 25), "mom", "Moving in", "Mija, did the move go okay? Send a picture of the new apartment. Dad says "
     "the bike rack is in the garage if you want it.", "All moved in! Dana already put plants everywhere. I'll get "
     "the bike rack at Thanksgiving."),
    (d(2024, 9, 22), "mom", "Abuela's birthday", "Don't forget Abuela turns 80 on Saturday. Call her in the morning, "
     "she goes to mass at 11.", "I'll call at 9. Should I send flowers?"),
    (d(2024, 10, 20), "dad", "Car registration", "Your car registration renews in November. I sent the form to the "
     "house; I'll mail it to you with the insurance card.", "Thanks Dad. I'll do it online if that's easier."),
    (d(2024, 11, 18), "mom", "Thanksgiving", "Are you coming Wednesday or Thursday? Nico gets in Wednesday night.",
     "Wednesday afternoon, the 3:10 train. Can someone pick me up?"),
    (d(2024, 12, 6), "mom", "Proud of you", "Dad told me about the Northwind job! Your first real engineering job. "
     "We're so proud.", "Thank you Mom! It's part-time to start but they build boats that test lake water, it's "
     "perfect."),
    (d(2025, 1, 26), "dad", "First week at work", "How was the first week at Northwind?", "Great! My mentor Priya "
     "is so smart and I already shipped a small fix."),
    (d(2025, 3, 3), "mom", "Lisbon", "Send me the name of the guesthouse in Lisbon so I can stop worrying.",
     "Casa Lumen Guesthouse, Rua do Vale 14. I'll text when we land."),
    (d(2025, 4, 27), "nico", "Your app", "Saw Tidepool on the Port Avalon forum. My little sister, the civic hacker.",
     "Haha thank you. It only texted 12 people so far."),
    (d(2025, 5, 18), "mom", "Summer", "Are you coming home before the internship starts?", "Just for a long weekend, "
     "May 23-26."),
    (d(2025, 7, 20), "dad", "Wedding Saturday", "Cousin Lucas's wedding is Saturday the 26th in Riverton. You're "
     "coming Friday night?", "Yes, I took Friday off. See you then."),
    (d(2025, 9, 7), "mom", "Senior year", "Last first day of school! How are your classes?", "Distributed systems "
     "is hard but fun. I'm in a group with Jordan and Alex."),
    (d(2025, 11, 12), "nico", "Thanksgiving pickup", "I can pick you up from the train Wednesday. 3:10 again?",
     "3:10, yes. Thank you!"),
    (d(2026, 1, 18), "mom", "Last semester", "Last semester! What's the capstone about?", "A live map of the harbor "
     "shuttle. Adeyemi is our advisor again."),
    (d(2026, 3, 9), "mom", "!!!", "Dad said you have news?", "Northwind offered me a full-time job starting in July! "
     "I'm accepting it."),
    (d(2026, 4, 21), "dad", "Graduation tickets", "How many tickets do we get for graduation? Abuela wants to come.",
     "Six! Abuela, you, Mom, Nico, Tía Rosa and one spare."),
    (d(2026, 5, 17), "mom", "Yesterday", "I'm still crying. Our graduate! The photos are in the shared album.",
     "Thank you for coming, all of you. Best day."),
    (d(2026, 6, 3), "mom", "Iceland", "Be careful driving in Iceland. Do they drive on the right?", "On the right, "
     "and Theo is a very careful driver. I'll send pictures."),
    (d(2026, 7, 12), "dad", "New apartment", "How's the new place on Pier Rd?", "Tiny but I can walk to work. "
     "Come see it in August."),
    (d(2026, 9, 20), "mom", "Visit", "Can we visit October 10? Dad wants to see the boats.", "Yes! I'll ask Jonah "
     "if we can see a Heron up close."),
]


def family(life: Life) -> None:
    rng = life.rng("family")
    for day, who, subject, body, reply in FAMILY:
        sender = p(who)
        cc = [p("dad")] if who == "mom" and rng.random() < 0.3 else []
        t = at(day, f"{rng.randint(8, 21):02d}:{rng.randint(0, 59):02d}")
        sign = {"mom": "Love, Mom", "dad": "Dad", "nico": "N"}[who]
        life.thread(subject, [(t, sender, [ME], f"{body}\n\n{sign}", cc),
                              (t + timedelta(hours=rng.randint(1, 20)), ME, [sender], reply + "\n\nMira", cc)])


def apartment(life: Life) -> None:
    greg, dana = p("greg"), p("dana")
    personal = ("Dana Kimura", "dana.kimura@inbox.example.net")
    life.thread("Heater in unit 2", [
        (at(d(2024, 11, 4), "08:20"), ME, [greg], "Hi Greg,\n\nThe heater in unit 2 at 418 Linden stopped working "
         "last night. Could someone look at it this week?\n\nMira Castellanos", [dana]),
        (at(d(2024, 11, 4), "11:05"), greg, [ME], "Hi Mira, a technician will come Wednesday between 10 and 2. Leave "
         "the thermostat on.\n\nGreg Ferris, Linden Properties", [dana]),
        (at(d(2024, 11, 6), "15:30"), dana, [greg, ME], "Fixed, thank you Greg!"),
    ])
    life.thread("Lease renewal, 418 Linden Ave unit 2", [
        (at(d(2025, 4, 2), "10:00"), greg, [ME, personal], "Hi Mira and Dana,\n\nYour lease ends July 31. I can "
         "renew it for another year at $1,480 a month (up from $1,420). Let me know by May 1.\n\nGreg"),
        (at(d(2025, 4, 3), "19:12"), personal, [greg, ME], "Hi Greg, we'd like to renew. Can we sign before "
         "finals?\n\nDana"),
        (at(d(2025, 4, 4), "09:00"), greg, [personal, ME], "Of course, I'll send the papers this week."),
    ])
    life.thread("Move-out, 418 Linden Ave unit 2", [
        (at(d(2026, 5, 26), "09:40"), personal, [greg, ME], "Hi Greg,\n\nWe'll move out on July 31 as the lease says. "
         "When can we do the walkthrough?\n\nDana"),
        (at(d(2026, 5, 26), "14:15"), greg, [personal, ME], "July 31 at 10am works. Thanks for being great "
         "tenants.\n\nGreg"),
    ])
    life.thread("Porto next spring?", [
        (at(d(2025, 9, 15), "21:00"), personal, [ME, p("sofia")], "Following up on the chat: Porto over spring break "
         "2026? I looked at flights already."),
        (at(d(2025, 9, 15), "21:40"), p("sofia"), [personal, ME], "I can't, it's my thesis semester. Next year!"),
    ])
    life.doc("Personal/apartment/lease-renewal-2025-notes.md", at(d(2025, 4, 10), "20:00"),
             "# Lease renewal 2025\n\n418 Linden Ave unit 2, Aug 1 2025 - Jul 31 2026, $1,480/month, split 50/50 "
             "with Dana. Signed April 9.\n")
    life.doc("Personal/apartment/move-out-checklist.md", at(d(2026, 7, 28), "21:00"),
             "# Move-out checklist\n\n- patch nail holes\n- clean oven\n- return keys to Greg July 31, 10am\n- "
             "forward mail to 8 Pier Rd, Pier Lofts #4\n")


def roommate_chat(life: Life) -> None:
    dana = p("dana")
    life.space("dm-dana", "", [ME, dana])
    rng = life.rng("dm-dana")
    banks = [
        [(dana, "groceries? I'm at the market"), (ME, "eggs, oat milk, limes"), (dana, "got it")],
        [(ME, "rent is due friday, I paid my half"), (dana, "doing mine tonight")],
        [(dana, "the plants are thriving"), (ME, "the basil is out of control")],
        [(ME, "home late, work thing"), (dana, "leftovers in the fridge")],
        [(dana, "movie night?"), (ME, "yes! your pick"), (dana, "something with boats, for you")],
        [(dana, "my water samples came back clean"), (ME, "yay for the creek")],
        [(ME, "did you take my umbrella"), (dana, "...yes. sorry"), (ME, "lol")],
        [(dana, "interview with Great Lakes Water Trust went well!"), (ME, "!!! tell me everything")],
        [(ME, "landlord says the heater guy comes wednesday"), (dana, "finally")],
        [(dana, "climbing tonight?"), (ME, "yes, 7 at Boulder Barn")],
        [(ME, "I'm making arroz con pollo, want some?"), (dana, "always")],
        [(dana, "can you sign for my package"), (ME, "got it, it's on your bed")],
        [(ME, "presentation went fine!"), (dana, "told you"), (ME, "🥲")],
        [(dana, "dishes?"), (ME, "doing them now I swear")],
    ]
    for day in days(FIRST_DAY, LAST_DAY):
        if away(day) or (day > d(2026, 7, 31) and rng.random() < 0.6) or rng.random() > 0.62:
            continue
        life.chat("dm-dana", at(day, f"{rng.randint(9, 22):02d}:{rng.randint(0, 59):02d}"), rng.choice(banks))


def climbing(life: Life) -> None:
    """The climbing club: a chat with Sofia, Theo and Rafael, the club's list mail, and evenings at the gym."""
    sofia, theo, rafael = p("sofia"), p("theo"), p("rafael")
    life.space("climbing", "Lakeshore climbing", [ME, sofia, theo, rafael])
    rng = life.rng("climbing")
    banks = [
        [(sofia, "Boulder Barn tonight at 7?"), (ME, "yes"), (theo, "I'll be there by 7:30")],
        [(theo, "new set on the cave wall, there's a purple V4 that's all heel hooks"), (ME, "I'm scared")],
        [(sofia, "club comp is Saturday, sign up by Thursday"), (rafael, "signed up"), (ME, "me too")],
        [(ME, "finally sent the orange V3!!"), (sofia, "LET'S GO")],
        [(theo, "anyone want to split a 10-punch pass"), (ME, "me")],
        [(rafael, "skin is gone, rest day"), (sofia, "tape and come anyway")],
        [(sofia, "outdoor trip to the ledges in october?"), (theo, "if it's dry"), (ME, "yes please")],
    ]
    for day in days(FIRST_DAY, LAST_DAY, "TS"):
        if away(day) or rng.random() < 0.4:
            continue
        if rng.random() < 0.55:
            life.chat("climbing", at(day, f"{rng.randint(12, 18):02d}:{rng.randint(0, 59):02d}"), rng.choice(banks))
        life.visit("gym", at(day, "19:05" if day.weekday() == 1 else "14:10"), rng.randint(80, 140))
    month = d(2024, 9, 1)
    topics = ["Fall kickoff and belay checks", "Club comp this Saturday", "Outdoor trip sign-ups", "Dues for the "
              "semester", "Route-setting clinic", "End-of-semester social"]
    while month <= d(2026, 5, 1):
        if month.month not in (6, 7, 8, 1):
            t = at(month + timedelta(days=rng.randint(2, 20)), "12:00")
            life.mail(t, CLUB, [ME], f"[Climbing Club] {rng.choice(topics)}",
                      "Hi climbers,\n\nDetails in the club calendar. See you at Boulder Barn!\n\nLakeshore Climbing "
                      "Club officers\n\nTo unsubscribe from this list, reply with UNSUBSCRIBE.",
                      headers={"Precedence": "list", "List-Id": f"<climbing-club.{LAKESHORE}>"})
        month = (month.replace(day=1) + timedelta(days=32)).replace(day=1)


def friends(life: Life) -> None:
    theo, sofia = p("theo"), p("sofia")
    life.thread("Chicago this weekend?", [
        (at(d(2025, 7, 7), "20:10"), theo, [ME], "I started at Brightline! Come visit Chicago this weekend? The "
         "couch is yours."),
        (at(d(2025, 7, 7), "21:00"), ME, [theo], "Congrats!! I can't this weekend (field trial wrap-up), but August?"),
    ])
    life.thread("Sofia's thesis talk", [
        (at(d(2026, 4, 6), "11:00"), sofia, [ME, p("dana")], "My honors thesis defense is Friday at 3 in Marsh 108. "
         "Moral support welcome, pastries provided."),
        (at(d(2026, 4, 6), "11:30"), ME, [sofia, p("dana")], "We'll be in the front row!"),
    ])


# ---- the machines' mail ----

def notifications(life: Life) -> None:
    rng = life.rng("notifications")
    month = d(2024, 8, 1)
    while month <= LAST_DAY:
        life.mail(at(month + timedelta(days=4), "05:30"), BANK, [ME], f"Your {month:%B} statement is ready",
                  f"Your Lakeshore Credit Union statement for {month:%B %Y} is ready in online banking.\n\nThis is an "
                  "automated message.")
        life.mail(at(month + timedelta(days=11), "06:00"), PHONE, [ME], f"Your Lumen Mobile bill for {month:%B}",
                  f"Your bill of $35.00 for {month:%B %Y} will be paid automatically on the 20th.")
        month = (month + timedelta(days=32)).replace(day=1)
    things = ["climbing shoes", "a USB-C hub", "a rain shell", "textbooks", "plant pots", "a desk lamp", "hiking socks",
              "a power bank", "coffee beans", "a birthday gift for Mom", "an EU plug adapter", "a sleep mask"]
    for day in days(FIRST_DAY, LAST_DAY):
        if away(day) or rng.random() > 0.035:
            continue
        thing = rng.choice(things)
        number = f"QP{rng.randint(10**9, 10**10 - 1)}"
        life.mail(at(day, f"{rng.randint(9, 19):02d}:{rng.randint(0, 59):02d}"), PARCEL, [ME],
                  f"Your package is out for delivery ({number})",
                  f"Your package ({thing}) is out for delivery today.\nTracking number {number}.")
    for day in days(FIRST_DAY, LAST_DAY, "MTWRF"):
        if rng.random() < 0.012:
            life.mail(at(day, "06:45"), TRANSIT, [ME], "Service alert: Route 4 Harbor detour",
                      "Route 4 Harbor is detoured around Front St due to construction. Expect 5-10 minute delays.")


def newsletters(life: Life) -> None:
    rng = life.rng("newsletters")
    unsub = {"List-Unsubscribe": "<https://lists.example.org/unsubscribe>"}
    stories = ["Algal bloom season starts early on Lake Erie's western basin", "What a warmer winter means for lake "
               "ice", "How cities test beach water, and why results take a day", "Volunteer monitors find microplastics "
               "in every sample", "Phosphorus runoff: the farm bill fight", "Inside a buoy that measures lake "
               "oxygen", "Swimmers want faster beach advisories"]
    for day in days(FIRST_DAY, LAST_DAY, "U"):
        life.mail(at(day, "07:00"), WATERSHED, [ME], f"The Watershed Weekly: {rng.choice(stories)}",
                  f"This week: {rng.choice(stories)}. Also: events, jobs, and a reader photo of the week.\n\n"
                  "You're receiving this because you subscribed. Unsubscribe or manage your preferences.",
                  headers=unsub)
    month = d(2024, 9, 1)
    while month <= LAST_DAY:
        life.mail(at(month + timedelta(days=rng.randint(0, 10)), "10:00"), GYM, [ME], "Boulder Barn: new sets and "
                  "member events", "New problems on the cave wall, yoga for climbers on Sundays, and the member "
                  "social on the 20th.\n\nUnsubscribe from these emails.", headers=unsub)
        life.mail(at(month + timedelta(days=rng.randint(11, 25)), "09:00"), DEALS, [ME], "Fares from $99: this "
                  "month's deals from Port Avalon", "Fly to Boston, Chicago, Reykjavik and Lisbon for less. Book by "
                  "Sunday.\n\nUnsubscribe | Manage preferences", headers=unsub)
        month = (month + timedelta(days=32)).replace(day=1)
    for term in TERMS:
        for day in days(term.start, term.end, "M"):
            life.mail(at(day, "08:00"), CAREERS, [ME], "This week at the Career Center",
                      "Resume reviews, employer info sessions and the spring career fair.\n\nManage your email "
                      "preferences.", headers={"Precedence": "bulk"})


# ---- where, what she looked up ----

def places(life: Life) -> None:
    """A day's visits: home, campus on some evenings, the coffee shop and market on weekends, the office on work
    days, her parents' over the holidays (lectures, trips, the gym and field days are added where they happen)."""
    rng = life.rng("places")
    for day in days(FIRST_DAY, LAST_DAY):
        where = away(day)
        if where in ("lisbon", "iceland"):
            continue
        home = "pierlofts" if day >= d(2026, 7, 31) else "home"
        if where == "parents":
            life.visit("parents", at(day, "09:00"), 13 * 60)
            continue
        life.visit(home, at(day, "06:30"), rng.randint(60, 120))
        if shift := work.shift(day):
            life.visit("northwind", at(day, "12:50" if shift == "part" else "08:55"), 310 if shift == "part" else 500)
        if day.weekday() < 5 and any(t.teaching(day) for t in TERMS) and rng.random() < 0.3:
            life.visit("library", at(day, f"{rng.randint(15, 18)}:{rng.randint(10, 50)}"), rng.randint(60, 180))
        if day.weekday() >= 5:
            if rng.random() < 0.5:
                life.visit("coffee", at(day, f"{rng.randint(9, 11)}:{rng.randint(10, 50)}"), rng.randint(40, 120))
            if day.weekday() == 5 and rng.random() < 0.6:
                life.visit("market", at(day, f"{rng.randint(11, 13)}:{rng.randint(10, 50)}"), rng.randint(30, 80))
            if day.month in (6, 7, 8) and rng.random() < 0.4:
                life.visit("beach", at(day, "14:20"), rng.randint(90, 200))
        life.visit(home, at(day, f"{rng.randint(19, 21)}:{rng.randint(10, 50)}"), rng.randint(300, 600))


SEARCHES = ["weather port avalon", "arroz con pollo recipe", "how long to boil eggs", "best climbing shoes for "
            "beginners", "port avalon farmers market hours", "harbor street coffee hours", "python dataclass default "
            "factory", "postgres index on jsonb field", "git undo last commit", "how to fold a fitted sheet",
            "cheap flights from port avalon", "lake avalon water temperature", "roth ira for first job",
            "abuela birthday gift ideas", "how to write a cover letter software engineer", "pier lofts port avalon",
            "boulder barn hours", "electric bike rental port avalon", "go generics tutorial", "mqtt qos 1 vs 2",
            "protobuf oneof backwards compatibility", "kalman filter tutorial", "commencement lakeshore university"]
VIDEOS = [("Watched Raft consensus explained in 12 minutes", "Systems Simplified"),
          ("Watched How to heel hook (bouldering technique)", "Crux Coaching"),
          ("Watched Lisbon in 4 days: a budget guide", "Wander Cheap"),
          ("Watched Driving Iceland's Ring Road in June", "North Roads"),
          ("Watched Kalman filters, intuitively", "Signal & Noise"),
          ("Watched Easy arroz con pollo", "Cocina con Lupe"),
          ("Watched Why lakes turn green", "Freshwater Explained")]
PAGES = [("Lakeshore Canvas", "https://canvas.lakeshore.example.edu/courses"),
         ("mira-c/tidepool", "https://code.example.org/mira-c/tidepool"),
         ("Wavelink dashboard", "https://dash.northwind.example.com/fleet"),
         ("Beach advisories | City of Port Avalon", "https://www.portavalon.example.org/beaches/advisories"),
         ("The Watershed Weekly", "https://watershedweekly.example.org/latest"),
         ("Boulder Barn schedule", "https://boulderbarn.example.com/schedule"),
         ("Port Avalon Transit trip planner", "https://pat.example.org/plan"),
         ("Lakeshore Library catalog", "https://library.lakeshore.example.edu/search")]


def browsing(life: Life) -> None:
    rng = life.rng("browsing")
    for day in days(FIRST_DAY, LAST_DAY):
        zone = away(day) if away(day) in ("lisbon", "iceland") else "home"
        if rng.random() < 0.22:
            q = rng.choice(SEARCHES)
            life.search(at(day, f"{rng.randint(8, 23):02d}:{rng.randint(0, 59):02d}", zone), "Search",
                        f"Searched for {q}", "https://www.google.com/search?q=" + q.replace(" ", "+"))
        if rng.random() < 0.08:
            title, channel = rng.choice(VIDEOS)
            life.search(at(day, f"{rng.randint(19, 23):02d}:{rng.randint(0, 59):02d}", zone), "YouTube", title,
                        "https://www.youtube.com/watch?v=" + life.unique("video", title)[:11], channel)
        if rng.random() < 0.18 and zone == "home":
            for _ in range(rng.randint(1, 4)):
                title, url = rng.choice(PAGES)
                life.pages.append(PageVisit(at(day, f"{rng.randint(8, 23):02d}:{rng.randint(0, 59):02d}"), title, url))
    life.bookmarks += [("School", "Lakeshore Canvas", "https://canvas.lakeshore.example.edu/"),
                       ("School", "Lakeshore Library", "https://library.lakeshore.example.edu/"),
                       ("Work", "Wavelink dashboard", "https://dash.northwind.example.com/fleet"),
                       ("Work", "Northwind wiki", "https://wiki.northwind.example.com/"),
                       ("Tidepool", "Repository", "https://code.example.org/mira-c/tidepool"),
                       ("Tidepool", "City beach advisories", "https://www.portavalon.example.org/beaches/advisories"),
                       ("", "Boulder Barn", "https://boulderbarn.example.com/")]


ASIDES = [
    (d(2024, 9, 8), "Arroz con pollo for six", "Give me an arroz con pollo recipe for six people that I can make in "
     "one big pan.", "Brown 8 seasoned thighs, set aside. Soften onion, pepper and garlic with sofrito, toast 3 cups "
     "of rice with a spoon of tomato paste and sazón, add 4 1/2 cups of stock and the chicken, cover and cook 25 "
     "minutes on low. Peas and olives at the end."),
    (d(2024, 10, 13), "Budget for a student apartment", "I pay $710 rent and make about $600 a month. How should I "
     "budget the rest of my expenses?", "Start from fixed costs (rent, phone, transit), then set weekly caps for "
     "groceries and fun, and put any surplus first into an emergency fund of one month's expenses."),
    (d(2025, 1, 5), "Gift ideas for a mom who gardens", "Birthday gift ideas under $50 for my mom, who gardens and "
     "likes to cook.", "A Japanese hori-hori garden knife, a set of heirloom seeds she can't find locally, a "
     "herb-drying rack, or a cookbook organized by what's in season."),
    (d(2025, 8, 30), "Roth IRA basics", "I'm 21 with a part-time job. Should I open a Roth IRA?", "Probably yes, if "
     "you have earned income and a cushion: you pay tax now at a low rate, and qualified withdrawals in retirement "
     "are tax-free. Contributions (not earnings) can come out anytime, which makes it a flexible start."),
    (d(2025, 12, 28), "Writing a reference request", "How do I ask a professor for a reference for a full-time "
     "job?", "Ask early and specifically: name the role and deadline, remind them which course or project you "
     "did with them, attach your resume, and make it easy to say no."),
    (d(2026, 2, 22), "Practicing for a technical interview", "Give me three medium system design questions about "
     "telemetry pipelines to practice.", "1. Design ingestion for 10,000 buoys reporting every minute, with late and "
     "duplicate packets. 2. Design alerting that never fires twice for one reading. 3. Design storage that keeps a "
     "year of per-second data queryable by sensor and time."),
    (d(2026, 5, 4), "Graduation thank-you notes", "Help me write a short thank-you note to my advisor Ruth Adeyemi "
     "for four years of advice and the introduction that got me my job.", "Dear Professor Adeyemi, thank you for "
     "four years of advice, for pushing me toward problems I cared about, and for the introduction to Owen that "
     "became my first job. I hope to pay it forward. With gratitude, Mira."),
    (d(2026, 7, 26), "Moving checklist", "Checklist for moving out of a shared apartment and into a studio.",
     "Notify the landlord in writing, photograph the unit, schedule the walkthrough, forward mail, transfer "
     "utilities and internet, update your address with your bank and employer, and pack a first-night box."),
    (d(2026, 9, 27), "Explaining my job to my parents", "How do I explain what a telemetry pipeline is to my "
     "parents?", "Say the boats and buoys send tiny postcards every few seconds (where they are, how deep, how warm "
     "the water is); the pipeline is the post office that reads every postcard, files it, and rings a bell when "
     "one says something's wrong."),
]

GEMINI = [
    (d(2026, 1, 15), "Summarize the main points of a capstone project rubric with categories: design, process, "
     "demo, and report.", "<p>The rubric weighs four things: a sound <b>design</b> with tradeoffs, a visible "
     "<b>process</b> (planning, reviews, testing), a working <b>demo</b>, and a clear <b>report</b>.</p>"),
    (d(2026, 2, 5), "What should I bring to a hackathon-style demo day?", "<p>Your laptop and charger, a backup "
     "video of the demo, a one-page handout, and a story about who it's for.</p>"),
    (d(2026, 3, 18), "Iceland in June: how many hours of daylight?", "<p>Almost all of them: around 21 hours, and "
     "it never gets fully dark.</p>"),
    (d(2026, 6, 20), "Ideas for a housewarming in a tiny studio", "<p>Keep it to snacks and a short guest list, open "
     "the windows, and ask everyone to bring one thing for the walls.</p>"),
    (d(2026, 8, 14), "What is a good first-year budget for a $96,000 salary in a small Great Lakes city?",
     "<p>After taxes and retirement, roughly $5,000 a month: about $1,500 housing, $600 food, $300 transport, "
     "$500 savings and the rest for everything else.</p>"),
]


def asides(life: Life) -> None:
    rng = life.rng("asides")
    for day, title, q, a in ASIDES:
        t = at(day, f"{rng.randint(10, 23):02d}:{rng.randint(0, 59):02d}")
        life.ask(title, [(t, q), (t + timedelta(seconds=25), a)])
    for day, q, a in GEMINI:
        life.prompts.append(Prompt(at(day, f"{rng.randint(10, 22):02d}:{rng.randint(0, 59):02d}"), q, a))


def personal_docs(life: Life) -> None:
    life.doc("Personal/recipes/arroz-con-pollo.md", at(d(2024, 9, 8), "18:00"),
             "# Arroz con pollo (for 6)\n\n8 thighs, sofrito, 3 cups rice, 4 1/2 cups stock, sazón, peas, olives. "
             "25 min covered on low. Abuela adds a splash of beer.\n")
    life.doc("Personal/budget-2025.xlsx", at(d(2025, 1, 12), "16:00"), {"2025": [
        ["Month", "Income", "Rent", "Groceries", "Phone", "Fun", "Saved"],
        ["January", 1210, 710, 210, 35, 90, 165],
        ["February", 1680, 710, 230, 35, 120, 585],
        ["March", 1690, 710, 180, 35, 410, 355]]})
    life.doc("Personal/graduation-thank-you-list.md", at(d(2026, 5, 18), "20:30"),
             "# Thank-you notes\n\n- Professor Adeyemi (advisor, the Northwind intro)\n- Professor Kowalski "
             "(reference)\n- Priya (mentor)\n- Hannah Brooks (Tidepool pilot)\n- Mom, Dad, Nico, Abuela\n")
    life.doc("Climbing/training-plan.md", at(d(2025, 10, 2), "21:00"),
             "# Climbing plan, fall 2025\n\nTue: limit bouldering at Boulder Barn. Sat: volume + hangboard 3x10s.\n"
             "Goal: send the purple V4 on the cave wall before the club comp.\n")
    life.task_lists.append(TaskList("My Tasks", [
        Task("Renew car registration", at(d(2024, 10, 21), "09:00"), d(2024, 11, 15), at(d(2024, 11, 2), "11:00")),
        Task("Call Abuela", at(d(2024, 9, 22), "20:00"), d(2024, 9, 28), at(d(2024, 9, 28), "09:05")),
        Task("Sign lease renewal", at(d(2025, 4, 4), "09:30"), d(2025, 5, 1), at(d(2025, 4, 9), "18:00")),
        Task("Apply to graduate", at(d(2026, 2, 2), "08:00"), d(2026, 2, 15), at(d(2026, 2, 6), "14:00")),
        Task("Thank-you notes", at(d(2026, 5, 18), "20:35"), d(2026, 5, 31), at(d(2026, 5, 29), "19:00")),
        Task("Move to Pier Lofts", at(d(2026, 6, 25), "10:00"), d(2026, 7, 31), at(d(2026, 7, 31), "18:00")),
    ], tag="tasks.my-tasks"))


def write(life: Life) -> None:
    family(life)
    apartment(life)
    roommate_chat(life)
    climbing(life)
    friends(life)
    notifications(life)
    newsletters(life)
    places(life)
    browsing(life)
    asides(life)
    personal_docs(life)
