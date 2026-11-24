"""Two years at Lakeshore University: lectures on the calendar, the course site's notifications, mail with
professors, TAs and classmates, study-group and project-team chats, homework help from ChatGPT, notes in the
documents folder, and a task list per semester.

A course is named by its code in what people write about it ("CS 452"), the way the threads stage finds classes;
its lecture series and graded work give it weeks of activity.
"""
from datetime import date, timedelta

from igneous.persona.cast import COURSES, LAKESHORE, ME, TERMS, Course, at, days, p
from igneous.persona.life import Life, Task, TaskList

CANVAS = ("Lakeshore Canvas", f"notifications@canvas.{LAKESHORE}")
REGISTRAR = ("Lakeshore Registrar", f"registrar@{LAKESHORE}")


def d(y: int, m: int, day: int) -> date:
    return date(y, m, day)


# Per course: graded work, mail threads, ChatGPT sessions, notes and searches. A thread beat is
# (date, "HH:MM", sender key, [recipient keys], body); "me" is Mira.
CONTENT: dict[str, dict] = {
    "CS 341": {
        "work": [("P1: Unix shell", d(2024, 9, 20)), ("Midterm 1", d(2024, 10, 2)),
                 ("P2: Memory allocator", d(2024, 10, 11)), ("P3: Thread pool", d(2024, 11, 8)),
                 ("Midterm 2", d(2024, 11, 13)), ("P4: Tiny file system", d(2024, 12, 6)),
                 ("Final exam", d(2024, 12, 11))],
        "threads": [
            ("CS 341 P1: shell hangs on pipes", [
                (d(2024, 9, 12), "21:40", "me", ["osei"],
                 "Hi Kevin,\n\nIn P1 my shell hangs on `ls | wc -l`. Both children run, but wc never exits. I close the "
                 "pipe ends in each child after dup2. Should the parent close them too before waitpid?\n\nThanks,\nMira"),
                (d(2024, 9, 13), "09:05", "osei", ["me"],
                 "Hi Mira,\n\nYes: the parent still holds the write end, so wc never sees EOF. Close both ends in the "
                 "parent right after the second fork. If it still hangs, come to office hours Thursday 3-5 in "
                 "Hollis 220.\n\nKevin"),
                (d(2024, 9, 13), "12:31", "me", ["osei"], "That was it. Thank you!\n\nMira"),
            ]),
            ("CS 341 P2 extension request", [
                (d(2024, 10, 9), "08:12", "me", ["ferreira"],
                 "Dear Professor Ferreira,\n\nI've had the flu since Monday and I'm behind on P2 (the memory "
                 "allocator). Could I have until Monday to turn it in? My coalescing code works; free-list "
                 "splitting still fails two tests.\n\nThank you,\nMira Castellanos"),
                (d(2024, 10, 9), "10:47", "ferreira", ["me"],
                 "Mira,\n\nTake until Monday at noon, and note the extension at the top of your README. Feel better."
                 "\n\nDaniel Ferreira"),
            ]),
            ("CS 341 P2 regrade", [
                (d(2024, 10, 24), "16:20", "me", ["osei"],
                 "Hi Kevin,\n\nThe autograder took 6 points off P2 for test_realloc_shrink, but it passes on my "
                 "machine and on the lab machines. Could you rerun it? The log says it timed out at 2s.\n\nMira"),
                (d(2024, 10, 25), "11:02", "osei", ["me"],
                 "Reran it on a quiet grader: passes in 0.4s. The grader was overloaded on the 11th. Your P2 score "
                 "is now 96/100.\n\nKevin"),
            ]),
            ("CS 341 midterm 2 study session", [
                (d(2024, 11, 8), "19:15", "rafael", ["me", "leah"],
                 "Want to do a CS 341 study session before midterm 2? Sunday afternoon in Marrow Library, room 3B? "
                 "I'll bring the practice exam from last year."),
                (d(2024, 11, 8), "19:44", "leah", ["rafael", "me"], "Sunday works. 2pm? I'm shaky on page tables."),
                (d(2024, 11, 8), "20:03", "me", ["rafael", "leah"],
                 "2pm Sunday in 3B. I'll do a summary sheet for scheduling and synchronization."),
            ]),
            ("Thanks for CS 341", [
                (d(2024, 12, 16), "10:30", "me", ["osei"],
                 "Hi Kevin,\n\nThanks for all the help in CS 341 this semester, especially with P1 and the file "
                 "system. Good luck with your quals!\n\nMira"),
                (d(2024, 12, 16), "14:12", "osei", ["me"],
                 "Thanks Mira! You'd make a good TA for 341 if you're ever interested. Connected with you on "
                 "LinkedIn.\n\nKevin"),
            ]),
        ],
        "ai": [
            (d(2024, 9, 11), "My shell hangs when I pipe two commands for CS 341 P1. Each child calls dup2 and closes "
             "its pipe ends. What am I missing?",
             "The usual cause is a leftover write end: as long as any process holds the pipe's write end open, the "
             "reader never sees EOF. Make sure the parent closes both ends after forking, and that each child closes "
             "the end it doesn't use."),
            (d(2024, 9, 30), "Explain copy-on-write fork like I'm studying for CS 341 midterm 1.",
             "After fork, parent and child share the same physical pages, marked read-only. The first write to a "
             "page traps; the kernel copies just that page and maps the copy writable for the writer. Fork is cheap "
             "and memory is only duplicated where the two processes actually diverge."),
            (d(2024, 11, 5), "My CS 341 thread pool deadlocks when workers wait on a condition variable while the "
             "submitter holds the queue lock. How should the wait loop look?",
             "Wait in a loop on the predicate, with the mutex held: while queue empty and not shutting down, call "
             "pthread_cond_wait(&cv, &m). Signal after pushing, while holding the lock or right after releasing it, "
             "and broadcast on shutdown so every worker wakes up and exits."),
            (d(2024, 12, 8), "Compare FAT and inode-based file systems for my CS 341 final.",
             "FAT keeps a table of next-block pointers: simple, but random access walks a chain. Inode systems store "
             "block pointers (direct and indirect) per file, so finding block n is a few lookups, and metadata lives "
             "apart from names, which makes hard links possible."),
        ],
        "docs": [
            ("p1-shell-notes.md", d(2024, 9, 18), "# CS 341 P1: shell notes\n\n- fork per command, dup2 pipe ends, "
             "close everything else\n- parent closes both pipe ends after the last fork (the hang!)\n- builtins: cd, "
             "exit, history\n- tests: `make test`, 18/18 passing\n"),
            ("midterm-1-review.md", d(2024, 10, 1), "# CS 341 midterm 1 review\n\n## Processes\nfork/exec/wait, "
             "zombies vs orphans, copy-on-write.\n\n## Scheduling\nFIFO, SJF, round robin, MLFQ (priority boost every "
             "S ms).\n\n## Address spaces\nbase and bounds, segmentation, paging, TLB reach.\n"),
            ("p3-thread-pool-design.md", d(2024, 11, 4), "# CS 341 P3 thread pool design\n\nQueue: bounded ring "
             "buffer, one mutex, two condition variables (not_empty, not_full).\nWorkers loop on not_empty; "
             "shutdown flag + broadcast.\nOpen question: fairness when the queue is full.\n"),
            ("final-review.md", d(2024, 12, 10), "# CS 341 final review\n\nFile systems: FAT vs inodes, journaling "
             "(metadata vs data), crash consistency.\nConcurrency: locks, condition variables, semaphores, deadlock "
             "conditions.\nVirtual memory: page tables, TLB, replacement (LRU approximations, clock).\n"),
        ],
        "searches": [(d(2024, 9, 12), "close pipe write end parent wc hangs"),
                     (d(2024, 10, 7), "buddy allocator coalescing explained"),
                     (d(2024, 11, 6), "pthread_cond_wait spurious wakeup loop")],
    },
    "ENVS 210": {
        "work": [("Lab report 1: Dissolved oxygen", d(2024, 9, 27)), ("Field day at Avalon North Beach", d(2024, 10, 5)),
                 ("Lab report 2: Macroinvertebrates", d(2024, 10, 25)), ("Midterm", d(2024, 10, 17)),
                 ("Term paper: Harmful algal blooms in Lake Avalon", d(2024, 12, 9))],
        "threads": [
            ("ENVS 210 field day carpool", [
                (d(2024, 10, 1), "17:30", "dana", ["me"],
                 "Hey! For the ENVS 210 field day Saturday, can you drive? I have the waders and the Secchi disk from "
                 "the lab. Meet at the field station at 8."),
                (d(2024, 10, 1), "18:02", "me", ["dana"], "Yes, I'll pick you up at 7:30. Bring coffee and I'll forgive "
                 "the early start."),
            ]),
            ("ENVS 210 term paper topic", [
                (d(2024, 11, 4), "15:10", "me", ["achterberg"],
                 "Dear Professor Achterberg,\n\nFor the ENVS 210 term paper I'd like to write about harmful algal blooms "
                 "in Lake Avalon, using the city's beach sampling data from 2019-2024. Is that scope reasonable?\n\n"
                 "Best,\nMira Castellanos"),
                (d(2024, 11, 5), "09:40", "achterberg", ["me"],
                 "Mira,\n\nGood topic. Keep it to the north beaches so the data are comparable, and look at E. coli "
                 "advisories alongside the bloom counts: the city posts them but nobody puts the two together. Hannah "
                 "Brooks at the parks department keeps the raw data if you need it later.\n\nHelen Achterberg"),
            ]),
        ],
        "ai": [
            (d(2024, 9, 25), "For my ENVS 210 lab report, why does dissolved oxygen drop in late summer at the bottom "
             "of a stratified lake?",
             "Stratification cuts the bottom layer (hypolimnion) off from the air, while bacteria there keep "
             "consuming oxygen as they decompose organic matter. With no mixing until fall turnover, oxygen at depth "
             "falls, sometimes to hypoxic levels."),
            (d(2024, 12, 2), "Help me outline an ENVS 210 paper on harmful algal blooms and beach advisories in a "
             "Great Lakes city.",
             "1. What a bloom is (cyanobacteria, microcystin). 2. Drivers: phosphorus runoff, warm calm water. "
             "3. The city's sampling program and advisory thresholds. 4. Your data: bloom counts vs E. coli advisories "
             "at the north beaches, 2019-2024. 5. Gaps: how fast advisories reach swimmers. 6. Recommendations."),
        ],
        "docs": [
            ("field-day-notes.md", d(2024, 10, 5), "# ENVS 210 field day, Avalon North Beach\n\nSecchi depth 1.4 m, "
             "water 16.2 C, DO 8.9 mg/L at surface.\nKick-net samples: mayfly nymphs, caddisflies, scuds.\nThe city's "
             "sampling buoy sends readings every 30 minutes but the advisory page lags a day. Idea: an app that "
             "shows readings the moment they arrive?\n"),
            ("algal-blooms-paper-draft.md", d(2024, 12, 8), "# Harmful algal blooms and beach advisories in Lake "
             "Avalon\n\nENVS 210 term paper, Mira Castellanos\n\nBetween 2019 and 2024 the north beaches had 41 "
             "advisory days. On 9 of them the advisory was posted after the sample that triggered it was a day "
             "old.\n"),
        ],
        "searches": [(d(2024, 11, 3), "lake avalon beach sampling data e coli"),
                     (d(2024, 12, 3), "microcystin advisory threshold epa")],
    },
    "CS 375": {
        "work": [("HW1: Linear models", d(2025, 1, 31)), ("HW2: Trees and ensembles", d(2025, 2, 21)),
                 ("Project proposal", d(2025, 2, 28)), ("Midterm", d(2025, 3, 20)),
                 ("HW3: Neural networks", d(2025, 4, 4)), ("Project report", d(2025, 5, 2)),
                 ("Poster session", d(2025, 5, 6))],
        "threads": [
            ("CS 375 project partner?", [
                (d(2025, 1, 29), "13:05", "sam", ["me"],
                 "Hi Mira, are you looking for a CS 375 project partner? I was thinking of predicting beach closures "
                 "from weather and sampling data, which sounded like your ENVS stuff."),
                (d(2025, 1, 29), "14:20", "me", ["sam"],
                 "Yes! I've been playing with the city's beach data already. Let's meet after class Thursday."),
            ]),
            ("CS 375 project proposal", [
                (d(2025, 2, 25), "20:15", "me", ["adeyemi"],
                 "Hi Professor Adeyemi,\n\nSam Whitfield and I are proposing a CS 375 project that predicts next-day "
                 "E. coli advisories at Port Avalon's beaches from rainfall, wind and the previous day's samples. "
                 "Draft attached. I'll send you the revised project proposal by Friday, once we've checked we can "
                 "get five years of data.\n\nThanks,\nMira", "", "cs375.promise"),
                (d(2025, 2, 26), "08:55", "adeyemi", ["me"],
                 "Mira,\n\nA good, well-scoped problem. Watch for leakage: the advisory is decided from the same "
                 "sample you'd use as a feature. Look forward to the revision.\n\nRuth"),
                (d(2025, 2, 28), "16:40", "me", ["adeyemi"],
                 "Hi Professor Adeyemi,\n\nAttached is the revised project proposal, as promised. We now predict from "
                 "data available the evening before (rain, wind, lake temperature, the last sample older than 24 "
                 "hours), so nothing leaks.\n\nMira", "", "cs375.confirmed"),
            ]),
            ("CS 375 poster session", [
                (d(2025, 5, 5), "09:00", "adeyemi", ["me", "sam"],
                 "Mira, Sam,\n\nYou're poster 14 at tomorrow's CS 375 poster session, 2-4pm in the Hollis atrium. "
                 "Owen Brandt from Northwind said he'll stop by; he was impressed by the beach model.\n\nRuth"),
                (d(2025, 5, 5), "09:31", "sam", ["adeyemi", "me"], "Thank you! We'll be there at 1:45 to set up."),
            ]),
        ],
        "ai": [
            (d(2025, 1, 30), "CS 375 HW1: why does ridge regression shrink coefficients but lasso sets some to "
             "exactly zero?",
             "Picture the constraint regions: ridge's is a circle (L2), lasso's a diamond (L1). The loss contours "
             "usually first touch the diamond at a corner, where some coefficients are exactly zero; the circle has "
             "no corners, so ridge shrinks everything without zeroing it."),
            (d(2025, 2, 24), "We want to predict next-day beach advisories for CS 375. How do we avoid data leakage?",
             "Only use features known when the prediction is made: the evening before. Exclude the sample that "
             "triggers the advisory, split train and test by season rather than at random, and fit any scaling on "
             "the training seasons only."),
            (d(2025, 4, 2), "Explain backpropagation for a two-layer network, for my CS 375 homework.",
             "Run the forward pass and keep each layer's inputs. At the output, the gradient of the loss with respect "
             "to the pre-activation is (prediction - target) for softmax with cross-entropy. Multiply back through "
             "each layer's weights and activation derivatives; each weight's gradient is its layer's input times "
             "the gradient arriving from above."),
        ],
        "docs": [
            ("project-proposal.md", d(2025, 2, 28), "# CS 375 project proposal (revised)\n\nPredicting next-day "
             "E. coli advisories at Port Avalon beaches\nMira Castellanos, Sam Whitfield\n\nFeatures available the "
             "evening before: 24 h rainfall, wind direction and speed, lake temperature, the last sample older than "
             "24 hours. Model: gradient-boosted trees vs logistic regression. Split by season (2019-2022 train, "
             "2023 validation, 2024 test).\n"),
            ("project-results.md", d(2025, 5, 1), "# CS 375 project results\n\nGradient-boosted trees: recall 0.78 "
             "at precision 0.61 on 2024, vs 0.52 / 0.48 for the city's rule of thumb (advise after 0.5 in of rain). "
             "Wind from the northwest is the strongest single feature.\n"),
        ],
        "searches": [(d(2025, 2, 24), "data leakage time series train test split by season"),
                     (d(2025, 4, 3), "softmax cross entropy gradient derivation")],
    },
    "CS 362": {
        "work": [("A1: SQL", d(2025, 1, 29)), ("A2: B+ trees", d(2025, 2, 19)), ("Midterm", d(2025, 3, 5)),
                 ("A3: Query optimizer", d(2025, 4, 9)), ("A4: Transactions and recovery", d(2025, 4, 30)),
                 ("Final exam", d(2025, 5, 7))],
        "threads": [
            ("CS 362 study group", [
                (d(2025, 1, 22), "18:40", "leah", ["me", "rafael"],
                 "Should we make a CS 362 study group like for 341? I made a chat space for it, I'll add you both."),
                (d(2025, 1, 22), "19:10", "rafael", ["leah", "me"], "In. Wednesdays after lab?"),
            ]),
            ("CS 362 A3 office hours", [
                (d(2025, 4, 3), "11:15", "me", ["hale"],
                 "Hi Professor Hale,\n\nFor CS 362 A3, should the optimizer consider bushy join trees or only "
                 "left-deep ones? The handout mentions both.\n\nThanks,\nMira"),
                (d(2025, 4, 3), "15:30", "hale", ["me"], "Left-deep only for A3. Bushy plans are extra credit.\n\nM. Hale"),
            ]),
        ],
        "ai": [
            (d(2025, 2, 17), "For CS 362, why do B+ trees keep all values in the leaves?",
             "Keeping records only in the leaves makes internal nodes small, so fan-out is high and the tree is "
             "shallow, and linking the leaves turns a range scan into one walk along the bottom level."),
            (d(2025, 4, 28), "Explain ARIES redo and undo for my CS 362 assignment on recovery.",
             "ARIES first repeats history: redo every logged update since the checkpoint, even for losers, so the "
             "database is exactly as it was at the crash. Then undo the losers' updates in reverse, writing "
             "compensation log records so a crash during recovery never undoes anything twice."),
        ],
        "docs": [("a3-optimizer-notes.md", d(2025, 4, 6), "# CS 362 A3: query optimizer notes\n\nSelinger-style "
                  "dynamic programming over left-deep plans. Cost = page I/Os. Interesting orders kept per subset.\n")],
        "searches": [(d(2025, 4, 5), "selinger optimizer interesting orders")],
    },
    "CS 452": {
        "work": [("Lab 1: MapReduce", d(2025, 9, 12)), ("Lab 2: Raft leader election", d(2025, 10, 3)),
                 ("Lab 3: Raft log replication", d(2025, 10, 31)), ("Midterm", d(2025, 10, 16)),
                 ("Lab 4: Sharded key-value store", d(2025, 12, 5)), ("Final project demo", d(2025, 12, 10))],
        "threads": [
            ("CS 452 team for the Raft labs", [
                (d(2025, 8, 28), "22:05", "jordan", ["me", "achen"],
                 "Hey Mira, Alex: want to team up for the CS 452 Raft labs? I made a chat space."),
                (d(2025, 8, 28), "22:30", "achen", ["jordan", "me"], "Sure, I'm in."),
                (d(2025, 8, 29), "07:50", "me", ["jordan", "achen"], "Me too. I'll set up the repo tonight."),
            ]),
            ("CS 452 Lab 3 grading", [
                (d(2025, 11, 7), "10:20", "gliu", ["me", "jordan", "achen"],
                 "Hi team,\n\nLab 3 (Raft log replication) passed 31 of 32 tests; TestFigure8Unreliable failed 2 of "
                 "50 runs, which costs 4 points: 46/50. Your write-up on the commit index bug was the clearest in the "
                 "class.\n\nGrace (CS 452 TA)", "", "cs452.grade"),
                (d(2025, 11, 7), "11:02", "me", ["gliu", "jordan", "achen"],
                 "Thanks Grace! We think it's the election timeout under partition; we'll fix it for Lab 4."),
            ]),
            ("CS 452 final project demo slot", [
                (d(2025, 12, 1), "09:30", "kowalski", ["me", "jordan", "achen"],
                 "Your CS 452 final demo is Wednesday Dec 10 at 3:20pm in Hollis 110. Ten minutes: show the sharded "
                 "store surviving a partition, then questions.\n\nM. Kowalski"),
                (d(2025, 12, 1), "10:12", "me", ["kowalski", "jordan", "achen"], "Thank you, we'll be there."),
            ]),
            ("CS 452 recommendation letter", [
                (d(2026, 1, 20), "15:00", "me", ["kowalski"],
                 "Dear Professor Kowalski,\n\nWould you be willing to write a short reference for me? Northwind "
                 "Robotics is considering me for a full-time role on their distributed telemetry system, and CS 452 "
                 "is the course that prepared me for it.\n\nThank you,\nMira"),
                (d(2026, 1, 21), "08:10", "kowalski", ["me"],
                 "Mira, happily. Send me the contact and the deadline.\n\nMK"),
            ]),
        ],
        "ai": [
            (d(2025, 9, 26), "CS 452 Lab 2: my Raft cluster elects two leaders in the same term after a partition. "
             "What could cause that?",
             "Two leaders in one term means a node granted two votes in that term. Persist votedFor and currentTerm "
             "before replying to RequestVote, reset votedFor only when the term advances, and reject votes from "
             "candidates with a staler log."),
            (d(2025, 10, 22), "Explain why Raft only commits entries from the leader's current term (the figure 8 "
             "case) for my CS 452 lab.",
             "An entry from an earlier term can be replicated on a majority and still be overwritten if a leader "
             "without it wins. Raft therefore only advances commitIndex by counting replicas of current-term entries; "
             "earlier entries commit indirectly once a current-term entry after them does."),
            (d(2025, 11, 20), "For CS 452 Lab 4, how should a sharded key-value store move shards between groups "
             "without losing writes?",
             "Make reconfiguration a log entry: when a group learns a new config, it stops serving shards it lost, "
             "the new owner pulls their data (and duplicate-detection table) through its own Raft log, and only then "
             "serves them. Never serve a shard in a config you haven't fully installed."),
        ],
        "docs": [
            ("raft-notes.md", d(2025, 10, 1), "# CS 452 Raft notes\n\nElection timeout 300-600 ms randomized; "
             "heartbeats every 100 ms.\nPersist currentTerm, votedFor, log before replying.\nCommit only "
             "current-term entries (figure 8).\nTeam: Jordan (election), Alex (persistence), me (replication).\n"),
            ("lab4-design.md", d(2025, 11, 24), "# CS 452 Lab 4 design: sharded KV\n\nShard controller as its own "
             "Raft group. Config changes go through the log. Shard migration: pull + install + GC.\nOpen: what if "
             "the old owner is partitioned during handoff?\n"),
        ],
        "searches": [(d(2025, 9, 25), "raft two leaders same term votedFor persist"),
                     (d(2025, 10, 21), "raft figure 8 commit current term")],
    },
    "CS 481": {
        "work": [("Team charter", d(2026, 1, 23)), ("Design review", d(2026, 2, 20)), ("Alpha demo", d(2026, 3, 27)),
                 ("Beta release", d(2026, 4, 17)), ("Demo Day", d(2026, 5, 1)), ("Final report", d(2026, 5, 6))],
        "threads": [
            ("CS 481 capstone team: Harborline", [
                (d(2026, 1, 14), "20:00", "me", ["rafael", "jordan", "leah"],
                 "Hi all,\n\nAdeyemi approved our CS 481 capstone: Harborline, a live map of Port Avalon's harbor "
                 "shuttle for riders. I made a chat space and a shared folder. First meeting Thursday after class?\n\n"
                 "Mira"),
                (d(2026, 1, 14), "20:22", "leah", ["me", "rafael", "jordan"], "Thursday works!"),
                (d(2026, 1, 14), "21:05", "jordan", ["me", "rafael", "leah"], "Same. I'll draft the team charter."),
            ]),
            ("CS 481 design review feedback", [
                (d(2026, 2, 23), "13:30", "adeyemi", ["me", "rafael", "jordan", "leah"],
                 "Team Harborline,\n\nGood design review. Two things before the alpha: decide how you handle the "
                 "shuttle's GPS gaps under the bridge, and get the transit authority's feed terms in writing.\n\n"
                 "Ruth"),
                (d(2026, 2, 23), "14:02", "me", ["adeyemi", "rafael", "jordan", "leah"],
                 "Thank you! We'll interpolate along the route for gaps under 90 seconds, and I'll email the transit "
                 "authority this week."),
            ]),
            ("CS 481 Demo Day", [
                (d(2026, 4, 27), "10:00", "adeyemi", ["me", "rafael", "jordan", "leah"],
                 "CS 481 Demo Day is Friday May 1, 1-5pm in the Student Union ballroom. Harborline is table 6.\n\nRuth"),
                (d(2026, 4, 27), "10:20", "rafael", ["adeyemi", "me", "jordan", "leah"], "We'll be there!"),
            ]),
        ],
        "ai": [
            (d(2026, 2, 18), "For our CS 481 capstone (Harborline, live shuttle map), how should we smooth GPS points "
             "that jump around near tall buildings?",
             "Snap each point to the known route (map matching), then run a simple Kalman filter along the route "
             "distance, so noise across the street disappears and short gaps are interpolated by the motion model."),
            (d(2026, 4, 14), "Write a short abstract for our CS 481 capstone Harborline, a live map of a city harbor "
             "shuttle, for Demo Day.",
             "Harborline shows riders where Port Avalon's harbor shuttle is right now and when it will reach their "
             "stop. It matches noisy GPS to the route, predicts arrivals from the last three weeks of trips, and "
             "works offline on the pier where coverage drops."),
        ],
        "docs": [
            ("harborline-design.md", d(2026, 2, 19), "# CS 481 capstone: Harborline design\n\nTeam: Mira "
             "Castellanos, Rafael Ortiz, Jordan Pike, Leah Goldberg\n\nIngest: transit authority GTFS-realtime feed "
             "every 15 s.\nMap matching to the shuttle route; Kalman filter on route distance.\nArrival prediction: "
             "median of the last 21 days' segment times.\n"),
            ("harborline-final-report.md", d(2026, 5, 6), "# CS 481 final report: Harborline\n\nAt Demo Day the "
             "arrival predictions were within 45 s for 83% of stops. The transit authority asked to keep running "
             "it over the summer.\n"),
        ],
        "searches": [(d(2026, 2, 17), "gps map matching kalman filter route distance"),
                     (d(2026, 3, 2), "gtfs realtime vehicle positions feed terms")],
    },
}

