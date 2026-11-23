"""The job: Northwind Robotics, a small Port Avalon company whose Heron boats sample lake water on their own.

Ruth Adeyemi introduces Mira to Owen Brandt in November 2024; she starts part-time in January 2025 (Tuesdays and
Thursdays at the office), works full-time through summer 2025, when the Heron field trials run on Lake Avalon, goes
back to part-time in the fall, accepts a full-time offer in March 2026 and starts it in July. Colleagues write to
her school address and talk in the team's chat space, where she is an external member; standups and 1:1s are on
Meet. Once she has a Northwind account (July 2026), work moves there, out of these exports.
"""
from datetime import date, timedelta

from igneous.persona.cast import (
    FULL_TIME_START,
    JOB_START,
    ME,
    SUMMER_END,
    SUMMER_START,
    TERMS,
    at,
    days,
    p,
)
from igneous.persona.life import Life, Task, TaskList

PAYSTUB = ("Paystub", "statements@paystub.example.com")
LAST_CHAT_DAY = date(2026, 5, 29)  # her last part-time day: from July 6 she works from a Northwind account

COMPONENTS = ["the telemetry parser", "the buoy dashboard", "the mission planner", "Wavelink ingest",
              "the alert rules", "the battery model", "the OTA updater", "the sample-log exporter"]
# Part-time afternoons, on the weekdays her classes leave free each semester.
PART_TIME = [(date(2025, 1, 21), date(2025, 5, 9), (1, 3)), (date(2025, 8, 25), date(2025, 12, 12), (0, 4)),
             (date(2026, 1, 12), LAST_CHAT_DAY, (0, 4))]


def d(y: int, m: int, day: int) -> date:
    return date(y, m, day)


def shift(day: date) -> str | None:
    """Whether Mira works that day: "full" (summer 2025, and from July 2026), "part" (an afternoon) or None."""
    if day.weekday() >= 5 or day < JOB_START:
        return None
    if SUMMER_START <= day <= SUMMER_END or day >= FULL_TIME_START:
        return "full"
    on_break = not any(t.teaching(day) for t in TERMS) and day.month not in (5,)
    for start, end, weekdays in PART_TIME:
        if start <= day <= end and day.weekday() in weekdays and not on_break:
            return "part"
    return None


