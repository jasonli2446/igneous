"""The side project: Tidepool, open-source beach water-quality alerts for Lake Avalon, built with Rafael.

The idea comes from the ENVS 210 field day (October 2024); Mira and Rafael start it in February 2025 in a git
checkout in her documents folder. Priya introduces her to Hannah Brooks, who runs the city's beach sampling, in
September 2025; the city pilots Tidepool's alerts in the 2026 beach season. Its code host mails notifications for
issues and pull requests; most of the talk is in a direct chat with Rafael.
"""
from datetime import date, timedelta

from igneous.persona.cast import ME, at, days, p
from igneous.persona.life import Life, Task, TaskList

FORGE = ("Forge", "notifications@code.example.org")
REMOTE = "https://code.example.org/mira-c/tidepool.git"
ROOT = "Projects/tidepool"


def d(y: int, m: int, day: int) -> date:
    return date(y, m, day)


def mail(life: Life) -> None:
    rafael, priya, hannah = p("rafael"), p("priya"), p("hannah")
    life.thread("Tidepool: want to build a beach alert app?", [
        (at(d(2025, 2, 8), "11:30"), ME, [rafael],
         "Hey Rafael,\n\nRemember the field-day idea? The city's beach buoys post readings every 30 minutes, but "
         "the advisory page lags a day. I want to build Tidepool: a small open-source app that polls the readings "
         "and texts swimmers when a beach crosses the E. coli threshold. Want to build it with me?\n\nMira"),
        (at(d(2025, 2, 8), "13:02"), rafael, [ME],
         "Absolutely. I'll take the data import if you take the alert rules. Repo tonight?"),
        (at(d(2025, 2, 8), "13:15"), ME, [rafael], "Repo tonight. code.example.org/mira-c/tidepool"),
    ], tag="tidepool.start")
    life.thread("Tidepool CSV importer", [
        (at(d(2025, 3, 21), "17:10"), ME, [rafael],
         "Rafael, the city also publishes the historical samples as one big CSV. Could you add an importer so we "
         "can backfill 2019-2024?"),
        (at(d(2025, 3, 21), "18:40"), rafael, [ME],
         "Sure. I'll finish the CSV importer this weekend, with a dry-run flag so we can check the counts first.",
         (), "tidepool.promise"),
        (at(d(2025, 3, 24), "09:05"), rafael, [ME],
         "Pushed the CSV importer last night: 5,812 historical samples backfilled, dry run matched the city's "
         "totals. PR #9 is merged.", (), "tidepool.confirmed"),
        (at(d(2025, 3, 24), "10:30"), ME, [rafael], "Amazing, thank you! Charts look so much better with history."),
    ])
    life.thread("Introduction: Hannah Brooks (Port Avalon Parks) and Mira Castellanos", [
        (at(d(2025, 9, 18), "11:00"), priya, [ME, hannah],
         "Hannah, Mira,\n\nHannah: Mira works with us part-time and built Tidepool, an open-source app that turns "
         "your beach sampling readings into alerts for swimmers. Mira: Hannah coordinates the city's beach water "
         "quality program and was asking about faster advisories at our last meeting.\n\nI think you should talk!"
         "\n\nPriya", (), "intro.hannah"),
    ], quote=False)
    life.thread("Tidepool and the city's sampling data", [
        (at(d(2025, 9, 22), "14:20"), ME, [hannah],
         "Hi Hannah,\n\nThanks to Priya for connecting us. Tidepool currently scrapes the public advisory page. "
         "Could we use the raw 2025 sampling data instead, so alerts go out as soon as a lab result is posted?\n\n"
         "Mira"),
        (at(d(2025, 9, 23), "09:15"), hannah, [ME],
         "Hi Mira,\n\nHappy to help. I'll send you the 2025 beach sampling export by the end of the month, and we "
         "can talk about a direct feed for next season.\n\nHannah Brooks\nBeach Water Quality Coordinator, City of "
         "Port Avalon", (), "hannah.promise"),
        (at(d(2025, 9, 30), "16:45"), hannah, [ME],
         "Hi Mira,\n\nAs promised, here's the 2025 beach sampling export (attached): 1,204 E. coli samples from six "
         "beaches, May 19 to September 5. Avalon North had the most exceedances (23).\n\nHannah", (),
         "hannah.data"),
        (at(d(2025, 9, 30), "18:02"), ME, [hannah], "Thank you, Hannah! I'll show you what Tidepool does with it at "
         "Civic Hack Night on November 6."),
    ])
    life.thread("Tidepool pilot for the 2026 beach season", [
        (at(d(2026, 4, 28), "10:10"), hannah, [ME, rafael],
         "Mira, Rafael,\n\nThe department approved a Tidepool pilot for the 2026 beach season: we'll post lab "
         "results to your feed the same afternoon and link Tidepool from the advisory page, May 18 to September 7."
         "\n\nHannah"),
        (at(d(2026, 4, 28), "11:00"), ME, [hannah, rafael], "Wonderful news! We'll have SMS alerts ready by May 15."),
        (at(d(2026, 9, 14), "15:00"), hannah, [ME, rafael],
         "Pilot results: Tidepool sent 312 alerts to 1,480 subscribers this season, on average 19 hours before the "
         "old advisory page updated. We'd like to keep it next year.\n\nHannah", (), "tidepool.pilot"),
        (at(d(2026, 9, 14), "17:30"), rafael, [hannah, ME], "That's incredible. Thank you for trusting us with it!"),
    ])
    hack = life.event("Port Avalon Civic Hack Night: Tidepool demo", at(d(2025, 11, 6), "18:30"), 150,
                      location="Port Avalon Public Library", attendees=(rafael, hannah), tag="tidepool.demo")
    _ = hack