# Courses that leave little trace: a couple of course-site notices and a note or two.
LIGHT_NOTES = {
    "STAT 315": [("hw-notes.md", d(2024, 10, 14), "# STAT 315 notes\n\nConditional expectation, law of total "
                  "variance, the CLT for sums of iid variables.\n")],
    "ENVS 410": [("gis-lab-notes.md", d(2025, 10, 8), "# ENVS 410 GIS lab notes\n\nWatershed delineation from the "
                  "DEM; flow accumulation threshold 500 cells. The Avalon Creek outfall drains to the north beach.\n"),
                 ("final-map-notes.md", d(2025, 12, 5), "# ENVS 410 final map\n\nStorm outfalls within 1 km of each "
                  "beach, colored by advisory days per season.\n")],
    "PHIL 220": [("essay-1-notes.md", d(2026, 2, 9), "# PHIL 220 essay 1\n\nIs a recommender system responsible "
                  "for what it recommends? Draft argument: responsibility follows foreseeability.\n")],
}


def lectures(life: Life, c: Course) -> None:
    """The course's weekly lectures on the calendar (one recurring event), and Mira's days in its building."""
    first = next(day for day in days(c.term.start, c.term.end, c.meets))
    hour, minute = c.hour.split(":")
    until = at(c.term.end, "23:59").strftime("%Y%m%dT%H%M%SZ")
    byday = ",".join({"M": "MO", "T": "TU", "W": "WE", "R": "TH", "F": "FR"}[x] for x in c.meets)
    life.event(f"{c.code} {c.title}", at(first, c.hour), c.minutes, location=c.room,
               rrule=f"FREQ=WEEKLY;BYDAY={byday};UNTIL={until}", tag=f"{tag(c)}.lectures")
    for day in days(c.term.start, c.term.end, c.meets):
        if c.term.teaching(day):
            building = "marsh" if c.code.startswith("ENVS") else "hollis"
            life.visit(building, at(day, f"{int(hour):02d}:{minute}") - timedelta(minutes=7), c.minutes + 12)


