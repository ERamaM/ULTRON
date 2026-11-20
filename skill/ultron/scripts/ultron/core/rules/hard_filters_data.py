from __future__ import annotations

# Vocabulary and compiled signals shared by role matching and filtering policies.
DISALLOWED_TITLE_TERMS_SUBSTR = {"team lead", "tech lead", "senior lead"}
DISALLOWED_TITLE_TERMS_WB = {"head", "manager", "director", "vp"}
HIGH_SENIORITY_IC_TERMS = {"staff", "principal", "lead", "distinguished"}
ROLE_FAMILY_MANAGEMENT_TERMS_WB = {"head", "manager", "director", "vp"}
ROLE_FAMILY_NEGATIVE_TERMS_WB = {"recruiter", "sales", "account", "marketing", "finance", "legal", "hr"}
# Word-boundary matched: plain substring matching falsely hit "Internal Tools",
# "International", "postgraduate", "Sentry", etc. ("entry" with \b still matches
# "entry-level" / "entry level" because "-" and " " are non-word characters).
JUNIOR_TITLE_TERMS_WB = {"intern", "junior", "graduate", "entry"}
# Student and internship levels in titles (English, German, Dutch, French, Spanish, Portuguese). They only
# discard a posting for a profile whose ``seniority.excluded_terms`` names one of STUDENT_LEVEL_TRIGGER_TERMS.
# The Dutch and French "stage" is only read at the start of a title ("Stage Data Analyst"), never in
# "Early Stage".
STUDENT_LEVEL_TITLE_TERMS = (
    "intern", "interns", "internship", "working student", "student assistant", "werkstudent", "werkstudentin",
    "werkstudierende", "praktikant", "praktikantin", "praktikum", "pflichtpraktikum", "trainee", "apprentice",
    "apprenticeship", "azubi", "stagiair", "stagiaire", "afstudeerstage", "meewerkstage", "becario", "becaria",
    "practicas", "estagio", "estagiario", "estagiaria",
)
# Roles that run a student programme: after one of these nouns a student word names the programme
# ("Internship Coordinator", "Apprenticeship Programme Manager"), not the level of the role.
STUDENT_PROGRAMME_ROLE_NOUNS = (
    "coordinator", "manager", "lead", "officer", "recruiter", "specialist", "director", "advisor", "adviser",
    "partner", "program", "programme", "administrator", "consultant", "head", "coach", "mentor",
)
STUDENT_LEVEL_TRIGGER_TERMS = frozenset({*STUDENT_LEVEL_TITLE_TERMS, "student", "students", "stage"})
SENIOR_REQUIRED_TERMS = {"senior", "sr.", "sr "}
DISALLOWED_REMOTE_TERMS = {
    "hybrid",
    "on-site",
    "onsite",
    "in-office",
    "office presence",
    "hibrid",
    "presencial",
    "en oficina",
}
CLOSED_STATUS_PHRASES = {
    "position filled",
    "the job you are trying to apply for has been filled",
    "job expired",
    "no longer accepting",
    "not accepting applications",
    "applications closed",
    "posting closed",
    "role closed",
    "oferta cerrada",
    "vacante cerrada",
    "plaza cubierta",
    "proceso cerrado",
    "proceso finalizado",
    "inscripcion cerrada",
    "ya no aceptamos solicitudes",
}


def _closed_posting_phrases() -> set[str]:
    """Explicit "this posting is filled / no longer available" phrases (EN, ES, DE, FR), accent-folded.

    Each phrase names the posting ("position", "oferta", "Stelle", "offre", ...) so open postings that use
    the same verbs ("the form has been filled", "die Stelle soll besetzt werden") never match.
    """
    phrases = {
        "deadline for applications has passed",
        "application deadline has passed",
        "applications deadline has passed",
        "no se admiten mas solicitudes",
        "no se admiten mas candidaturas",
        "no se aceptan mas solicitudes",
        "no se aceptan mas candidaturas",
    }
    for noun in ("position", "role", "job", "vacancy", "posting", "opening", "requisition", "job offer"):
        for state in (
            "has been filled", "has already been filled", "is no longer available", "is no longer active",
            "is closed", "is now closed", "has expired",
        ):
            phrases.add(f"{noun} {state}")
    for noun in ("oferta", "vacante", "plaza", "puesto", "posicion"):
        for ending in ("a", "o"):
            phrases.update({
                f"{noun} esta cerrad{ending}", f"{noun} ha sido cubiert{ending}",
                f"{noun} ya ha sido cubiert{ending}", f"{noun} ya esta cubiert{ending}",
            })
        phrases.add(f"{noun} ya no esta disponible")
    for noun in ("stelle", "position", "stellenanzeige", "stellenangebot", "anzeige"):
        for state in (
            "ist besetzt", "ist bereits besetzt", "ist leider bereits besetzt", "wurde besetzt",
            "wurde bereits besetzt", "ist nicht mehr verfugbar", "ist leider nicht mehr verfugbar",
        ):
            phrases.add(f"{noun} {state}")
    for noun in ("offre", "poste", "annonce", "offre d'emploi"):
        for state in (
            "n'est plus disponible", "n'est plus active", "est pourvu", "est pourvue", "est deja pourvu",
            "est deja pourvue", "a ete pourvu", "a ete pourvue",
        ):
            phrases.add(f"{noun} {state}")
    return phrases


