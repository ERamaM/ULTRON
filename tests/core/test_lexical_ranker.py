from datetime import date

from ultron.core.ranker.lexical import LexicalRanker


def dossier(identity, company, title, **facts):
    return {"id": identity, "company": company, "title": title, "status": "open", **facts}


def test_relevance_and_exclusions_are_soft():
    ranker = LexicalRanker({"criteria": {
        "target_titles": ["Clinical Nurse"], "excluded_titles": ["student"],
        "location_scope": {"preferred": ["Quebec"]}, "specialties": ["Pediatrics"],
    }}, today=date(2026, 11, 26))
    relevant = dossier("a", "Regional Health", "Clinical Nurse", location="QuÃ©bec", role_excerpt="Pediatrics")
    noise = dossier("b", "Another", "Office Clerk", role_excerpt="Worked with a clinical nurse", location="Australia")
    excluded = dossier("c", "School", "Clinical Nurse (Student)", location="QuÃ©bec", role_excerpt="Pediatrics")
    result = ranker.shortlist([noise, excluded, relevant], 10)
    assert result[0] == relevant
    assert {row["id"] for row in result} == {"a", "b", "c"}


def test_open_first_diversity_and_freshness():
    ranker = LexicalRanker({}, query="nurse", today=date(2026, 11, 26))
    rows = [dossier(str(i), "Acme", f"Nurse {i}", posted="2026-11-10") for i in range(6)]
    rows.extend([
        dossier("open", "Other", "Technician", posted="2026-11-26", memory="new", source="watch"),
        {**rows[0], "status": "listed", "board": "greenhouse:acme"},
        {**dossier("listed", "Different", "Nurse"), "status": "listed"},
        dossier("dupe", "ACME", "Nurse 1", posted="2026-11-12"),
    ])
    ranked = ranker.shortlist(rows, 10)
    assert sum(row["company"].casefold() == "acme" for row in ranked) == 3
    assert sum(row["title"] == "Nurse 1" for row in ranked) <= 1
    assert ranked[-1]["id"] == "listed"
    assert all(row["status"] == "open" for row in ranked[:-1])