def notices(life: Life, c: Course) -> None:
    """The course site's mail: an announcement now and then, a reminder before each piece of graded work, the grade
    after it."""
    rng = life.rng(f"canvas:{c.code}")
    for title, due in CONTENT.get(c.code, {}).get("work", []) or default_work(c):
        life.mail(at(due - timedelta(days=1), f"{rng.randint(7, 9):02d}:00"), CANVAS, [ME],
                  f"Reminder: {title} is due tomorrow ({c.code})",
                  f"{c.code} {c.title}\n\n{title} is due {due:%A, %B} {due.day} at 11:59pm.\n\n"
                  "View the assignment on Lakeshore Canvas.\nYou are receiving this because you are enrolled in "
                  f"{c.code}. Change your notification settings in Canvas.")
        graded = due + timedelta(days=rng.randint(5, 11))
        if graded <= c.term.end + timedelta(days=7) and "exam" not in title.lower() and "demo" not in title.lower():
            life.mail(at(graded, f"{rng.randint(10, 22):02d}:{rng.choice(['05', '20', '41'])}"), CANVAS, [ME],
                      f"Assignment graded: {title} ({c.code})",
                      f"{c.code} {c.title}\n\nYour submission for {title} has been graded.\n\nView your grade and "
                      "comments on Lakeshore Canvas.")
    week = c.term.start + timedelta(days=rng.randint(9, 16))
    topics = ["Office hours moved this week", "Exam logistics", "Slides for this week are posted",
              "Reading for next week", "Course evaluation is open"]
    while week < c.term.end:
        life.mail(at(week, f"{rng.randint(8, 18):02d}:{rng.choice(['00', '15', '30', '45'])}"), CANVAS, [ME],
                  f"{c.code}: New announcement: {rng.choice(topics)}",
                  f"{c.code} {c.title}\n\nA new announcement was posted by your instructor. Read it on Lakeshore "
                  "Canvas.")
        week += timedelta(days=rng.randint(18, 30))