def hiring(life: Life) -> None:
    ruth, owen, lucia = p("adeyemi"), p("owen"), p("lucia")
    life.thread("Introduction: Mira Castellanos, meet Owen Brandt (Northwind Robotics)", [
        (at(d(2024, 11, 6), "16:05"), ruth, [ME, owen],
         "Owen, Mira,\n\nI'd like to introduce you two.\n\nOwen: Mira is a junior in computer science with an "
         "environmental science minor and one of my advisees. This fall she's been digging into the city's beach "
         "sampling data for a paper on algal blooms, on her own initiative.\n\nMira: Owen was my student about ten "
         "years ago and co-founded Northwind Robotics, which builds autonomous boats that sample lake water. He told "
         "me last week they're looking for a part-time software intern.\n\nI think you should talk. I'll let you "
         "take it from here.\n\nRuth", (), "intro.owen"),
    ], quote=False)
    life.thread("Coffee next week?", [
        (at(d(2024, 11, 7), "09:12"), owen, [ME],
         "Hi Mira,\n\nThanks to Ruth for connecting us (moving her to bcc). Would you like to get coffee next week? "
         "Harbor Street Coffee, Wednesday at 4?\n\nOwen"),
        (at(d(2024, 11, 7), "11:48"), ME, [owen],
         "Hi Owen,\n\nWednesday at 4 at Harbor Street Coffee works. Looking forward to it!\n\nMira"),
        (at(d(2024, 11, 14), "08:30"), owen, [ME],
         "Great talking yesterday. As promised, here's the role: part-time on the telemetry team, about 15 hours a "
         "week during the semester, full-time next summer. Interested?\n\nOwen"),
        (at(d(2024, 11, 14), "12:05"), ME, [owen], "Very interested! What are the next steps?\n\nMira"),
        (at(d(2024, 11, 14), "13:20"), owen, [ME],
         "A 90-minute technical interview with me and Priya Raman, our senior engineer. I'll send an invite for "
         "early December.\n\nOwen"),
    ])
    life.ask("Preparing for a robotics startup interview", [
        (at(d(2024, 11, 30), "21:05"), "I have a 90-minute technical interview at Northwind Robotics on Tuesday, a "
         "small company that builds autonomous boats that sample lake water. The role is part-time on their "
         "telemetry team. What should I prepare?"),
        (at(d(2024, 11, 30), "21:06"), "Expect a practical exercise (parsing or cleaning sensor data, a small "
         "service), questions about a project you built, and how you debug. Review how telemetry flows (devices, a "
         "message broker such as MQTT, parsing, storage, dashboards), have one story about a bug you tracked down, "
         "and prepare questions: how they test on the water, and what a good first month looks like."),
        (at(d(2024, 11, 30), "21:15"), "What questions should I ask them at the end?"),
        (at(d(2024, 11, 30), "21:16"), "What a typical week looks like for the part-time role, how code reaches "
         "the boats, what broke last field season, and who you would work with most."),
    ], tag="northwind.prep")
    life.doc("Career/northwind-interview-prep.md", at(d(2024, 12, 1), "17:30"),
             "# Northwind Robotics interview prep (Tue Dec 3, 2pm, Owen Brandt + Priya Raman)\n\n- Northwind builds "
             "Heron, an autonomous boat that samples lake water; buoys report every 10 min\n- Telemetry: MQTT -> "
             "parser -> database -> dashboard\n- My story: the shell pipe bug from CS 341 P1\n- Project: the beach "
             "data paper for ENVS 210\n- Ask: how code reaches the boats, what broke last season, first month\n")
    interview = life.event("Interview: Mira Castellanos / Northwind", at(d(2024, 12, 3), "14:00"), 90,
                           location="Google Meet", attendees=(owen, p("priya")), organizer=owen)
    life.mail(at(d(2024, 11, 20), "10:02"), owen, [ME], "Invitation: Interview: Mira Castellanos / Northwind @ "
              "Tue Dec 3, 2024 2pm - 3:30pm (EST) (mcastellanos@lakeshore.example.edu)",
              "Owen Brandt has invited you to an event.\n\nInterview: Mira Castellanos / Northwind\nTuesday Dec 3, "
              "2024 2pm - 3:30pm (Eastern Time - New York)\nJoining info: Google Meet\nGuests: Owen Brandt "
              "(organizer), Priya Raman, mcastellanos@lakeshore.example.edu\n\nInvitation from Google Calendar",
              calendar=ics_request(interview))
    life.meet(interview.start, 88, event=interview)
    offer = life.thread("Northwind: part-time offer", [
        (at(d(2024, 12, 5), "17:40"), owen, [ME],
         "Hi Mira,\n\nPriya and I loved the interview, especially how you debugged the parser exercise out loud. "
         "We'd like to offer you the part-time software engineering internship: 15 hours a week from January, "
         "$28/hour, full-time over the summer. I'll get you the written offer by Thursday.\n\nOwen", (),
         "offer.promise"),
        (at(d(2024, 12, 5), "19:02"), ME, [owen],
         "Owen, thank you! I'm thrilled. I'll look out for the letter.\n\nMira"),
        (at(d(2024, 12, 12), "11:15"), lucia, [ME],
         "Hi Mira,\n\nAs Owen mentioned, here is your offer letter for the part-time software engineering "
         "internship (attached): $28/hour, about 15 hours a week, starting Tuesday, January 21, 2025. Sign it "
         "through the link by December 20.\n\nWelcome!\nLucia Moretti\nPeople & Operations, Northwind Robotics",
         (owen,), "offer.letter"),
        (at(d(2024, 12, 12), "15:30"), ME, [lucia], "Hi Lucia,\n\nSigned and returned. See you January 21!\n\nMira",
         (owen,)),
    ])
    _ = offer
    life.doc("Work/Northwind/Northwind offer letter 2024.pdf", at(d(2024, 12, 12), "15:20"), [
        ["Northwind Robotics", "22 Pier Rd, Port Avalon", "", "December 12, 2024", "", "Dear Mira Castellanos,", "",
         "We are pleased to offer you the position of Software Engineering Intern (part-time) at Northwind",
         "Robotics, starting Tuesday, January 21, 2025. You will work about 15 hours a week during the academic",
         "semester and full-time from June 2 to August 15, 2025, at $28.00 per hour, reporting to Priya Raman.", "",
         "Sincerely,", "Owen Brandt, Co-founder & CTO"]], tag="doc.offer-2024")


