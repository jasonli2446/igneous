"""The volume of an ordinary life: weekly lecture notes, the short mail that keeps a class or a job running, a
ChatGPT question most days of the week, Sunday video calls home, and a friend's chat. None of it is a story, and
all of it is what the stories stand out from: a term that shows up everywhere all the time is background, not a
thread.
"""
from datetime import date, timedelta

from igneous.persona import work
from igneous.persona.cast import COURSES, FIRST_DAY, LAST_DAY, ME, TERMS, at, days, p
from igneous.persona.life import Life

# ---- ChatGPT, most weeks ----

# (when it comes up: "school" in term time, "work" while employed, "any"), title, question, answer
ASKS = [
    ("school", "Big-O of nested loops", "Is a loop over i and an inner loop from i to n O(n^2) or O(n log n)?",
     "O(n^2): the inner loop runs n - i times, and the sum over i is n(n+1)/2."),
    ("school", "Proof by induction help", "How do I set up a strong induction proof that every integer above 1 is a "
     "product of primes?", "Base case 2. Assume it holds for every k < n. Either n is prime, or n = ab with both "
     "factors smaller than n, and each of those is a product of primes by the hypothesis."),
    ("school", "Citing a dataset in APA", "How do I cite a city government dataset in APA style?",
     "Author (the agency), year, title in italics, [Data set], publisher if different, and the URL: City of Port "
     "Avalon. (2024). Beach sampling results [Data set]. https://..."),
    ("school", "Study plan for finals week", "I have three finals in four days. How should I split my studying?",
     "Start with the exam whose material you know least, interleave subjects in 50-minute blocks, do practice exams "
     "under time, and sleep: memory consolidates overnight."),
    ("school", "Explaining a confidence interval", "What does a 95% confidence interval actually mean?",
     "If you repeated the sampling many times, 95% of the intervals built this way would contain the true value. "
     "It is a statement about the method, not a 95% chance for this one interval."),
    ("school", "Git rebase vs merge for a team project", "Should our class project team rebase or merge?",
     "Merge for shared branches, rebase your own feature branch before opening a pull request; never rebase "
     "commits someone else has pulled."),
    ("school", "Office hours question", "How do I ask a TA for help without sounding like I want the answer?",
     "Show what you tried, the exact error or wrong output, and your current hypothesis; ask whether your "
     "hypothesis is on the right track."),
    ("school", "Writing a lab report discussion", "What goes in the discussion section of a lab report?",
     "What the results mean, how they compare with expectations and the literature, sources of error and how "
     "big they could be, and what you'd change next time."),
    ("work", "Protobuf field numbers", "Can I reuse a protobuf field number after deleting a field?",
     "No: mark it reserved, so old messages that still carry it are never misread as the new field."),
    ("work", "MQTT QoS levels", "What's the difference between MQTT QoS 1 and QoS 2 for sensor telemetry?",
     "QoS 1 delivers at least once (duplicates possible, so make processing idempotent); QoS 2 exactly once, at "
     "twice the handshakes. For telemetry, QoS 1 plus deduplication by sequence number is the usual choice."),
    ("work", "Postgres time-series indexes", "Best index for queries by sensor id and time range in Postgres?",
     "A composite B-tree on (sensor_id, time), or BRIN on time if rows arrive in time order and tables are huge; "
     "partition by month once tables reach hundreds of millions of rows."),
    ("work", "Writing a design doc", "Outline for a one-page design doc at a small company?",
     "Problem, goals and non-goals, the proposal, alternatives considered, risks, rollout and how you'll know it "
     "worked."),
    ("work", "Go context cancellation", "How do I stop a Go worker goroutine cleanly on shutdown?",
     "Pass a context.Context, select on ctx.Done() in the loop, and have the caller cancel and then wait on a "
     "WaitGroup."),
    ("work", "Debugging flaky tests", "A test fails 1 in 50 runs in CI. Where do I start?",
     "Look for time, ordering and shared state: real clocks, map iteration order, parallel tests touching the same "
     "row, unflushed buffers. Run it 500 times locally with -count and a fixed seed."),
    ("work", "Asking for feedback from a mentor", "How do I ask my mentor for honest feedback?",
     "Ask about one specific thing (a design review, how you ran a meeting) and what one change would help most; "
     "then act on it and tell them you did."),
    ("work", "Battery state of charge estimation", "Simple way to estimate battery state of charge on a small robot?",
     "Coulomb counting corrected by the voltage curve when the battery rests; temperature shifts the curve, so "
     "calibrate per cell chemistry."),
    ("any", "Meal prep for a busy week", "Five cheap lunches I can prep on Sunday?",
     "Black bean and rice bowls, lentil soup, chicken and roasted vegetable trays, pasta salad with chickpeas, "
     "and egg fried rice."),
    ("any", "Stretching after climbing", "Good stretches after a bouldering session?",
     "Forearm flexor and extensor stretches, shoulder sleeper stretch, hip flexor lunge, and a few minutes of easy "
     "pulling and pushing to cool down."),
    ("any", "Reply to a group email politely", "How do I decline a group dinner invitation without being rude?",
     "Thank them, say you can't make it this time, and suggest another plan if you mean it."),
    ("any", "Plant care", "Why are my basil leaves turning yellow?", "Usually too much water or too little light; "
     "let the top of the soil dry between waterings and move it to the sunniest window."),
    ("any", "Spanish phrasing", "How do I say 'I'm proud of you' warmly in Spanish to my grandmother?",
     "\"Estoy muy orgullosa de ti, abuela\", or \"Te quiero mucho y estoy orgullosa de ti\"."),
    ("any", "Sleep schedule", "How do I fix my sleep schedule after finals?", "Pick a fixed wake time, get morning "
     "light, no naps for a week, and move bedtime earlier by 15 minutes a night."),
    ("any", "Budget for a weekend trip", "How much should I budget for a weekend in Chicago staying with a friend?",
     "Train fare, about $40 a day for food, a museum or two, and transit: roughly $150-200 if the couch is free."),
]