def notifications(life: Life) -> None:
    """The code host's mail: issues and pull requests on Tidepool."""
    rng = life.rng("forge")
    titles = ["Add Avalon South beach", "Alert fires twice for the same sample", "Map tiles blank on Safari",
              "Use the city's lab feed instead of scraping", "Backfill historical samples from CSV",
              "Unsubscribe link in SMS", "Show the last sample's age on the map", "Dark mode",
              "Rate-limit alerts to one per beach per day", "Spanish translations", "Docs: how to run locally",
              "Bump Python to 3.12", "Flaky test in test_alerts.py", "Add Lake Avalon wind data"]
    when = d(2025, 2, 12)
    n = 1
    while when <= d(2026, 10, 15):
        if not (d(2025, 6, 1) <= when <= d(2025, 8, 20)):
            title = titles[(n - 1) % len(titles)]
            kind = rng.choice(["Issue", "Pull request"])
            who = rng.choice(["rafael-o", "mira-c", "hbrooks-pa", "a-contributor"])
            life.mail(at(when, f"{rng.randint(8, 23):02d}:{rng.randint(0, 59):02d}"), FORGE, [ME],
                      f"[mira-c/tidepool] {kind} #{n}: {title}",
                      f"{who} opened {kind.lower()} #{n} in mira-c/tidepool:\n\n{title}\n\nReply to this email or "
                      f"view it on Forge: https://code.example.org/mira-c/tidepool/{'issues' if kind == 'Issue' else 'pull'}"
                      f"/{n}\n\nYou are receiving this because you own the repository.")
            n += 1
        when += timedelta(days=rng.randint(4, 15))


def chat(life: Life) -> None:
    rafael = p("rafael")
    life.space("dm-rafael", "", [ME, rafael])
    rng = life.rng("dm-rafael")
    tidepool = [
        [(rafael, "pushed the poller, it checks the feed every 10 min"), (ME, "nice, alert rules next")],
        [(ME, "Tidepool sent its first real alert today!! Avalon North"), (rafael, "🌊🚨")],
        [(rafael, "should Tidepool text at night?"), (ME, "no, queue them for 7am"), (rafael, "ok")],
        [(ME, "someone opened an issue for Spanish translations"), (rafael, "I can do those")],
        [(rafael, "map tiles are blank on safari"), (ME, "it's the CSP header, fixing")],
        [(ME, "Hannah wants a direct feed for Tidepool next season"), (rafael, "that's huge")],
        [(rafael, "Tidepool has 300 subscribers"), (ME, "what!! from the hack night?"), (rafael, "yes")],
        [(ME, "Tidepool v0.3 SMS is live"), (rafael, "tested on my phone, works")],
    ]
    other = [
        [(rafael, "did you do the reading for tomorrow?"), (ME, "half of it"), (rafael, "same")],
        [(ME, "lunch at the union?"), (rafael, "12:30")],
        [(rafael, "climbing tonight?"), (ME, "can't, work"), (rafael, "next week then")],
        [(ME, "happy birthday!!"), (rafael, "thank you 🎂")],
        [(rafael, "are you watching the game"), (ME, "no I'm debugging lol")],
        [(ME, "how was your interview?"), (rafael, "good I think! second round next week")],
    ]
    active = [day for day in days(d(2025, 2, 9), d(2026, 10, 25))
              if not (d(2025, 6, 1) <= day <= d(2025, 8, 20)) and rng.random() < 0.34]
    for day in active:
        lines = rng.choice(tidepool if rng.random() < 0.5 else other)
        life.chat("dm-rafael", at(day, f"{rng.randint(12, 23):02d}:{rng.randint(0, 59):02d}"), lines)


