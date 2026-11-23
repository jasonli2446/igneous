"""Where people work: WORKED_AT facts from evidence only, one per person and organization."""
import pytest
from pipeline_data import card, local_models, rows  # noqa: F401

from igneous.graph import organizations, spine

pytestmark = pytest.mark.usefixtures("local_models")


def test_two_spellings_of_one_company_make_one_fact(db):
    """Sam's Google and Mac contact cards spell the company two ways."""
    card(db, "acct/people/7", "Sam Ito", emails=["sam@ito.dev"], orgs=["Acme Inc"])
    card(db, "card-7", "Sam Ito", emails=["sam@ito.dev"], orgs=["Acme Inc."], source="maccontacts")
    db.commit()
    spine.run(db)
    organizations.run(db)
    facts = rows(db, """SELECT p.key, o.name, (SELECT count(*) FROM edge_evidence ev WHERE ev.edge_id = e.id)
                        FROM edges e JOIN nodes p ON p.id = e.src JOIN nodes o ON o.id = e.dst
                        WHERE e.type = 'WORKED_AT' AND e.extractor = 'organizations'""")
    assert len(facts) == 1
    (person, org, evidence), = facts
    assert person == "sam@ito.dev" and org in ("Acme Inc", "Acme Inc.") and evidence == 2