def default_work(c: Course) -> list[tuple[str, date]]:
    mid = c.term.start + (c.term.end - c.term.start) / 2
    return [("Homework 1", c.term.start + timedelta(days=17)), ("Midterm", mid),
            ("Homework 3", mid + timedelta(days=21)), ("Final exam", c.term.end - timedelta(days=2))]


def tag(c: Course) -> str:
    """The prefix of a course's tags in the gold set: "cs341"."""
    return c.code.lower().replace(" ", "")


def course_mail(life: Life, c: Course) -> None:
    rng = life.rng(f"mail:{c.code}")
    for i, (subject, beats) in enumerate(CONTENT.get(c.code, {}).get("threads", []), 1):
        rows = []
        for n, beat in enumerate(beats):
            day, hhmm, who, to, body = beat[:5]
            cc = [p(k) for k in beat[5]] if len(beat) > 5 and beat[5] else []
            own = beat[6] if len(beat) > 6 else (f"{tag(c)}.mail{i}" if n == 0 else None)
            rows.append((at(day, hhmm), p(who), [p(k) for k in to], body, cc, own))
        life.thread(subject, rows, quote=rng.random() < 0.8)


def course_ai(life: Life, c: Course) -> None:
    rng = life.rng(f"ai:{c.code}")
    for i, (day, question, answer) in enumerate(CONTENT.get(c.code, {}).get("ai", []), 1):
        t = at(day, f"{rng.randint(19, 23):02d}:{rng.randint(0, 59):02d}")
        follow = rng.choice(["That makes sense, thanks. Can you give a tiny example?",
                             "Got it. What's the most common mistake people make here?",
                             "Thanks! How would I test that?"])
        reply = rng.choice(["Here's a small example: " + answer.split(". ")[0].lower() + ", then check the edge case "
                            "where the input is empty.",
                            "The most common mistake is fixing the symptom instead of the invariant; write the "
                            "invariant down first, then check each step against it.",
                            "Write a test that fails without the fix, run it a hundred times, and keep it."])
        title = question.split("?")[0].split(".")[0]
        title = (title[:52].rsplit(" ", 1)[0] if len(title) > 52 else title).strip()
        life.ask(title, [(t, question), (t + timedelta(seconds=20), answer),
                         (t + timedelta(minutes=rng.randint(2, 6)), follow),
                         (t + timedelta(minutes=rng.randint(7, 9)), reply)], tag=f"{tag(c)}.ai{i}")