def chatgpt(life: Life) -> None:
    rng = life.rng("chatgpt-routine")
    employed = lambda day: work.JOB_START <= day <= LAST_DAY  # noqa: E731
    for day in days(FIRST_DAY, LAST_DAY):
        if rng.random() > 0.22:
            continue
        in_term = any(t.teaching(day) for t in TERMS)
        pool = [a for a in ASKS if a[0] == "any" or (a[0] == "school" and in_term)
                or (a[0] == "work" and employed(day))]
        _kind, title, question, answer = rng.choice(pool)
        t = at(day, f"{rng.randint(8, 23):02d}:{rng.randint(0, 59):02d}")
        turns = [(t, question), (t + timedelta(seconds=rng.randint(15, 40)), answer)]
        if rng.random() < 0.35:
            turns += [(t + timedelta(minutes=rng.randint(2, 9)), rng.choice(
                ["Thanks! Can you make that shorter?", "Can you give an example?", "What would you do first?"])),
                (t + timedelta(minutes=rng.randint(10, 12)), rng.choice(
                    ["Sure: start with the smallest version that works, then check it against one real case.",
                     "Here's a short version: " + answer.split(". ")[0] + ".",
                     "First, write down what you already know and the one thing you need to decide."]))]
        life.ask(title, turns)


# ---- lecture notes ----

TOPICS = {
    "CS 341": ["processes and fork", "scheduling: MLFQ", "virtual memory and paging", "TLBs", "locks and condition "
               "variables", "semaphores", "deadlock", "file systems: inodes", "journaling"],
    "CS 375": ["linear regression", "regularization", "decision trees", "ensembles and boosting", "evaluation and "
               "leakage", "neural networks", "backpropagation", "fairness"],
    "CS 362": ["relational algebra", "SQL joins", "B+ trees", "hash indexes", "query optimization",
               "transactions", "recovery and ARIES"],
    "CS 452": ["RPC and failure", "MapReduce", "Raft elections", "Raft log replication", "linearizability",
               "sharding", "two-phase commit", "Spanner and TrueTime"],
    "CS 481": ["requirements and user stories", "design reviews", "testing in production", "accessibility",
               "demo preparation"],
}