def ai(life: Life) -> None:
    sessions = [
        (d(2025, 2, 9), "Polling a CSV feed for alerts", "I'm building Tidepool, a small Python app that polls a "
         "city's beach water-quality feed every 10 minutes and alerts subscribers when E. coli crosses 235 "
         "CFU/100 mL. How should I structure the polling so I never alert twice for one sample?",
         "Key each reading by (beach, sample time) and store the last one you processed per beach. On each poll, "
         "fetch, keep only readings newer than that, evaluate the threshold, enqueue alerts, then advance the "
         "watermark in the same transaction. Re-running a poll then changes nothing."),
        (d(2025, 2, 16), "Leaflet map of beaches", "For Tidepool, what's the simplest way to show six beaches on a "
         "map with a colored marker for each one's latest reading?",
         "Leaflet with OpenStreetMap tiles: one circleMarker per beach, colored green, amber or red by the latest "
         "reading, with a popup showing the value and the sample's age. Serve the readings as GeoJSON from one "
         "endpoint."),
        (d(2025, 4, 20), "Announcing an open-source project", "Write a short announcement for Tidepool v0.1, an "
         "open-source app that alerts swimmers when a Lake Avalon beach has high E. coli.",
         "Tidepool v0.1 is out: free text alerts when a Lake Avalon beach crosses the E. coli threshold, usually "
         "the same day the sample comes back instead of the next morning. It's open source (MIT), and the map "
         "shows every beach's latest reading and how old it is."),
        (d(2025, 10, 12), "Designing a data feed contract", "The city will give Tidepool a direct feed of lab "
         "results. What should the data contract cover?",
         "Fields and units (beach id, sample time, result, method), when results are posted and corrected, how "
         "a correction is marked, an id per sample, the feed's uptime expectations, and who to call when it "
         "breaks."),
        (d(2026, 5, 10), "SMS alerts on a budget", "Tidepool needs to text about 1,500 subscribers during a beach "
         "season. How do I keep SMS costs and spam complaints down?",
         "Send one alert per beach per day at most, only to people subscribed to that beach, never at night, "
         "with STOP handling and a short link instead of long text. Budget roughly a cent per message plus the "
         "number's monthly fee."),
    ]
    rng = life.rng("tidepool-ai")
    for day, title, q, a in sessions:
        t = at(day, f"{rng.randint(19, 23):02d}:{rng.randint(0, 59):02d}")
        life.ask(title, [(t, q), (t + timedelta(seconds=30), a)], tag="tidepool.ai" if day == d(2025, 2, 9) else None)