# One list for every closed-status check: the hard filters and the extractor's ``is_open_hint``.
CLOSED_STATUS_PHRASES |= _closed_posting_phrases()
# Agency boilerplate ("not accepting applications from recruitment agencies.") is not a closed posting.
# Conservative rule: an application-acceptance phrase is exempt only when a channel word ("from", "via",
# "through", "de"; never "by", as in "closed by the agency") is followed by a list made only of agency or
# recruiter terms, joined by commas/"and"/"or", and that list ends the sentence. Any other continuation
# ("... or individual applicants", "... and candidates must apply directly") keeps the phrase closed, which
# is the behavior without the exemption. Filled or closed postings ("position filled ...") are never exempt.
AGENCY_EXEMPTABLE_CLOSED_PHRASES = frozenset({
    "no longer accepting",
    "not accepting applications",
    "applications closed",
    "ya no aceptamos solicitudes",
    "no se admiten mas solicitudes",
    "no se admiten mas candidaturas",
    "no se aceptan mas solicitudes",
    "no se aceptan mas candidaturas",
})
_AGENCY_TERM = (
    r"(?:(?:external|third[- ]party|recruitment|staffing|unsolicited|recruiting|executive)\s+)*"
    r"(?:agenc\w*|recruiters?|headhunt\w*|(?:search|staffing|recruitment)\s+firms?)"
    r"(?:\s+de\s+(?:seleccion|reclutamiento|colocacion|empleo))?"
)
_AGENCY_LIST_SEPARATOR = r"(?:\s*,\s*(?:(?:and|or|y|o)\s+)?|\s+(?:and|or|y|o)\s+)"
# Up to three words before the channel word cover "applications or CVs from agencies".
CLOSED_STATUS_AGENCY_SUFFIX_PATTERN = (
    r"\s+(?:[\w-]+\s+){0,3}?(?:from|via|through|de)\s+(?:(?:the|las?|los)\s+)?"
    + _AGENCY_TERM + r"(?:" + _AGENCY_LIST_SEPARATOR + _AGENCY_TERM + r")*"
    + r"[ \t]*(?:[.!;]|\r?\n|$)"
)

LOCATION_CONTEXT_TERMS = {
    "remote",
    "location",
    "located",
    "based in",
    "eligible",
    "open to",
    "available in",
    "work from",
    "hiring in",
    "candidates in",
    "residents",
    "position",
    "role",
    "restricted to",
}
EXPLICIT_HIRING_SCOPE_TERMS = {
    "open to candidates in",
    "hiring in",
    "remote from",
    "eligible to work in",
    "must be located in",
    "candidates based in",
    "we can hire in",
    "restricted to",
}
INCIDENTAL_GEO_TERMS = {
    "offices in",
    "office in",
    "customers in",
    "clients in",
    "gdpr",
    "operations in",
    "entity in",
    "compliance in",
    "headquartered in",
    "headquarters in",
    "r&d hubs",
    "hubs across",
    "story began in",
    "founded in",
}
GEO_BODY_CONTEXT_TERMS = {
    "location",
    "remote",
    "hiring",
    "eligible",
    "open to candidates",
    "candidates in",
    "right to work",
    "work arrangement",
    "working arrangement",
    "work model",
    "hybrid",
    "onsite",
    "on-site",
    "office",
    "in-person",
    "relocation",
}