def lecture_notes(life: Life) -> None:
    rng = life.rng("lecture-notes")
    for c in COURSES:
        topics = TOPICS.get(c.code)
        if not topics:
            continue
        weeks = [day for day in days(c.term.start + timedelta(days=6), c.term.end - timedelta(days=5), "R")]
        chosen = sorted(rng.sample(weeks, min(5, len(topics), len(weeks))))
        for n, (day, topic) in enumerate(zip(chosen, topics, strict=False), 1):
            life.doc(f"School/{c.term.name}/{c.code} {c.title}/notes/week-{n:02d}.md",
                     at(day, f"{rng.randint(16, 22):02d}:{rng.randint(0, 59):02d}"),
                     f"# {c.code} lecture notes: {topic}\n\n"
                     + "\n".join(f"- {line}" for line in rng.sample(NOTE_LINES, 3)) + "\n")


NOTE_LINES = ["the invariant is what the proof rests on: write it down first", "example on the board, copy it into "
              "the notebook", "edge case: empty input", "this will be on the midterm (he said so twice)",
              "read section 4 before Tuesday", "the tradeoff is latency vs throughput", "ask in office hours why the "
              "second case works", "draw the timeline before reasoning about it", "compare with last week's "
              "approach", "homework uses exactly this pattern"]


# ---- the mail that keeps things running ----

WORK_NOTES = [
    ("Notes from today's design review", "Notes from the design review are in the shared doc. Decisions: keep "
     "the ingest queue, add a dead-letter topic, revisit retention in a month."),
    ("Moving my shift to Monday", "Could I swap Thursday for Monday this week? I have a project demo Thursday "
     "afternoon."),
    ("Dashboard access for the city", "The parks team asked for read-only dashboard access. Can you set up two "
     "accounts?"),
    ("Retro: what went well", "Thanks for the retro today. My notes: deploys are calmer since the freeze rule, "
     "alerts still noisy at night."),
    ("Lunch and learn Friday", "I'll do a 20-minute lunch and learn on the depth filter Friday, if people are "
     "interested."),
    ("Spare parts order", "We need two more pressure sensors before the next field day. Can you add them to the "
     "order?"),
    ("PTO request", "I'd like to take Friday off for a family thing. My PRs are all merged."),
    ("Question about the battery model", "Do we have discharge curves for the new cells, or should I fit them from "
     "last month's logs?"),
]
# Most say which class only in passing, if at all: the people and the week make it clear to them.
CLASS_NOTES = [
    ("Homework question", "Quick question on the homework: does part (c) want the worst case or the average case?"),
    ("Missed class today", "I missed {code} today (doctor's appointment). Could you send me what I missed?"),
    ("Notes from Thursday", "Here are my notes from Thursday, the board photos are blurry, sorry."),
    ("Sunday?", "Can we meet Sunday at 4 in the library to split up the next part?"),
    ("Exam room", "Is the exam in the usual room or the big lecture hall?"),
    ("Practice problems", "Did you get the same answer for the third practice problem? I keep getting 42."),
]


def routine_mail(life: Life) -> None:
    rng = life.rng("routine-mail")
    colleagues = [p("priya"), p("owen"), p("jonah"), p("nwchen"), p("lucia")]
    for day in days(work.JOB_START, work.LAST_CHAT_DAY):
        if work.shift(day) and rng.random() < 0.12:
            subject, body = rng.choice(WORK_NOTES)
            them = rng.choice(colleagues)
            mine = rng.random() < 0.5
            sender, to = (ME, them) if mine else (them, ME)
            t = at(day, f"{rng.randint(9, 17):02d}:{rng.randint(0, 59):02d}")
            reply = rng.choice(["Sounds good, thanks!", "Done.", "Yes, no problem.", "Thanks for the heads up.",
                                "Added it, see the doc."])
            life.thread(subject, [(t, sender, [to], f"Hi {to.first},\n\n{body}\n\n{sender.first}"),
                                  (t + timedelta(minutes=rng.randint(20, 240)), to, [sender], reply)])
    for c in COURSES:
        mates = [p(k) for k in c.classmates]
        if not mates:
            continue
        for day in days(c.term.start + timedelta(days=10), c.term.end, "MTWRF"):
            if not c.term.teaching(day) or rng.random() > 0.022 * c.weight:
                continue
            subject, body = rng.choice(CLASS_NOTES)
            mate = rng.choice(mates)
            t = at(day, f"{rng.randint(9, 22):02d}:{rng.randint(0, 59):02d}")
            sender, to = (mate, ME) if rng.random() < 0.5 else (ME, mate)
            life.thread(subject.format(code=c.code), [
                (t, sender, [to], f"Hi {to.first},\n\n{body.format(code=c.code)}\n\n{sender.first}"),
                (t + timedelta(minutes=rng.randint(10, 300)), to, [sender],
                 rng.choice(["Sure!", "Sent.", "Yes, see you then.", "Good question, I think worst case.",
                             "The big lecture hall, per the announcement."]))])


