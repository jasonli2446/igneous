"""Mira's history as she states it: her LinkedIn profile and two versions of her resume (2024 and 2026), which
agree on the facts and differ in what each version lists, as real versions do."""
from datetime import date

from igneous.persona.cast import ME, at
from igneous.persona.life import Life

HEADLINE = "Software Engineer at Northwind Robotics | Lakeshore University CS '26"
SUMMARY = ("I build software for clean water: telemetry for autonomous sampling boats at Northwind Robotics, and "
           "Tidepool, open-source beach water-quality alerts for Lake Avalon.")

# (company, title, start, end or None, location, description) as LinkedIn's Positions.csv lists them
POSITIONS = [
    ("Northwind Robotics", "Software Engineer", date(2026, 7, 6), None, "Port Avalon",
     "Wavelink telemetry pipeline for Heron autonomous sampling boats and mooring buoys."),
    ("Northwind Robotics", "Software Engineering Intern", date(2025, 1, 21), date(2026, 5, 29), "Port Avalon",
     "Part-time during the school year, full-time in summer 2025. Protobuf packet parser, Heron depth filtering, "
     "alert rules."),
    ("Harbor Street Coffee", "Barista", date(2022, 9, 1), date(2024, 8, 15), "Port Avalon", ""),
]
EDUCATION = [("Lakeshore University", "Bachelor of Science - BS, Computer Science", date(2022, 8, 1),
              date(2026, 5, 31), "Minor in Environmental Science.")]
PROJECTS = [("Tidepool", "Open-source beach water-quality alerts for Lake Avalon, built with Rafael Ortiz; piloted by "
             "the City of Port Avalon in 2026.", date(2025, 2, 1), None, "https://code.example.org/mira-c/tidepool")]
SKILLS = ["Python", "Go", "TypeScript", "PostgreSQL", "Distributed Systems", "Protocol Buffers", "MQTT", "GIS",
          "Machine Learning", "Kalman Filtering"]
LANGUAGES = [("Spanish", "Native or bilingual proficiency"), ("English", "Native or bilingual proficiency")]


def resumes(life: Life) -> None:
    """A PDF from the fall 2024 job search and the current Word resume."""
    old = [
        "MIRA CASTELLANOS",
        f"Port Avalon | {ME.email} | {ME.phone}",
        "",
        "EDUCATION",
        "Lakeshore University, Port Avalon",
        "B.S. Computer Science, minor in Environmental Science, expected May 2026",
        "Coursework: Data Structures, Algorithms, Operating Systems (in progress), Freshwater Ecology",
        "",
        "EXPERIENCE",
        "Harbor Street Coffee, Port Avalon - Barista, Sep 2022 - Aug 2024",
        "Opened the shop three mornings a week; trained four new baristas; built the shift-swap sheet.",
        "",
        "Lakeshore University Library - Student Assistant, Jan 2023 - May 2024",
        "Helped students with the 3D printers and the media lab.",
        "",
        "PROJECTS",
        "Campus bike-share dashboard (2024): a Python and Postgres dashboard of dock availability from the city's",
        "open data, used by the student government to argue for two new docks.",
        "",
        "SKILLS",
        "Python, Java, SQL, Git, Excel, Spanish (native)",
    ]
    new = [
        ("Mira Castellanos", "Title"),
        (f"Port Avalon | {ME.email} | {ME.phone} | code.example.org/mira-c", None),
        ("Education", "Heading 1"),
        ("Lakeshore University, Port Avalon - B.S. Computer Science, minor in Environmental Science, May 2026", None),
        ("Coursework: Distributed Systems, Operating Systems, Machine Learning, Database Systems, GIS for Water "
         "Resources, Senior Capstone", None),
        ("Experience", "Heading 1"),
        ("Northwind Robotics, Port Avalon - Software Engineering Intern, Jan 2025 - present", None),
        ("Part-time during the school year, full-time in summer 2025, on Wavelink, the telemetry pipeline for "
         "Heron autonomous sampling boats and mooring buoys.", None),
        ("Wrote the v3 protobuf packet parser, backwards compatible with v2 buoys.", None),
        ("Built the Kalman filter that cut Heron depth noise by 70% in the June 2025 field trials.", None),
        ("Fixed false low-oxygen alerts by suppressing readings during sensor cleaning.", None),
        ("Harbor Street Coffee, Port Avalon - Barista, Sep 2022 - Aug 2024", None),
        ("Projects", "Heading 1"),
        ("Tidepool (2025 - present): open-source beach water-quality alerts for Lake Avalon, with Rafael Ortiz. "
         "Piloted by the City of Port Avalon for the 2026 beach season.", None),
        ("Raft-based sharded key-value store (CS 452, Fall 2025), with Alex Chen and Jordan Pike.", None),
        ("Harborline (CS 481 capstone, Spring 2026): a live map of the harbor shuttle.", None),
        ("Skills", "Heading 1"),
        (", ".join(SKILLS) + ". Spanish (native).", None),
    ]
    life.doc("Career/Mira_Castellanos_Resume_2024.pdf", at(date(2024, 9, 3), "21:10"), [old], tag="doc.resume-2024")
    life.doc("Career/Mira_Castellanos_Resume.docx", at(date(2026, 2, 11), "20:45"), new, tag="doc.resume")