SLIDES = {
    "CS 375": ("poster-slides.pptx", d(2025, 5, 5), [
        ("Predicting beach advisories a day ahead", ["CS 375 project, Mira Castellanos and Sam Whitfield",
                                                     "Port Avalon beaches, 2019-2024"]),
        ("What we predict from", ["24 h rainfall, wind, lake temperature", "The last sample older than 24 hours",
                                  "Nothing the advisory itself is decided from"]),
        ("Results", ["Gradient-boosted trees: recall 0.78 at precision 0.61 (2024)", "City rule of thumb: 0.52 / 0.48",
                     "Wind from the northwest matters most"])]),
    "CS 481": ("demo-day-slides.pptx", d(2026, 4, 30), [
        ("Harborline", ["Where is the harbor shuttle right now?", "CS 481 Senior Capstone, Demo Day, table 6"]),
        ("How it works", ["GTFS-realtime feed every 15 s", "Map matching and a Kalman filter on route distance",
                          "Arrivals from the last 21 days of trips"]),
        ("Results", ["Within 45 s for 83% of stops", "Works offline on the pier"])]),
}


def course_docs(life: Life, c: Course) -> None:
    rng = life.rng(f"docs:{c.code}")
    folder = f"School/{c.term.name}/{c.code} {c.title}"
    for name, day, body in CONTENT.get(c.code, {}).get("docs", []) + LIGHT_NOTES.get(c.code, []):
        life.doc(f"{folder}/{name}", at(day, f"{rng.randint(14, 23):02d}:{rng.randint(0, 59):02d}"), body,
                 tag=f"{tag(c)}.doc.{name.rsplit('.', 1)[0]}")
    if c.code in SLIDES:
        name, day, slides = SLIDES[c.code]
        life.doc(f"{folder}/{name}", at(day, "22:40"), slides)
    for day, query in CONTENT.get(c.code, {}).get("searches", []):
        life.search(at(day, f"{rng.randint(9, 23):02d}:{rng.randint(0, 59):02d}"), "Search", f"Searched for {query}",
                    "https://www.google.com/search?q=" + query.replace(" ", "+"))