# ---- home and friends ----

def family_calls(life: Life) -> None:
    """Sunday video calls home, on Meet, about every other week."""
    rng = life.rng("family-calls")
    for day in days(FIRST_DAY, LAST_DAY, "U"):
        if rng.random() < 0.55:
            life.meet(at(day, f"{rng.randint(17, 19)}:{rng.choice(['00', '15', '30'])}"), rng.randint(14, 55),
                      counterparts=(p("mom"),), call=True)


def theo_chat(life: Life) -> None:
    theo = p("theo")
    life.space("dm-theo", "", [ME, theo])
    rng = life.rng("dm-theo")
    banks = [
        [(theo, "how's senior year treating you"), (ME, "busy but good. how's Chicago?"), (theo, "cold. great")],
        [(ME, "climbing this weekend if you're in town?"), (theo, "I'm back saturday!")],
        [(theo, "send me that pasta recipe again"), (ME, "it's the one with the chickpeas, sending")],
        [(theo, "work question: do you use protobuf at your job"), (ME, "every day lol"), (theo, "help me")],
        [(ME, "happy birthday Theo!!"), (theo, "thank you!! 🎂")],
        [(theo, "watched that lake documentary you mentioned"), (ME, "the algae one? so good")],
        [(ME, "how's the new apartment"), (theo, "tiny, but there's a view of the river")],
    ]
    for day in days(date(2024, 9, 1), LAST_DAY):
        if rng.random() < 0.14:
            life.chat("dm-theo", at(day, f"{rng.randint(11, 23):02d}:{rng.randint(0, 59):02d}"), rng.choice(banks))


def class_of_2026(life: Life) -> None:
    """The CS class of 2026's group chat: internships, exams, free food, graduation. Busy in term time."""
    me, rafael, leah, jordan, achen, sam, ella = (p(k) for k in ("me", "rafael", "leah", "jordan", "achen", "sam",
                                                                  "ella"))
    life.space("cs26", "Lakeshore CS '26", [me, rafael, leah, jordan, achen, sam, ella])
    rng = life.rng("cs26")
    banks = [
        [(ella, "free pizza in the Hollis atrium, career fair leftovers"), (jordan, "omw"), (sam, "save me a slice")],
        [(leah, "anyone else's internship interview this week?"), (achen, "thursday, nervous"), (me, "you'll be great")],
        [(rafael, "the printers in Marrow are down again"), (ella, "use the ones in the union")],
        [(sam, "who's going to the hackathon"), (jordan, "me"), (me, "maybe, if I finish my lab")],
        [(achen, "course registration opens monday at 7am"), (leah, "setting three alarms")],
        [(ella, "study room 3B is free if anyone needs it"), (me, "taking it, thank you")],
        [(jordan, "did the advising form change?"), (leah, "yes, it's online now")],
        [(me, "lost a blue water bottle in Hollis 110"), (sam, "it's at the front desk")],
        [(rafael, "the senior photo is friday at noon on the quad"), (ella, "what do we wear"), (leah, "blue")],
        [(leah, "graduation tickets came out"), (jordan, "six each!"), (me, "my abuela is so excited")],
    ]
    for day in days(date(2024, 8, 26), date(2026, 5, 20)):
        in_term = any(t.teaching(day) for t in TERMS)
        if rng.random() < (0.32 if in_term else 0.05):
            life.chat("cs26", at(day, f"{rng.randint(9, 23):02d}:{rng.randint(0, 59):02d}"), rng.choice(banks))


def write(life: Life) -> None:
    chatgpt(life)
    lecture_notes(life)
    routine_mail(life)
    family_calls(life)
    theo_chat(life)
    class_of_2026(life)