def onboarding(life: Life) -> None:
    lucia, priya, owen = p("lucia"), p("priya"), p("owen")
    life.thread("Your first day at Northwind", [
        (at(d(2025, 1, 16), "14:00"), lucia, [ME],
         "Hi Mira,\n\nYour first day is Tuesday, January 21. Come to 22 Pier Rd at 1pm; I'll have your badge and "
         "laptop ready. Priya will be your mentor. Bring an ID for the payroll forms.\n\nLucia"),
        (at(d(2025, 1, 16), "16:45"), ME, [lucia], "Perfect, see you at 1 on Tuesday!\n\nMira"),
    ])
    life.thread("Hello from your mentor", [
        (at(d(2025, 1, 20), "09:10"), priya, [ME],
         "Hi Mira,\n\nI'm Priya, I'll be your mentor. Tomorrow we'll set up your environment and I'll walk you "
         "through Wavelink, our telemetry pipeline: buoys and Heron boats publish over MQTT, Wavelink parses and "
         "stores readings, and the dashboard shows them. Your first project: the parser for the new sensor "
         "packets.\n\nI added you to the team's chat space.\n\nPriya"),
        (at(d(2025, 1, 20), "10:02"), ME, [priya], "Hi Priya, sounds great. See you tomorrow!\n\nMira"),
    ])
    life.chat("northwind", at(JOB_START, "13:40"), [
        (owen, "Everyone, please welcome Mira to Northwind! She's joining the telemetry team part-time, Tuesdays and "
               "Thursdays."),
        (priya, "welcome Mira!! 🎉"), (p("jonah"), "welcome! come see the boats sometime"),
        (ME, "thank you all, so happy to be here")], tag="northwind.welcome")
    life.doc("Work/Northwind/onboarding-notes.md", at(d(2025, 1, 23), "18:10"),
             "# Northwind onboarding notes\n\n- Wavelink: MQTT -> parser -> Postgres/Timescale -> dashboard\n"
             "- Buoys report every 10 min; Heron boats every 2 s while on a mission\n- Deploys: Tuesdays after "
             "standup\n- My first project: parser for the v3 sensor packets (protobuf)\n- Mentor: Priya; field "
             "questions: Jonah; firmware: Alex Chen\n")
    life.doc("Work/Northwind/wavelink-parser-design.md", at(d(2025, 2, 20), "17:40"),
             "# Wavelink v3 packet parser\n\nProtobuf with a oneof per sensor; unknown fields kept and logged.\n"
             "Backwards compatible with v2 packets for the old buoys.\nShipped behind a flag on 2025-03-04; "
             "Priya reviewed.\n")
    _ = owen