def semester_tasks(life: Life) -> None:
    """One Google Tasks list per semester: the graded work, done by its due date."""
    for term in TERMS:
        tasks = []
        for c in COURSES:
            if c.term is not term:
                continue
            for title, due in CONTENT.get(c.code, {}).get("work", []) or default_work(c):
                created = at(max(term.start, due - timedelta(days=14)), "08:30")
                tasks.append(Task(f"{c.code} {title}", created, due, at(due, "21:30")))
        tasks.sort(key=lambda t: t.due)
        life.task_lists.append(TaskList(term.name, tasks, tag=f"tasks.{term.name.lower().replace(' ', '-')}"))


def study_chats(life: Life) -> None:
    """The CS 362 study group, the CS 452 Raft team and the CS 481 capstone team, each in a chat space."""
    me, rafael, leah, jordan, achen = ME, p("rafael"), p("leah"), p("jordan"), p("achen")
    life.space("cs362", "CS 362 study group", [me, leah, rafael])
    life.space("cs452", "CS 452 Raft team", [me, jordan, achen])
    life.space("cs481", "Harborline capstone", [me, rafael, jordan, leah])
    rng = life.rng("chat:study")
    week_lines = {
        "cs362": [[(leah, "Study group tonight? 7 in Marrow 3B"), (rafael, "yes"), (me, "I'll bring snacks")],
                  [(rafael, "anyone finish the B+ tree split?"), (me, "mine fails when the root splits"),
                   (leah, "you have to make a new root and push the middle key up"), (me, "ohh. thank you")],
                  [(me, "CS 362 midterm covers through chapter 12 right?"), (leah, "through 13, hash joins too")],
                  [(leah, "A3 optimizer is brutal"), (rafael, "left-deep only, Hale said"), (me, "phew")],
                  [(rafael, "practice final is up"), (leah, "doing it Saturday, join?"), (me, "yes")]],
        "cs452": [[(jordan, "lab 2 timeouts randomized 300-600ms?"), (me, "yes and heartbeats every 100"),
                   (achen, "pushed persistence, please pull")],
                  [(me, "two leaders in term 7 again"), (jordan, "votedFor isn't persisted before the reply"),
                   (me, "you're right, fixing")],
                  [(achen, "CS 452 lab 3 passing 30/32 locally"), (me, "figure 8 still flaky"),
                   (jordan, "let's pair on it thursday")],
                  [(jordan, "office hours with Grace at 4?"), (achen, "can't, work"), (me, "I'll go and report back")],
                  [(me, "shard migration works if the old owner stays up"), (achen, "and if it doesn't?"),
                   (me, "then we hang. tomorrow's problem")],
                  [(jordan, "demo script draft in the doc"), (me, "looks good, I'll do the partition part")]],
        "cs481": [[(me, "standup: map matching done for the inbound route"), (rafael, "arrival model v1 is in"),
                   (leah, "UI for the stop list"), (jordan, "transit feed keys came through")],
                  [(jordan, "the shuttle disappears under the bridge again"), (me, "interpolate up to 90s, like we "
                                                                                    "said in the design review")],
                  [(leah, "alpha demo went well!"), (rafael, "Adeyemi liked the offline mode"), (me, "🎉")],
                  [(me, "Harborline beta is live on the test URL"), (jordan, "sharing with the transit folks")],
                  [(rafael, "Demo Day poster printed"), (leah, "table 6!"), (me, "see you at 12:30 to set up")]],
    }
    spans = {"cs362": ("Spring 2025", "W"), "cs452": ("Fall 2025", "TR"), "cs481": ("Spring 2026", "TR")}
    for key, (term_name, weekdays) in spans.items():
        term = next(t for t in TERMS if t.name == term_name)
        for day in days(term.start + timedelta(days=7), term.end, weekdays):
            if not term.teaching(day) or rng.random() < 0.45:
                continue
            lines = rng.choice(week_lines[key])
            life.chat(key, at(day, f"{rng.randint(17, 22):02d}:{rng.randint(0, 59):02d}"), lines)