REMOTE_HINT_TERMS = {
    "remote",
    "remoto",
    "work from home",
    "distributed team",
    # Work-arrangement phrasing is a remote hint; bare "distributed" ("distributed systems") is not.
    "distributed work",
    "distributed workforce",
    "distributed company",
    "distributed first",
    "distributed-first",
    "teletrabajo",
    "trabajo remoto",
    "en remoto",
    "trabajo a distancia",
}
ONSITE_TERMS = {"on-site", "onsite", "in-office", "office presence", "in office"}
# ONSITE_TERMS match as whole terms; an office-suite product right after an "office" term on the same line
# ("in Office 365") is not a work mode. "on-site\nSuite 200" (an address) still counts.
ONSITE_TERM_PRODUCT_SUFFIX_PATTERN = r"[ \t]*(?:365|suite)\b"
ONSITE_BENEFIT_CONTEXT_TERMS = {
    "therapy",
    "counseling",
    "counselling",
    "mental health",
    "wellness",
    "childcare",
    "nursery",
    "gym",
    "fitness",
    "catering",
    "cafeteria",
    "meals",
    "lunch",
    "massage",
    "parking",
}
ONSITE_NEGATION_TERMS = {
    "no on-site",
    "no onsite",
    "on-site optional",
    "onsite optional",
    "without on-site",
    "no office attendance",
    "office not required",
    "no in-office",
    "optional office",
    "office optional",
    "remote or in-office",
    "remote or onsite",
    "remote or on-site",
    "work from home or office",
}
REMOTE_CONFLICT_NEGATION_TERMS = {
    "no relocation",
    "relocation not required",
    "without relocation",
    "office attendance not required",
    "in-person optional",
    "no in-person requirement",
    "no in person requirement",
    "office-first not required",
}
ONSITE_BODY_CONFLICT_TERMS = {
    "days per week",
    "days/week",
    "office days",
    "office attendance",
    "in-person required",
    "relocation required",
    "mandatory relocation",
    "must relocate",
    "office-first",
}
ONSITE_BODY_CONFLICT_PATTERNS = (
    r"\b\d+\s+days?\s*(?:/|per)\s*week\b",
    r"\b\d+\s+days?\s+in\s+office\b",
    r"\boffice\s+\d+\s+days?\b",
)
HYBRID_WORK_CONTEXT_TERMS = {
    "work arrangement",
    "working arrangement",
    "work model",
    "remote policy",
    "onsite",
    "on-site",
    "in-office",
    "in office",
    "office",
    "days per week",
    "week in office",
    "per week",
}
HYBRID_NEGATION_TERMS = {
    "not hybrid",
    "non-hybrid",
    "no hybrid",
    "without hybrid",
    "instead of hybrid",
    "optional hybrid",
    "hybrid optional",
    "remote or hybrid",
    "hybrid or remote",
}
HYBRID_TECH_CONTEXT_TERMS = {"hybrid cloud", "hybrid search", "hybrid model", "hybrid approach"}
# Narrower context for "closed"/"expired": generic words like "hiring" made
# "recently closed funding round, we're hiring" kill live jobs.
CLOSED_STATUS_POSITION_CONTEXT_TERMS = {
    "position",
    "role",
    "posting",
    "application",
    "applications",
    "vacancy",
    "job",
}
CLOSED_STATUS_EXEMPT_TERMS = {
    "funding round",
    "round closed",
    "closed round",
    "deal",
    "closed-source",
    "closed source",
    "closed beta",
    "series",
}
ROLE_BRIDGE_TITLE_TERMS = {
    "software engineer",
    "platform engineer",
    "backend engineer",
    "data engineer",
    "systems engineer",
    "research engineer",
    "research scientist",
    "applied scientist",
    "data scientist",
    "infrastructure engineer",
}
ROLE_NEGATIVE_BODY_TERMS = {
    "pipeline sales",
    "sales quota",
    "customer success",
    "marketing campaign",
    "paid media",
    "seo",
    "content marketing",
    "recruiting coordinator",
}

# Level words that never name a role family on their own ("Senior" in a target title, "lead" in "design lead").
ROLE_LEVEL_WORDS = frozenset({
    "senior", "sr", "staff", "principal", "lead", "distinguished", "junior", "jr", "mid", "level", "entry",
    "head", "chief", "intern", "trainee", "i", "ii", "iii", "iv",
})
# Prepositions that join a role to its field in Spanish, Portuguese and French titles ("analista de datos").
ROLE_FIELD_PREPOSITIONS = frozenset({"de", "del", "da", "do", "das", "dos", "des", "du"})
ROLE_FAMILY_PASS_SCORE = 2
ROLE_FAMILY_CLEAR_REJECT_SCORE = -2


STRONG_REMOTE_POSITIVE_TERMS = {
    "remote-first",
    "remote first",
    "remote friendly",
    "remote-friendly",
    "fully remote",
    "100% remote",
    "office optional",
    "office not required",
    "no office attendance",
    "100% remoto",
    "totalmente remoto",
    "100% teletrabajo",
    "teletrabajo completo",
}