def field_trials(life: Life) -> None:
    """Summer 2025: the Heron field trials on Lake Avalon, the burst a work thread is found by."""
    jonah, priya, owen, alex = p("jonah"), p("priya"), p("owen"), p("nwchen")
    life.thread("Heron field trials: schedule", [
        (at(d(2025, 5, 28), "15:20"), jonah, [ME, priya, alex],
         "Team,\n\nHeron field trials start Monday June 9 at the Avalon North Beach launch. Three weeks of "
         "missions, Mon/Wed/Fri, launch at 7am. Mira: you're on the telemetry laptop; bring the dashboard on the "
         "big screen.\n\nJonah", (), "heron.schedule"),
        (at(d(2025, 5, 28), "16:02"), ME, [jonah, priya, alex], "Got it, I'll have the dashboard ready. Do we have "
         "shore power at the launch?"),
        (at(d(2025, 5, 28), "16:30"), jonah, [ME, priya, alex], "Generator only. Bring the spare battery pack."),
    ])
    life.thread("Heron trial 4: depth readings drifting", [
        (at(d(2025, 6, 18), "13:05"), ME, [priya, alex],
         "Priya, Alex,\n\nOn today's Heron mission the depth readings drifted by up to 40 cm over the run while "
         "the GPS track was steady. Plot attached. Is that the sensor warming up?\n\nMira", (), "heron.drift"),
        (at(d(2025, 6, 18), "14:40"), alex, [ME, priya],
         "Probably the pressure sensor's temperature drift. Firmware 2.3 compensates; Heron 2 is still on 2.1. "
         "I'll flash it tonight."),
        (at(d(2025, 6, 18), "15:12"), priya, [ME, alex],
         "And Mira: let's filter it anyway. A small Kalman filter on depth would hide the jitter too. Want to "
         "take it?"),
        (at(d(2025, 6, 18), "15:30"), ME, [priya, alex], "Yes! I'll have a draft by Friday's mission."),
    ])
    life.thread("Heron field trials: wrap-up", [
        (at(d(2025, 7, 3), "17:00"), jonah, [ME, priya, alex, owen],
         "Field trials done: 9 missions, 214 water samples, zero lost boats. The Kalman filter Mira wrote cut "
         "depth noise by 70%. Debrief Monday at 10.\n\nJonah", (), "heron.wrapup"),
        (at(d(2025, 7, 3), "17:22"), owen, [jonah, ME, priya, alex], "Great work, everyone. Pizza at the debrief."),
    ])
    trial_days = [x for x in days(d(2025, 6, 9), d(2025, 6, 27), "MWF")]
    rng = life.rng("heron")
    for i, day in enumerate(trial_days):
        life.visit("beach", at(day, "06:45"), rng.randint(200, 280))
        life.chat("northwind", at(day, "07:05"), [
            (jonah, f"Heron in the water, mission {i + 1}"),
            (ME, rng.choice(["telemetry looks good", "dashboard is live on the big screen",
                             "packets every 2s, all green"])),
            (alex, rng.choice(["👍", "battery at 96%", "nice"]))], gap=(1, 4))
    log = "\n".join(f"- {day:%a %b %d}: mission {i + 1}, {rng.randint(18, 30)} samples, wind {rng.randint(3, 14)} kt"
                    + (", depth drift (sensor fw 2.1)" if i == 4 else "") for i, day in enumerate(trial_days))
    life.doc("Work/Northwind/heron-field-trial-log.md", at(d(2025, 7, 3), "18:30"),
             "# Heron field trials, Lake Avalon, June 2025\n\n" + log + "\n\nTotal: 214 samples. Kalman filter on "
                                                                         "depth from mission 6.\n",
             tag="doc.heron-log")
    life.doc("Work/Northwind/depth-kalman.py", at(d(2025, 6, 20), "22:10"),
             '"""Heron depth smoothing: a 1-D Kalman filter over pressure-sensor depth readings."""\n\n\n'
             "def smooth(readings, q=0.002, r=0.04):\n    x, p = readings[0], 1.0\n    out = []\n"
             "    for z in readings:\n        p += q\n        k = p / (p + r)\n        x += k * (z - x)\n"
             "        p *= 1 - k\n        out.append(x)\n    return out\n")
    life.event("Heron field trials", at(d(2025, 6, 9), "07:00"), 240, location="Avalon North Beach launch",
               attendees=(jonah, priya, alex), organizer=jonah,
               rrule="FREQ=WEEKLY;BYDAY=MO,WE,FR;UNTIL=20250627T235900Z")
    life.mail(at(d(2025, 6, 2), "08:00"), jonah, [ME], "Invitation: Heron field trials @ Weekly on Monday, "
              "Wednesday, Friday from 7am to 11am (EDT) (mcastellanos@lakeshore.example.edu)",
              "Jonah Feld has invited you to an event.\n\nHeron field trials\nWeekly on Monday, Wednesday, Friday "
              "from 7am to 11am, June 9 - June 27, 2025\nWhere: Avalon North Beach launch\n\nInvitation from Google "
              "Calendar")
    ask = life.ask("Kalman filter for noisy depth sensor", tag="heron.ai", turns=[
        (at(d(2025, 6, 18), "20:40"), "I'm smoothing depth readings from a pressure sensor on a small autonomous "
         "boat (the Heron). Readings jitter by a few cm and drift slowly as the sensor warms. Is a 1D Kalman filter "
         "a good fit?"),
        (at(d(2025, 6, 18), "20:41"), "Yes for the jitter: a constant-position 1-D Kalman filter with process noise "
         "q and measurement noise r smooths it with no lag tuning beyond the ratio q/r. Slow drift is a bias, not "
         "noise: compensate it (temperature model, or a reference reading at the surface) or add the bias as a "
         "second state."),
        (at(d(2025, 6, 18), "20:55"), "Typical starting values for q and r if readings are in meters at 2 Hz?"),
        (at(d(2025, 6, 18), "20:56"), "Start with r near the sensor's variance (e.g. 0.04 for 20 cm noise) and q "
         "around 0.001-0.005; raise q if the filter lags real depth changes."),
    ])
    _ = ask


