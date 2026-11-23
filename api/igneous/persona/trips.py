"""Two trips, each a few weeks of planning and a week away: Lisbon over spring break 2025 with Dana, Sofia and
Rafael, and Iceland's Ring Road after graduation in June 2026 with Dana and Theo. Each has a group chat, bookings
by mail, a calendar block, a ChatGPT itinerary, a task list, saved places, notes in the documents folder and the
places Mira's phone saw her visit; Lisbon comes up again months later, the way a trip does.
"""
from datetime import date

from igneous.persona.cast import ICELAND, LISBON, ME, at, p
from igneous.persona.life import Life, Task, TaskList

AIRLINE = ("Atlantica Air", "no-reply@atlantica-air.example.com")
WANDERSTAY = ("Wanderstay", "no-reply@wanderstay.example.com")
NORDLYS = ("Nordlys Car Rental", "no-reply@nordlys-rental.example.com")
RAIL = ("Lisboa Rail", "noreply@lisboa-rail.example.com")


def d(y: int, m: int, day: int) -> date:
    return date(y, m, day)


def lisbon(life: Life) -> None:
    dana, sofia, rafael = p("dana"), p("sofia"), p("rafael")
    group = [ME, dana, sofia, rafael]
    life.space("lisbon", "Lisbon spring break", group)
    life.thread("Spring break: Lisbon?", [
        (at(d(2025, 1, 22), "21:10"), sofia, [ME, dana, rafael],
         "Okay hear me out: Lisbon for spring break. Flights from Port Avalon are cheap in March, it's warm-ish, "
         "and there's a guesthouse in Alfama with two rooms. Who's in?"),
        (at(d(2025, 1, 22), "21:26"), dana, [sofia, ME, rafael], "YES. I've wanted to go to Lisbon forever."),
        (at(d(2025, 1, 22), "21:58"), rafael, [sofia, ME, dana], "Only if we do a day trip to Sintra."),
        (at(d(2025, 1, 22), "22:40"), ME, [sofia, dana, rafael],
         "I'm in! Atlantica has a direct flight Saturday March 8 at 6:40pm, back Saturday the 15th, about $640. "
         "I'll make a chat space so we stop flooding our inboxes."),
    ], tag="lisbon.idea")
    life.mail(at(d(2025, 2, 2), "19:22"), AIRLINE, [ME], "Your Atlantica Air booking confirmation: Port Avalon to "
              "Lisbon, March 8", "Booking confirmation QX7R4M\n\nPassenger: MIRA CASTELLANOS\n\nOutbound: AT 214, "
              "Saturday March 8, 2025. Departs Port Avalon (PAV) 6:40pm, arrives Lisbon (LIS) 7:25am (+1).\n"
              "Return: AT 215, Saturday March 15, 2025. Departs Lisbon (LIS) 11:10am, arrives Port Avalon (PAV) "
              "2:05pm.\n\nFare paid: $638.40. Checked bag: 1.\n\nManage your booking at atlantica-air.example.com. "
              "This mailbox is not monitored.", tag="lisbon.flight")
    life.mail(at(d(2025, 2, 6), "20:05"), WANDERSTAY, [ME], "Booking confirmed: Casa Lumen Guesthouse, Lisbon "
              "(Mar 9 - Mar 15)", "Your stay is confirmed.\n\nCasa Lumen Guesthouse\nRua do Vale 14, 1200 Lisboa, "
              "Portugal\nCheck-in: Sunday, March 9, 2025 from 10:00am (early check-in arranged)\nCheck-out: "
              "Saturday, March 15, 2025 by 9:00am\nRooms: 2 twin rooms, 4 guests\nTotal: EUR 702.00, paid\n\n"
              "Reservation number WS-58204417. Questions? Message the host in the Wanderstay app.",
              tag="lisbon.hostel")
    life.thread("Lisbon budget", [
        (at(d(2025, 2, 12), "18:30"), dana, [ME, sofia, rafael],
         "Made a budget for Lisbon: flights $638, guesthouse EUR 175.50 each, Sintra train EUR 4.60 return, and "
         "about EUR 40 a day for food. Mira, can you keep the expenses during the trip? You're the spreadsheet "
         "person."),
        (at(d(2025, 2, 12), "19:05"), ME, [dana, sofia, rafael],
         "Happy to. I'll track everything and we'll settle up when we're back."),
    ])
    trip = life.event("Lisbon spring break", at(LISBON[0], "18:40"), 7 * 24 * 60 - 60 * 4, location="Lisbon",
                      attendees=(dana, sofia, rafael), organizer=dana)
    life.mail(at(d(2025, 3, 1), "12:00"), dana, [ME, sofia, rafael],
              "Invitation: Lisbon spring break @ Sat Mar 8 - Sat Mar 15, 2025 (mcastellanos@lakeshore.example.edu)",
              "Dana Kimura has invited you to an event.\n\nLisbon spring break\nSat Mar 8, 2025 - Sat Mar 15, 2025\n"
              "Where: Lisbon, Portugal\nFlight AT 214 leaves PAV at 6:40pm. Guesthouse: Casa Lumen, Rua do Vale 14.\n\n"
              "Invitation from Google Calendar")
    _ = trip
    life.event("Flight AT 214 PAV-LIS", at(LISBON[0], "18:40"), 465, location="Port Avalon International Airport")
    life.event("Sintra day trip", at(d(2025, 3, 11), "09:10", "lisbon"), 9 * 60, location="Sintra", zone="lisbon")
    life.event("Flight AT 215 LIS-PAV", at(LISBON[1], "11:10", "lisbon"), 535, location="Lisbon Airport",
               zone="lisbon")
    life.mail(at(d(2025, 3, 7), "18:40"), AIRLINE, [ME], "Check in now for your flight to Lisbon",
              "Check-in is open for AT 214 Port Avalon (PAV) to Lisbon (LIS), Saturday March 8 at 6:40pm. Boarding "
              "closes 30 minutes before departure.\n\nThis mailbox is not monitored.")
    life.mail(at(d(2025, 3, 10), "21:12", "lisbon"), RAIL, [ME], "Your tickets: Lisboa Rossio to Sintra, 11 March",
              "4 return tickets, Lisboa Rossio - Sintra, Tuesday 11 March 2025. Total EUR 18.40.\nShow the QR code "
              "at the gate.")
    rng = life.rng("lisbon")
    chat = [
        (d(2025, 1, 23), "09:15", [(ME, "welcome to the Lisbon chat!"), (sofia, "🇵🇹🇵🇹🇵🇹"),
                                   (rafael, "is the flight really $640"), (ME, "yes, booking this weekend")]),
        (d(2025, 1, 26), "20:40", [(dana, "found Casa Lumen in Alfama, two twin rooms, EUR 117 a night"),
                                   (sofia, "the one with the tiles? yes please"), (rafael, "book it")]),
        (d(2025, 2, 2), "19:30", [(ME, "flights booked! AT 214 sat march 8, 6:40pm"), (dana, "AAAA"),
                                  (sofia, "booking mine now")]),
        (d(2025, 2, 6), "20:10", [(dana, "Casa Lumen is booked, check-in Sunday morning, they let us in early"),
                                  (ME, "thank you!!")]),
        (d(2025, 2, 10), "22:05", [(ME, "asked ChatGPT for a 4 day Lisbon plan, it's in our folder"),
                                   (rafael, "does it include Sintra"), (ME, "tuesday the 11th, all day")]),
        (d(2025, 2, 18), "12:30", [(sofia, "fado night? there's a tiny place called Tasca do Mar"),
                                   (dana, "yes"), (ME, "yes")]),
        (d(2025, 2, 27), "17:50", [(rafael, "do we need cash in Lisbon"), (dana, "cards work almost everywhere"),
                                   (sofia, "the tram takes the Viva Viagem card")]),
        (d(2025, 3, 5), "21:00", [(ME, "itinerary v2 in the folder, LX Factory moved to Thursday"),
                                  (sofia, "perfect")]),
        (d(2025, 3, 8), "15:30", [(dana, "leaving for the airport at 4:30"), (rafael, "I'm already here lol"),
                                  (ME, "see you at gate B4")]),
        (d(2025, 3, 9), "08:20", [(sofia, "LANDED"), (ME, "Lisbon!!!"), (dana, "the guesthouse has a cat")]),
        (d(2025, 3, 10), "13:10", [(rafael, "Belém Tower line is long, going to the pastry place first"),
                                   (ME, "get me two")]),
        (d(2025, 3, 11), "08:30", [(ME, "train to Sintra 9:11 from Rossio, tickets on my phone"),
                                   (dana, "omw")]),
        (d(2025, 3, 11), "17:45", [(sofia, "Pena Palace was unreal"), (rafael, "worth it")]),
        (d(2025, 3, 12), "20:30", [(dana, "fado at Tasca do Mar at 9!"), (ME, "on our way")]),
        (d(2025, 3, 13), "15:00", [(ME, "LX Factory bookstore is amazing"), (rafael, "buy the map print")]),
        (d(2025, 3, 14), "22:10", [(sofia, "last night in Lisbon 😭"), (dana, "miradouro at sunset was the best"),
                                   (ME, "checkout is 9, taxi at 8:30")]),
        (d(2025, 3, 15), "16:00", [(rafael, "home. already miss it"), (ME, "same")]),
        (d(2025, 3, 20), "19:40", [(ME, "expenses are in the email, Venmo me whenever"),
                                   (sofia, "paid!"), (dana, "paid")]),
    ]
    for day, hhmm, lines in chat:
        zone = "lisbon" if LISBON[0] < day <= LISBON[1] else "home"
        life.chat("lisbon", at(day, hhmm, zone), lines, tag="lisbon.chat" if day == d(2025, 3, 11) else None)
    life.ask("4-day Lisbon itinerary", [
        (at(d(2025, 2, 10), "21:30"), "Plan 4 days in Lisbon for four college students in March, staying in "
         "Alfama, budget-friendly, with a day trip to Sintra."),
        (at(d(2025, 2, 10), "21:31"), "Day 1 (Alfama and Baixa): São Jorge castle, Miradouro de Santa Luzia, "
         "Tram 28, dinner in Alfama. Day 2 (Belém): Belém Tower, the monastery, pastries at the old bakery, the "
         "riverside walk to LX Factory. Day 3 (Sintra): train from Rossio, Pena Palace early, Quinta da Regaleira "
         "after lunch. Day 4: Time Out Market, Miradouro da Senhora do Monte at sunset, a fado night."),
        (at(d(2025, 2, 10), "21:40"), "Can you move LX Factory to its own afternoon? We want more time there."),
        (at(d(2025, 2, 10), "21:41"), "Sure: put LX Factory on Day 4 afternoon, and Time Out Market for lunch "
         "before it; the sunset miradouro and fado stay in the evening."),
    ], tag="lisbon.ai")
    life.doc("Travel/Lisbon 2025/itinerary.md", at(d(2025, 3, 5), "20:55"),
             "# Lisbon spring break itinerary\n\nMarch 8-15, 2025: Mira, Dana, Sofia, Rafael\nStay: Casa Lumen "
             "Guesthouse, Rua do Vale 14 (Alfama)\n\n- Sun 9: arrive 7:25am, early check-in, Alfama walk, Tram 28\n"
             "- Mon 10: Belém Tower, pastries, riverside walk\n- Tue 11: Sintra day trip (9:11 train from Rossio), "
             "Pena Palace\n- Wed 12: Time Out Market, fado at Tasca do Mar 9pm\n- Thu 13: LX Factory\n- Fri 14: "
             "Miradouro da Senhora do Monte at sunset\n- Sat 15: taxi 8:30am, AT 215 at 11:10am\n",
             tag="doc.lisbon-itinerary")
    life.doc("Travel/Lisbon 2025/expenses.xlsx", at(d(2025, 3, 20), "19:20"), {"Expenses": [
        ["Date", "What", "Paid by", "EUR", "Split"],
        ["2025-03-09", "Taxi from airport", "Mira", 32.0, "4"],
        ["2025-03-10", "Pastries, Belém", "Rafael", 14.4, "4"],
        ["2025-03-11", "Sintra train", "Mira", 18.4, "4"],
        ["2025-03-11", "Pena Palace tickets", "Dana", 80.0, "4"],
        ["2025-03-12", "Fado dinner, Tasca do Mar", "Sofia", 112.0, "4"],
        ["2025-03-13", "Groceries", "Mira", 27.9, "4"],
        ["2025-03-15", "Taxi to airport", "Dana", 30.0, "4"],
        ["", "Total", "", 314.7, ""]]}, tag="doc.lisbon-expenses")
    life.thread("Lisbon expenses: who owes what", [
        (at(d(2025, 3, 20), "19:30"), ME, [dana, sofia, rafael],
         "Settled the Lisbon expenses: EUR 314.70 total, EUR 78.68 each. Rafael owes Mira EUR 64.28, Sofia is "
         "owed EUR 33.32 by Dana. Spreadsheet's in our folder."),
        (at(d(2025, 3, 20), "20:02"), rafael, [ME, dana, sofia], "Sent! Thanks for keeping track."),
    ])
    life.thread("Lisbon photos", [
        (at(d(2025, 3, 18), "22:15"), rafael, [ME, dana, sofia],
         "Shared album with all the Lisbon photos: https://photos.example.com/album/lisbon-2025-7f3a. The Pena "
         "Palace panorama turned out great."),
        (at(d(2025, 3, 19), "08:40"), sofia, [rafael, ME, dana], "Crying at the fado one. Thank you!"),
    ])
    life.task_lists.append(TaskList("Lisbon trip", [
        Task("Check passport expiry", at(d(2025, 1, 24), "10:00"), d(2025, 1, 31), at(d(2025, 1, 25), "11:00")),
        Task("Book flights (AT 214 / AT 215)", at(d(2025, 1, 24), "10:01"), d(2025, 2, 5), at(d(2025, 2, 2), "19:25")),
        Task("Buy EU plug adapters", at(d(2025, 2, 13), "18:00"), d(2025, 3, 6), at(d(2025, 3, 4), "17:10")),
        Task("Download offline map of Lisbon", at(d(2025, 3, 5), "21:00"), d(2025, 3, 7), at(d(2025, 3, 7), "22:30")),
        Task("Buy Sintra train tickets", at(d(2025, 3, 5), "21:02"), d(2025, 3, 10), at(d(2025, 3, 10), "21:15")),
        Task("Settle Lisbon expenses", at(d(2025, 3, 15), "20:00"), d(2025, 3, 22), at(d(2025, 3, 20), "19:35")),
    ], tag="tasks.lisbon"))
    life.saved["Lisbon 2025"] = [("casalumen", "where we stay"), ("belem", "go early"), ("lxfactory", ""),
                                 ("timeout", "lunch day 4"), ("pena", "Sintra day"), ("tasca", "fado Wednesday"),
                                 ("miradouro", "sunset")]
    life.reviews.append(("tasca", 5, "Best night of the trip: tiny room, incredible fado, kind staff.",
                         at(d(2025, 3, 13), "10:20", "lisbon")))
    plan = [(d(2025, 3, 9), ["lis", "casalumen", "miradouro"]), (d(2025, 3, 10), ["belem", "casalumen"]),
            (d(2025, 3, 11), ["pena", "casalumen"]), (d(2025, 3, 12), ["timeout", "tasca", "casalumen"]),
            (d(2025, 3, 13), ["lxfactory", "casalumen"]), (d(2025, 3, 14), ["timeout", "miradouro", "casalumen"]),
            (d(2025, 3, 15), ["lis"])]
    for day, stops in plan:
        hour = 8
        for stop in stops:
            minutes = 600 if stop == "casalumen" else rng.randint(70, 200)
            life.visit(stop, at(day, f"{hour:02d}:{rng.randint(0, 50):02d}", "lisbon"), minutes)
            hour = min(21, hour + max(2, minutes // 60 + 1))
    for day, query in [(d(2025, 1, 25), "lisbon alfama guesthouse two rooms"),
                       (d(2025, 2, 1), "atlantica air PAV LIS march"),
                       (d(2025, 3, 10), "tram 28 lisbon schedule"),
                       (d(2025, 3, 10), "pastel de nata belem opening hours")]:
        zone = "lisbon" if LISBON[0] < day <= LISBON[1] else "home"
        life.search(at(day, f"{rng.randint(9, 22):02d}:{rng.randint(0, 59):02d}", zone), "Search",
                    f"Searched for {query}", "https://www.google.com/search?q=" + query.replace(" ", "+"))
    for day, query in [(d(2025, 3, 9), "Casa Lumen Guesthouse"), (d(2025, 3, 11), "Lisboa Rossio station")]:
        life.search(at(day, "07:50", "lisbon"), "Maps", f"Searched for {query}",
                    "https://www.google.com/maps/search/" + query.replace(" ", "+"))
    # Months later, Lisbon comes up again: not part of the trip.
    life.chat("lisbon", at(d(2025, 9, 14), "18:20"), [(sofia, "remember Lisbon? we should do Porto next spring"),
                                                      (dana, "yes!!"), (ME, "only if there's fado")],
              tag="lisbon.later")


def iceland(life: Life) -> None:
    dana, theo, ama = p("dana"), p("theo"), p("ama")
    life.space("iceland", "Iceland ring road 2026", [ME, dana, theo])
    life.thread("Graduation trip: Iceland?", [
        (at(d(2026, 3, 25), "20:00"), theo, [ME, dana],
         "Graduation trip idea: Iceland's Ring Road in June. Eight days, a 4x4, waterfalls, hot pools. I've "
         "already got the week off. Thoughts?"),
        (at(d(2026, 3, 25), "20:31"), dana, [theo, ME], "My cousin Ama lives in Reykjavik! She'd love to host a "
         "dinner. I'm in."),
        (at(d(2026, 3, 25), "21:12"), ME, [theo, dana], "YES. The week after graduation, June 6-14? I'll start "
         "the chat."),
    ], tag="iceland.idea")
    life.thread("Mira, meet my cousin Ama (Reykjavik!)", [
        (at(d(2026, 4, 9), "17:45"), dana, [ME, ama],
         "Ama, meet Mira, my roommate and the best trip planner I know. Mira, meet Ama, my cousin, who has lived in "
         "Reykjavik for six years and knows every hot pool on the island.\n\nWe're coming June 6-14. Ama, Mira has "
         "a hundred questions about the Ring Road; I said you wouldn't mind.\n\nDana"),
    ], quote=False, tag="intro.ama")
    life.thread("Iceland questions from Dana's roommate", [
        (at(d(2026, 4, 12), "10:20"), ME, [ama],
         "Hi Ama,\n\nThanks for being willing to help! Three questions: is June 7 good for dinner, is the F-road "
         "to Landmannalaugar open in early June, and which hot pool would you not skip?\n\nMira"),
        (at(d(2026, 4, 13), "08:05"), ama, [ME],
         "Hi Mira!\n\nJune 7 is perfect, come at 7, I'll make plokkfiskur. The F-roads usually open late June, so "
         "skip Landmannalaugar this time. And don't skip Mývatn Nature Baths: quieter than the Blue Lagoon and "
         "half the price.\n\nAma", (), "iceland.ama-reply"),
        (at(d(2026, 4, 13), "09:30"), ME, [ama], "Perfect, thank you! See you June 7.\n\nMira"),
    ], tag="iceland.ama")
    life.mail(at(d(2026, 4, 14), "20:40"), AIRLINE, [ME], "Your Atlantica Air booking confirmation: Port Avalon to "
              "Reykjavik, June 6", "Booking confirmation ZT4K9P\n\nPassenger: MIRA CASTELLANOS\n\nOutbound: AT 310, "
              "Saturday June 6, 2026. Departs Port Avalon (PAV) 9:15pm, arrives Reykjavik-Keflavík (KEF) 6:40am "
              "(+1).\nReturn: AT 311, Sunday June 14, 2026. Departs Keflavík (KEF) 4:20pm, arrives Port Avalon (PAV) "
              "6:10pm.\n\nFare paid: $712.10.\n\nThis mailbox is not monitored.", tag="iceland.flight")
    life.mail(at(d(2026, 4, 20), "21:00"), NORDLYS, [ME], "Your 4x4 rental is confirmed: Keflavík, June 7-14",
              "Reservation NL-30981\n\nVehicle: Dacia Duster 4x4 or similar\nPick-up: Keflavík Airport, Sunday 7 "
              "June 2026, 07:30\nDrop-off: Keflavík Airport, Sunday 14 June 2026, 13:30\nGravel protection: "
              "included\nTotal: ISK 168,400, paid\n\nThis message was sent automatically.", tag="iceland.car")
    life.mail(at(d(2026, 5, 2), "19:15"), WANDERSTAY, [ME], "Booking confirmed: Fjallakot Guesthouse, Vík (Jun 9 - "
              "Jun 10)", "Your stay is confirmed.\n\nFjallakot Guesthouse\nAusturvegur 7, 870 Vík, Iceland\nCheck-in: "
              "Tuesday, June 9, 2026 from 3:00pm\nCheck-out: Wednesday, June 10, 2026 by 10:00am\nRooms: 1 family "
              "room, 3 guests\n\nReservation number WS-77310952.")
    life.thread("Iceland: packing list and driving plan", [
        (at(d(2026, 6, 1), "19:00"), theo, [ME, dana],
         "Driving plan for Iceland: I'll take the first day (Golden Circle), Dana the south coast, Mira the east "
         "fjords. Pack layers, a rain shell and swimsuits. Mira, is the plan doc final?"),
        (at(d(2026, 6, 1), "19:40"), ME, [theo, dana],
         "Final! It's in the folder. Day 3 is Seljalandsfoss and Skógafoss on the way to Vík, where we sleep at "
         "Fjallakot."),
    ])
    life.event("Flight AT 310 PAV-KEF", at(ICELAND[0], "21:15"), 445, location="Port Avalon International Airport")
    life.event("Iceland Ring Road", at(d(2026, 6, 7), "07:30", "iceland"), 7 * 24 * 60, location="Iceland",
               attendees=(dana, theo), zone="iceland")
    life.event("Dinner at Ama's", at(d(2026, 6, 7), "19:00", "iceland"), 150, location="Bergstaðastræti 52, Reykjavík",
               attendees=(dana, theo, ama), zone="iceland")
    rng = life.rng("iceland")
    chat = [
        (d(2026, 4, 2), "21:00", [(ME, "Iceland chat! flights first"), (theo, "June 6 out, June 14 back?"),
                                  (dana, "yes")]),
        (d(2026, 4, 9), "18:00", [(dana, "introduced Mira to Ama, she's hosting dinner on the 7th"),
                                  (theo, "legend")]),
        (d(2026, 4, 14), "20:45", [(ME, "flights booked, AT 310 June 6 9:15pm"), (theo, "booked mine too"),
                                   (dana, "same")]),
        (d(2026, 4, 20), "21:05", [(ME, "4x4 from Nordlys, pick up at KEF 7:30 on the 7th"),
                                   (theo, "gravel protection?"), (ME, "included")]),
        (d(2026, 4, 22), "22:10", [(ME, "draft ring road plan in the folder, 8 days counterclockwise"),
                                   (dana, "Jökulsárlón on day 4 🧊")]),
        (d(2026, 5, 2), "19:20", [(ME, "booked Fjallakot in Vík for the 9th"), (theo, "nice")]),
        (d(2026, 5, 20), "12:00", [(theo, "graduation is saturday right?"), (ME, "yes! then Iceland in 3 weeks")]),
        (d(2026, 6, 4), "20:30", [(dana, "packing list?"), (ME, "in the folder. layers, rain shell, swimsuit")]),
        (d(2026, 6, 7), "08:10", [(theo, "car picked up, Iceland here we come"), (dana, "it's so bright")]),
        (d(2026, 6, 8), "18:30", [(ME, "Þingvellir and Gullfoss done, Geysir went off twice"),
                                  (theo, "best day so far")]),
        (d(2026, 6, 9), "14:00", [(dana, "Seljalandsfoss you can walk behind!!"), (ME, "soaked, worth it"),
                                  (theo, "Skógafoss next")]),
        (d(2026, 6, 10), "16:40", [(ME, "Jökulsárlón 🧊"), (dana, "seals!!")]),
        (d(2026, 6, 12), "20:15", [(theo, "Mývatn baths were perfect, Ama was right"), (ME, "telling her")]),
        (d(2026, 6, 14), "19:00", [(dana, "home. best graduation trip ever"), (ME, "❤️"),
                                   (theo, "photos tomorrow")]),
    ]
    for day, hhmm, lines in chat:
        zone = "iceland" if ICELAND[0] < day <= ICELAND[1] else "home"
        life.chat("iceland", at(day, hhmm, zone), lines, tag="iceland.chat" if day == d(2026, 6, 9) else None)
    life.ask("8-day Iceland Ring Road in June", [
        (at(d(2026, 4, 22), "21:30"), "Plan an 8-day Ring Road trip in Iceland in early June for three people in a "
         "4x4, starting and ending at Keflavík, with a night in Vík and the Mývatn Nature Baths."),
        (at(d(2026, 4, 22), "21:31"), "Day 1: Reykjavík. Day 2: Golden Circle (Þingvellir, Geysir, Gullfoss). "
         "Day 3: south coast, Seljalandsfoss and Skógafoss, night in Vík. Day 4: Reynisfjara, then Jökulsárlón. "
         "Day 5: east fjords to Egilsstaðir. Day 6: Mývatn Nature Baths, Akureyri. Day 7: back to Reykjavík via "
         "the west. Day 8: fly home."),
    ], tag="iceland.ai")
    life.doc("Travel/Iceland 2026/ring-road-plan.md", at(d(2026, 5, 28), "22:00"),
             "# Iceland Ring Road plan\n\nJune 6-14, 2026: Mira, Dana, Theo. 4x4 from Nordlys (KEF, 7:30 on the "
             "7th).\n\n- Day 1 (Jun 7): Reykjavík, Hallgrímskirkja, dinner at Ama's at 7\n- Day 2 (Jun 8): Golden "
             "Circle: Þingvellir, Geysir, Gullfoss\n- Day 3 (Jun 9): south coast: Seljalandsfoss and Skógafoss, "
             "night at Fjallakot Guesthouse in Vík\n- Day 4 (Jun 10): Reynisfjara, Jökulsárlón glacier lagoon\n- Day 5 "
             "(Jun 11): east fjords to Egilsstaðir\n- Day 6 (Jun 12): Mývatn Nature Baths, Akureyri\n- Day 7 (Jun 13): "
             "back to Reykjavík\n- Day 8 (Jun 14): AT 311 from KEF at 4:20pm\n", tag="doc.iceland-plan")
    life.doc("Travel/Iceland 2026/packing-list.txt", at(d(2026, 6, 4), "20:20"),
             "Iceland packing list\n\n- rain shell, fleece, thermal layer\n- swimsuit and quick-dry towel\n- "
             "headlamp (not needed, it never gets dark)\n- sleep mask\n- power bank, car charger\n- passport\n")
    life.task_lists.append(TaskList("Iceland trip", [
        Task("Book flights (AT 310 / AT 311)", at(d(2026, 4, 3), "09:00"), d(2026, 4, 15), at(d(2026, 4, 14), "20:45")),
        Task("Rent a 4x4", at(d(2026, 4, 3), "09:01"), d(2026, 4, 25), at(d(2026, 4, 20), "21:05")),
        Task("Book a night in Vík", at(d(2026, 4, 22), "22:20"), d(2026, 5, 5), at(d(2026, 5, 2), "19:20")),
        Task("Buy a gift for Ama", at(d(2026, 5, 20), "12:10"), d(2026, 6, 5), at(d(2026, 6, 3), "17:00")),
        Task("Download offline maps", at(d(2026, 6, 1), "19:45"), d(2026, 6, 5), at(d(2026, 6, 5), "22:00")),
    ], tag="tasks.iceland"))
    life.saved["Iceland 2026"] = [("hallgrims", ""), ("thingvellir", "day 2"), ("seljalandsfoss", "walk behind it"),
                                  ("skogafoss", ""), ("reynisfjara", "careful with the waves"),
                                  ("jokulsarlon", "day 4"), ("myvatn", "Ama's tip"), ("fjallakot", "night of the 9th")]
    plan = [(d(2026, 6, 7), ["kef", "hallgrims", "amaflat"]), (d(2026, 6, 8), ["thingvellir"]),
            (d(2026, 6, 9), ["seljalandsfoss", "skogafoss", "fjallakot"]), (d(2026, 6, 10), ["reynisfjara",
                                                                                            "jokulsarlon"]),
            (d(2026, 6, 12), ["myvatn", "akureyri"]), (d(2026, 6, 14), ["kef"])]
    for day, stops in plan:
        hour = 8
        for stop in stops:
            minutes = rng.randint(60, 180) if stop != "fjallakot" else 900
            if stop == "amaflat":
                hour, minutes = 19, 150
            life.visit(stop, at(day, f"{hour:02d}:{rng.randint(0, 40):02d}", "iceland"), minutes)
            hour = min(21, hour + max(2, minutes // 60 + 1))
    for day, query in [(d(2026, 3, 26), "iceland ring road june 8 days"),
                       (d(2026, 4, 12), "landmannalaugar f-road opening date"),
                       (d(2026, 6, 10), "jökulsárlón boat tour times")]:
        zone = "iceland" if ICELAND[0] < day <= ICELAND[1] else "home"
        life.search(at(day, f"{rng.randint(9, 22):02d}:{rng.randint(0, 59):02d}", zone), "Search",
                    f"Searched for {query}", "https://www.google.com/search?q=" + query.replace(" ", "+"))
    life.thread("Iceland photos", [
        (at(d(2026, 6, 16), "21:30"), theo, [ME, dana],
         "Iceland photos: https://photos.example.com/album/iceland-2026-c1e9. The one of Dana behind Seljalandsfoss "
         "is my new wallpaper."),
        (at(d(2026, 6, 16), "22:02"), ME, [theo, dana], "These are perfect. Sending the Jökulsárlón one to Ama."),
    ])
    life.mail(at(d(2026, 6, 17), "18:00"), ME, [ama], "Thank you from the Ring Road!",
              "Hi Ama,\n\nThank you again for dinner and for the Mývatn tip: it was the best day of the trip. Photo "
              "of the glacier lagoon attached. Come visit Port Avalon sometime!\n\nMira")


def write(life: Life) -> None:
    lisbon(life)
    iceland(life)
