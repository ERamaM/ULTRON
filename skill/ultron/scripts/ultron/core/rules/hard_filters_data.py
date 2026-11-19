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
# An application-acceptance phrase followed by this is agency boilerplate, not a closed posting
# ("not accepting applications from recruitment agencies"). Filled or closed postings
# ("position filled by recruitment agency") stay closed. The suffix names an application channel
# ("from", "via", "through", "de"), never "by": "applications closed by the agency" is a real closure.
AGENCY_EXEMPTABLE_CLOSED_PHRASES = frozenset({
    "no longer accepting",
    "not accepting applications",
    "applications closed",
    "ya no aceptamos solicitudes",
})
# Up to three words before the channel word cover "applications or CVs from agencies".
CLOSED_STATUS_AGENCY_SUFFIX_PATTERN = (
    r"\s+(?:[\w-]+\s+){0,3}?(?:from|via|through|de)\s+(?:(?:external|third[- ]party|recruitment|staffing|"
    r"unsolicited|recruiting|search)\s+)*(?:recruit\w*|agenc\w*|headhunt\w*|search firms?|seleccion\w*)\b"
)

# Right after the agency suffix, a coordinated non-agency group ("... from agencies or individual applicants")
# means the restriction also covers direct applicants, so it is a real closure.
CLOSED_STATUS_DIRECT_APPLICANT_CONTINUATION_PATTERN = (
    r"\s*,?\s*(?:or|and|nor|y|o|ni)\s+(?:from\s+|de\s+)?(?:(?:individual|direct|private|other|any)\s+)?"
    r"(?:applicants?|candidates?|individuals?|anyone|people|candidat\w*|particulares)\b"
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