def routine(life: Life) -> None:
    """The work weeks: chat in the team space, Meet standups and 1:1s, the odd email, paystubs."""
    owen, priya, jonah, lucia, alex = p("owen"), p("priya"), p("jonah"), p("lucia"), p("nwchen")
    team = [ME, owen, priya, jonah, alex]
    life.space("northwind", "Northwind software", team)
    life.space("dm-priya", "", [ME, priya])
    rng = life.rng("northwind")
    standup = life.event("Northwind standup (remote)", at(JOB_START, "09:30"), 15, location="Google Meet",
                         attendees=(priya, owen, alex, jonah), organizer=priya,
                         rrule="FREQ=WEEKLY;BYDAY=MO,WE,FR;UNTIL=20260702T235900Z")
    one_on_one = life.event("Mira / Priya 1:1", at(d(2025, 1, 30), "16:00"), 30, location="Google Meet",
                            attendees=(priya,), organizer=priya, rrule="FREQ=WEEKLY;INTERVAL=2;BYDAY=TH")
    life.event("Northwind (in office)", at(JOB_START, "13:00"), 300, location="22 Pier Rd",
               rrule="FREQ=WEEKLY;BYDAY=TU,TH;UNTIL=20250530T235900Z")
    ticket = 212
    day = JOB_START
    while day <= d(2026, 10, 30):
        if shift(day) and day <= LAST_CHAT_DAY:
            ticket += rng.randint(0, 3)
            comp = rng.choice(COMPONENTS)
            start = at(day, f"{rng.randint(9, 15):02d}:{rng.randint(0, 59):02d}")
            life.chat("northwind", start, work_day(rng, team, comp, ticket), gap=(2, 25))
            if rng.random() < 0.3:
                life.chat("dm-priya", start + timedelta(hours=rng.randint(1, 3)), mentor_day(rng, comp))
        if JOB_START <= day <= LAST_CHAT_DAY:
            if day.weekday() in (0, 2, 4) and (shift(day) or rng.random() < 0.5) and rng.random() < 0.55:
                life.meet(at(day, "09:30") + timedelta(minutes=rng.randint(0, 3)), rng.randint(9, 17), event=standup)
            if day.weekday() == 3 and (day - d(2025, 1, 30)).days % 14 == 0:
                life.meet(at(day, "16:00"), rng.randint(24, 34), event=one_on_one)
            if day.weekday() == 4 and (day - d(2025, 1, 24)).days % 14 == 0:
                life.mail(at(day, "06:10"), PAYSTUB, [ME], f"Your Northwind Robotics paystub for {day:%B} {day.day}",
                          f"Your paystub for the pay period ending {day:%B} {day.day}, {day.year} is ready.\n\n"
                          "Sign in to Paystub to view it. This message was sent automatically.")
        day += timedelta(days=1)
    mail_beats = [
        (d(2025, 2, 4), priya, "Code review notes on the v3 parser",
         "Hi Mira,\n\nLeft comments on the parser PR. Mostly small; the one that matters: keep unknown fields "
         "instead of dropping them, so a newer buoy never loses data on an older Wavelink.\n\nPriya",
         "Thanks Priya, keeping them now and logging a counter for unknown fields."),
        (d(2025, 3, 11), jonah, "Buoy B-07 offline since Sunday",
         "Mira, B-07 stopped reporting Sunday night. Can you check if Wavelink got anything after 22:00? I'm "
         "deciding whether to send the boat out.\n\nJonah",
         "Last packet 21:58 Sunday, battery at 11%. Looks like it just ran down; the solar panel readings were "
         "flat all weekend."),
        (d(2025, 4, 15), owen, "Can you demo the dashboard Thursday?",
         "Mira, the city parks folks are visiting Thursday at 2. Could you demo the buoy dashboard? 10 minutes.\n\n"
         "Owen",
         "Of course. I'll show the live map and the E. coli overlay."),
        (d(2025, 5, 13), lucia, "Summer schedule",
         "Hi Mira,\n\nFor the summer (June 2 - August 15) you're full-time, 9 to 5:30, in the office. Let me know "
         "about any days off before May 30.\n\nLucia",
         "Thanks Lucia! I'll be out Friday July 25 for my cousin's wedding."),
        (d(2025, 8, 14), owen, "Thank you for a great summer",
         "Mira,\n\nThank you for a great summer: the depth filter, the trial dashboard, and the alert rules are all "
         "in production. We'd like you to keep going part-time this fall, same schedule as spring.\n\nOwen",
         "Thank you, Owen! Yes, I'd love to keep going. Tuesdays and Thursdays again."),
        (d(2025, 10, 7), priya, "Wavelink alert rules: false alarms",
         "Mira, the low-oxygen alerts fired 14 times last week, all on B-03 at night. Can you look at it Thursday?\n\n"
         "Priya",
         "Found it: B-03's DO sensor reads low when the cleaning wiper runs. I'll suppress readings during the wiper "
         "cycle."),
        (d(2025, 12, 16), owen, "Holiday party",
         "Team holiday party Friday Dec 19, 6pm at the Avalon Public Market. Partners welcome.\n\nOwen",
         "I'll be there!"),
        (d(2026, 2, 10), alex, "OTA updater: rollback test",
         "Mira, can you run the rollback test on Heron 3 Thursday? I'll leave the test firmware on the bench.\n\n"
         "Alex", "Will do, I'll post results in the chat."),
    ]
    for day, who, subject, body, reply in mail_beats:
        t = at(day, f"{rng.randint(8, 16):02d}:{rng.randint(0, 59):02d}")
        life.thread(subject, [(t, who, [ME], body), (t + timedelta(minutes=rng.randint(25, 170)), ME, [who], reply)])
    tasks = [Task("Set up Wavelink dev environment", at(d(2025, 1, 21), "13:30"), d(2025, 1, 23),
                  at(d(2025, 1, 23), "17:00")),
             Task("v3 packet parser PR", at(d(2025, 1, 23), "17:30"), d(2025, 2, 14), at(d(2025, 2, 13), "16:40")),
             Task("Dashboard demo for the city", at(d(2025, 4, 15), "12:00"), d(2025, 4, 17),
                  at(d(2025, 4, 17), "15:00")),
             Task("Heron depth Kalman filter", at(d(2025, 6, 18), "15:40"), d(2025, 6, 20),
                  at(d(2025, 6, 20), "22:15"), "q=0.002 r=0.04; test on mission 4 logs"),
             Task("Suppress DO readings during wiper cycle", at(d(2025, 10, 9), "14:00"), d(2025, 10, 16),
                  at(d(2025, 10, 14), "17:00")),
             Task("Sign full-time offer", at(d(2026, 3, 13), "10:00"), d(2026, 3, 20), at(d(2026, 3, 16), "09:30")),
             Task("Return school laptop, set up Northwind account", at(d(2026, 6, 25), "11:00"), d(2026, 7, 6),
                  at(d(2026, 7, 6), "10:00"))]
    life.task_lists.append(TaskList("Northwind", tasks, tag="tasks.northwind"))


