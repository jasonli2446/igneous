"""How a person's name reads.

Outlook and Exchange write people surname first ("Rangel, Ina", "Allen, Phillip K."), and so do the mail headers,
calendars and address books that come from them. `natural_name` puts such a name in the order it is read: "Ina
Rangel", "Phillip K. Allen", and "Errol McLaughlin Jr." for "McLaughlin Jr., Errol". The spine gives every name it
takes for a person in this order (graph/spine.py), so the explorer, search, summaries and the MCP tools show it too.

Only the order changes, and only when the comma separates a surname from given names. A name with no comma or more
than one is left as it is, and so is one whose comma means something else: a suffix or credential after it ("Smith,
Jr.", "Robert W. Huntley, CFP"), a company ("Acme, Inc."), a role or department ("Manager, Sales"), or anything that
doesn't read as one person's name once turned (a list of people, all capitals, a word in lowercase).
"""
from igneous.graph.self_identity import looks_like_person_name

# Generational suffixes. Outlook writes one with the surname ("McLaughlin Jr., Errol"); it follows the surname once the
# name is turned ("Errol McLaughlin Jr."), and resolve.split_name never takes one for the surname.
GENERATIONAL = frozenset({"jr", "sr", "ii", "iii", "iv"})

# Words that say a comma doesn't separate a surname from given names: a credential after a name ("Smith, PhD"), a
# company form ("Acme, Inc."), and the words of roles, departments and shared mailboxes ("Manager, Sales", "Desk,
# Help"). Words that are also names (Head, Dean, Judge, and Md, short for Mohammad) are left out; a credential in
# capitals ("Smith, MD") doesn't read as a name anyway.
NOT_A_NAME = frozenset({
    "phd", "dds", "dvm", "esq", "cpa", "cfa", "cfp", "mba", "rn",
    "inc", "llc", "ltd", "llp", "plc", "corp", "co", "company", "corporation", "incorporated", "limited", "gmbh",
    "group", "holdings", "partners", "associates",
    "manager", "director", "president", "chairman", "officer", "chief", "executive", "assistant", "coordinator",
    "administrator", "admin", "analyst", "specialist", "representative", "consultant", "secretary", "treasurer",
    "team", "department", "dept", "office", "desk", "support", "services", "service", "sales", "marketing",
    "operations", "accounting", "finance", "legal", "communications", "relations", "resources", "staff", "employees",
    "committee", "board", "council", "center", "centre", "help", "info", "news", "mail", "mailbox",
})


def _word(w: str) -> str:
    return w.rstrip(".").lower()


def natural_name(name: str) -> str:
    """`name` in the order it is read: "Rangel, Ina" -> "Ina Rangel", "Allen, Phillip K." -> "Phillip K. Allen",
    "McLaughlin Jr., Errol" -> "Errol McLaughlin Jr.". Any other name comes back unchanged (see the module)."""
    if name.count(",") != 1:
        return name
    surname, given = (part.split() for part in name.split(","))
    suffix = []
    for part in (given, surname):  # "Smith, John Jr." and "Smith Jr., John" both read "John Smith Jr."
        while part and _word(part[-1]) in GENERATIONAL:
            suffix.insert(0, part.pop())
    if not surname or not given or any(_word(w) in NOT_A_NAME for w in surname + given):
        return name
    turned = " ".join(given + surname)
    return " ".join([turned, *suffix]) if looks_like_person_name(turned) else name