def repository(life: Life) -> None:
    """The git checkout: its code, docs and history, and a .git folder with the remote the files connector reads."""
    doc = life.doc
    doc(f"{ROOT}/.git/config", at(d(2025, 2, 8), "21:05"),
        "[core]\n\trepositoryformatversion = 0\n\tfilemode = true\n\tbare = false\n\tlogallrefupdates = true\n"
        f'[remote "origin"]\n\turl = {REMOTE}\n\tfetch = +refs/heads/*:refs/remotes/origin/*\n'
        '[branch "main"]\n\tremote = origin\n\tmerge = refs/heads/main\n')
    doc(f"{ROOT}/.git/HEAD", at(d(2025, 2, 8), "21:05"), "ref: refs/heads/main\n")
    doc(f"{ROOT}/README.md", at(d(2025, 10, 20), "22:10"),
        "# Tidepool\n\nFree alerts when a Lake Avalon beach has high E. coli, usually the same day the sample comes "
        "back.\n\nTidepool polls the City of Port Avalon's beach sampling feed, keeps every beach's latest reading, "
        "and texts subscribers when a reading crosses 235 CFU/100 mL. A map shows each beach and how old its last "
        "sample is.\n\nBuilt by Mira Castellanos and Rafael Ortiz. MIT licensed.\n\n## Run it\n\n```\npip install "
        "-e .\ntidepool poll --once\n```\n", tag="doc.tidepool-readme")
    doc(f"{ROOT}/pyproject.toml", at(d(2025, 10, 20), "22:12"),
        '[project]\nname = "tidepool"\nversion = "0.2.0"\nrequires-python = ">=3.12"\ndependencies = ["httpx", '
        '"pydantic"]\n\n[project.scripts]\ntidepool = "tidepool.cli:main"\n')
    doc(f"{ROOT}/tidepool/__init__.py", at(d(2025, 2, 9), "23:40"), '"""Tidepool: beach water-quality alerts."""\n'
        '__version__ = "0.2.0"\n')
    doc(f"{ROOT}/tidepool/feeds.py", at(d(2025, 10, 18), "20:30"),
        '"""Readings from the city\'s lab feed (and, before October 2025, the advisory page)."""\nfrom dataclasses '
        "import dataclass\nfrom datetime import datetime\n\nTHRESHOLD = 235  # E. coli, CFU per 100 mL\n\n\n"
        "@dataclass(frozen=True)\nclass Reading:\n    beach: str\n    sampled_at: datetime\n    ecoli: float\n\n"
        "    @property\n    def unsafe(self) -> bool:\n        return self.ecoli >= THRESHOLD\n")
    doc(f"{ROOT}/tidepool/alerts.py", at(d(2025, 4, 18), "23:15"),
        '"""Alert rules: at most one alert per beach per day, never at night (queued for 7am)."""\nfrom datetime '
        "import time\n\nQUIET = (time(21, 0), time(7, 0))\n\n\ndef due(reading, last_alert_day):\n"
        "    return reading.unsafe and reading.sampled_at.date() != last_alert_day\n")
    doc(f"{ROOT}/tests/test_alerts.py", at(d(2025, 4, 18), "23:20"),
        "from datetime import date, datetime\n\nfrom tidepool.alerts import due\nfrom tidepool.feeds import Reading\n"
        "\n\ndef test_one_alert_per_beach_per_day():\n    r = Reading('avalon-north', datetime(2025, 7, 1, 9), 410)\n"
        "    assert due(r, None) and not due(r, date(2025, 7, 1))\n")
    doc(f"{ROOT}/docs/2025-02-11-design.md", at(d(2025, 2, 11), "22:00"),
        "# Tidepool design (Feb 2025)\n\nPoll the advisory page every 10 minutes; one watermark per beach; alerts "
        "go through a queue so a crash never sends twice. Later: the city's raw lab feed.\n")
    doc(f"{ROOT}/docs/roadmap.md", at(d(2026, 1, 25), "16:00"),
        "# Tidepool roadmap\n\n- [x] v0.1: polling, map, email alerts (April 2025)\n- [x] v0.2: city lab feed, "
        "historical backfill (October 2025)\n- [ ] v0.3: SMS alerts, Spanish (for the 2026 pilot)\n- [ ] wind "
        "and rain overlays\n")
    doc(f"{ROOT}/docs/city-pilot-2026.md", at(d(2026, 9, 15), "10:30"),
        "# City pilot, 2026 beach season\n\nMay 18 - September 7. 312 alerts to 1,480 subscribers, on average 19 "
        "hours before the advisory page. The city wants to keep it in 2027.\n", tag="doc.tidepool-pilot")
    doc(f"{ROOT}/CHANGELOG.md", at(d(2026, 5, 14), "21:00"),
        "# Changelog\n\n## 0.3.0 (2026-05-14)\nSMS alerts, Spanish translations, quiet hours.\n\n## 0.2.0 "
        "(2025-10-20)\nThe city's lab feed; historical backfill from CSV.\n\n## 0.1.0 (2025-04-26)\nFirst release: "
        "polling, map, email alerts.\n")
    life.task_lists.append(TaskList("Tidepool", [
        Task("Set up repo and CI", at(d(2025, 2, 8), "13:20"), d(2025, 2, 10), at(d(2025, 2, 9), "23:45")),
        Task("Alert rules + tests", at(d(2025, 2, 12), "10:00"), d(2025, 4, 18), at(d(2025, 4, 18), "23:30")),
        Task("Release v0.1", at(d(2025, 4, 19), "11:00"), d(2025, 4, 26), at(d(2025, 4, 26), "15:00")),
        Task("Switch to the city's lab feed", at(d(2025, 10, 1), "09:00"), d(2025, 10, 20),
             at(d(2025, 10, 20), "22:15")),
        Task("Demo at Civic Hack Night", at(d(2025, 10, 1), "09:05"), d(2025, 11, 6), at(d(2025, 11, 6), "21:00")),
        Task("SMS alerts for the pilot", at(d(2026, 4, 28), "11:10"), d(2026, 5, 15), at(d(2026, 5, 14), "21:05")),
    ], tag="tasks.tidepool"))
    rng = life.rng("tidepool-search")
    for day, query in [(d(2025, 2, 9), "leaflet circlemarker color by value"),
                       (d(2025, 4, 17), "epa beach action value e coli 235"),
                       (d(2026, 5, 9), "sms api stop keyword compliance")]:
        life.search(at(day, f"{rng.randint(10, 23):02d}:{rng.randint(0, 59):02d}"), "Search",
                    f"Searched for {query}", "https://www.google.com/search?q=" + query.replace(" ", "+"))


def write(life: Life) -> None:
    mail(life)
    notifications(life)
    chat(life)
    ai(life)
    repository(life)