def work_day(rng, team, comp: str, ticket: int) -> list:
    me, owen, priya, jonah, alex = team
    a = rng.choice([
        [(priya, f"morning! today: WL-{ticket}, {comp}"), (me, f"on it. I'll pair with you on {comp} after lunch?"),
         (priya, "yes, 2pm")],
        [(jonah, "anyone seeing B-05 report twice?"), (me, "yes, duplicate packets since 10:40, looking"),
         (me, f"fixed in {comp}: dedupe on sequence number. deploying"), (jonah, "thank you!")],
        [(me, f"PR for WL-{ticket} is up ({comp}), small one"), (priya, "approved with one nit"),
         (me, "merged")],
        [(alex, "flashing boat 2 at 3, Wavelink will see a reboot"), (me, "noted, I'll mute the alert"),
         (alex, "👍")],
        [(owen, "reminder: deploy freeze after 4 today"), (me, f"getting WL-{ticket} in before then"),
         (priya, "I'll review now")],
        [(me, f"question: should {comp} retry forever or give up after 5?"),
         (priya, "give up after 5 and raise an alert, otherwise a dead buoy floods the queue"), (me, "makes sense")],
        [(jonah, "boat's back, 26 samples today"), (me, "all 26 in the dashboard"), (owen, "nice")],
        [(priya, f"can someone look at the flaky test in {comp}?"), (me, "it's a timezone thing, the test assumes "
                                                                         "UTC"), (priya, "classic")],
        [(me, "standup notes: finished the export, starting on the battery model"),
         (alex, "battery model needs the new cell curves, sending them"), (me, "thanks")],
        [(owen, "city wants daily summaries by email, who wants it?"), (me, "I'll take it"),
         (owen, f"WL-{ticket + 1}, thanks Mira")],
    ])
    extra = rng.choice([[], [], [(rng.choice([priya, jonah, alex]), rng.choice(
        ["lunch at the market?", "coffee run, anyone?", "the lake is flat today, good sampling weather",
         "heads up, the office wifi restarts at 5"])), (me, rng.choice(["yes!", "in", "😊", "thanks"]))]])
    return a + extra