def admin(life: Life) -> None:
    """The registrar and advising: registration windows, the graduation application, commencement."""
    rows = [
        (d(2024, 10, 28), "Spring 2025 registration opens November 4", "Your registration window opens Monday, "
         "November 4 at 7:00am. Clear any holds before then."),
        (d(2025, 3, 31), "Fall 2025 registration opens April 7", "Your registration window opens Monday, April 7 at "
         "7:00am."),
        (d(2025, 10, 27), "Spring 2026 registration opens November 3", "Your registration window opens Monday, "
         "November 3 at 7:00am."),
        (d(2026, 2, 2), "Apply to graduate by February 15", "Students completing degrees in May 2026 must apply to "
         "graduate by February 15."),
        (d(2026, 4, 20), "Commencement: Saturday, May 16", "Commencement is Saturday, May 16 at 10:00am in "
         "Lakefront Stadium. Each graduate receives six guest tickets."),
    ]
    for day, subject, body in rows:
        life.mail(at(day, "07:00"), REGISTRAR, [ME], subject, body + "\n\nOffice of the Registrar, Lakeshore "
                  "University", headers={"Precedence": "bulk"})
    life.thread("Advising: spring 2026 plan", [
        (at(d(2025, 10, 29), "10:15"), ME, [p("adeyemi")],
         "Hi Professor Adeyemi,\n\nFor spring I'm planning CS 481 (capstone), PHIL 220 and two electives. That "
         "finishes my degree, right? Could we meet during advising week?\n\nMira"),
        (at(d(2025, 10, 29), "13:40"), p("adeyemi"), [ME],
         "Mira,\n\nYes, that completes the B.S. with the environmental science minor. Thursday 11:30 in Hollis 305 "
         "works.\n\nRuth"),
    ])


def write(life: Life) -> None:
    for c in COURSES:
        lectures(life, c)
        notices(life, c)
        course_mail(life, c)
        course_ai(life, c)
        course_docs(life, c)
    semester_tasks(life)
    study_chats(life)
    admin(life)