def mentor_day(rng, comp: str) -> list:
    priya = p("priya")
    return rng.choice([
        [(priya, "how's the week going?"), (ME, f"good, a bit stuck on {comp}"),
         (priya, "bring it to our 1:1, we'll whiteboard it")],
        [(ME, "do you have time to look at my design doc?"), (priya, "tomorrow morning, leave comments?"),
         (ME, "perfect")],
        [(priya, "you did great in the city demo"), (ME, "thank you, I was nervous")],
        [(ME, "should I take the on-call rotation this summer?"),
         (priya, "shadow me for two weeks first, then yes")],
    ])


def return_offer(life: Life) -> None:
    owen, lucia = p("owen"), p("lucia")
    life.thread("Full-time offer", [
        (at(d(2026, 3, 9), "10:30"), owen, [ME],
         "Mira,\n\nWe'd like you to join Northwind full-time after graduation as a Software Engineer on the "
         "Wavelink team, at $96,000 a year. Lucia will send the letter today.\n\nOwen"),
        (at(d(2026, 3, 9), "15:05"), lucia, [ME],
         "Hi Mira,\n\nYour offer letter is attached: Software Engineer, $96,000 a year, starting Monday, July 6, "
         "2026, reporting to Priya Raman. Please reply by March 20.\n\nLucia", (owen,), "offer.fulltime"),
        (at(d(2026, 3, 16), "09:20"), ME, [lucia, owen],
         "Lucia, Owen,\n\nI'm delighted to accept. See you on July 6!\n\nMira"),
    ])
    life.doc("Work/Northwind/Northwind full-time offer 2026.pdf", at(d(2026, 3, 16), "09:10"), [
        ["Northwind Robotics", "22 Pier Rd, Port Avalon", "", "March 9, 2026", "", "Dear Mira Castellanos,", "",
         "We are delighted to offer you the position of Software Engineer at Northwind Robotics, starting",
         "Monday, July 6, 2026, at an annual salary of $96,000, reporting to Priya Raman.", "",
         "Sincerely,", "Lucia Moretti, People & Operations Manager"]])
    life.ask("Negotiating a return offer", [
        (at(d(2026, 3, 10), "21:15"), "I got a full-time return offer from the startup where I interned (software "
         "engineer, $96k, small robotics company). Is it reasonable to ask for more, and how?"),
        (at(d(2026, 3, 10), "21:16"), "It's reasonable to ask once, politely, with a reason: a competing offer, "
         "local market data for new-grad engineers, or the scope you already own. Ask for a specific number or for "
         "something easier for a startup to give (a signing bonus, equity, a review at six months)."),
    ])
    life.thread("Welcome back (full-time)", [
        (at(d(2026, 6, 29), "11:00"), lucia, [ME],
         "Hi Mira,\n\nWelcome back! On Monday July 6 your Northwind account (mira@northwind.example.com) will be "
         "ready; from then on please use it for work, including the chat space. Start at 9.\n\nLucia"),
        (at(d(2026, 6, 29), "12:14"), ME, [lucia], "Can't wait. See you Monday!\n\nMira"),
    ])
    life.say("northwind", at(LAST_CHAT_DAY, "17:40"), ME, "last part-time day! see you all July 6, from my "
                                                           "Northwind account")
    life.say("northwind", at(LAST_CHAT_DAY, "17:44"), p("priya"), "🎉 welcome to the team for real")


def ics_request(e) -> str:
    """A minimal text/calendar REQUEST, as an invitation carries it."""
    stamp = lambda t: t.strftime("%Y%m%dT%H%M%SZ")  # noqa: E731
    attendees = "".join(f"ATTENDEE;CN={a.name};ROLE=REQ-PARTICIPANT:mailto:{a.email}\r\n" for a in e.attendees)
    return ("BEGIN:VCALENDAR\r\nPRODID:-//Google Inc//Google Calendar 70.9054//EN\r\nVERSION:2.0\r\nMETHOD:REQUEST\r\n"
            f"BEGIN:VEVENT\r\nDTSTART:{stamp(e.start)}\r\nDTEND:{stamp(e.end)}\r\nUID:{e.uid}@google.com\r\n"
            f"ORGANIZER;CN={e.organizer.name}:mailto:{e.organizer.email}\r\n{attendees}"
            f"ATTENDEE;CN={ME.name};PARTSTAT=NEEDS-ACTION:mailto:{ME.email}\r\nSUMMARY:{e.summary}\r\n"
            "END:VEVENT\r\nEND:VCALENDAR\r\n")


def write(life: Life) -> None:
    hiring(life)
    onboarding(life)
    field_trials(life)
    routine(life)
    return_offer(life)
