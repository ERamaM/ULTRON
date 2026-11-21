"""Tests for deterministic CV section splitting and experience timeline analysis."""

from __future__ import annotations

from datetime import date

from ultron.cv import experience as cv_experience


def test_multilingual_headings_and_date_formats_with_deterministic_as_of():
    cv_text = """
Jane Example
Madrid, Spain

### Experiencia Profesional
Senior Data Engineer | Acme Labs
ene. 2022 - actualidad
- Built Spark and Kafka streaming pipelines.

Backend Engineer | Beta Corp
03/2019 - 12/2021
- Developed Python APIs.

### Formación Académica
MSc Computer Science
2014 - 2016
"""
    res = cv_experience.analyze_experience(cv_text, as_of=date(2026, 6, 1))
    # 2022-01..2026-06 = 54 months; 2019-03..2021-12 = 34 months; total = 88 months (7.3 yrs)
    assert res["total_months"] == 88
    assert res["total_years"] == 7.3
    assert res["internship_months"] == 0
    assert res["highest_education"] == "masters"
    assert res["chronology_ok"] is True
    assert res["chronology_order"] == "reverse_chronological"
    assert res["roles_count"] == 2
    assert "experience" in res["sections_present"]
    assert "education" in res["sections_present"]


def test_french_german_portuguese_italian_dates_and_year_only_without_spaces():
    cv_text = """
Berufserfahrung
Software Engineer - Beispiel GmbH (März 2020 - heute)
- Microservices in Go.

Data Analyst - Paris Tech
juin 2018 - févr. 2020

Junior Developer - Lisboa Soft
2016-2018

Formazione
PhD in Machine Learning
2012 - 2016
"""
    res = cv_experience.analyze_experience(cv_text, as_of=date(2026, 3, 1))
    # 2016-01..2017-12 (24m), 2018-06..2020-02 (21m), 2020-03..2026-03 (73m) -> total 118m
    assert res["total_months"] == 118
    assert res["highest_education"] == "phd"
    assert res["career_gaps_months"] == 5  # Jan..May 2018 = 5 months


def test_overlapping_intervals_deduplicated_and_internships_separated():
    cv_text = """
Work Experience
Lead ML Engineer | Core AI | Jan 2022 - Dec 2024
Technical Consultant (Part-time) | Side Co | Jan 2023 - Dec 2023
Data Science Intern | StartUp | Jun 2021 - Dec 2021
Werkstudent Python | Uni Lab | Jan 2021 - May 2021
"""
    res = cv_experience.analyze_experience(cv_text, as_of=date(2025, 1, 1))
    # Non-intern intervals: 2022-01..2024-12 (36m) + overlapping 2023-01..2023-12 -> merged = 36m
    assert res["total_months"] == 36
    assert res["total_years"] == 3.0
    # Intern intervals: 2021-01..2021-05 (5m) + 2021-06..2021-12 (7m) = 12m
    assert res["internship_months"] == 12
    assert res["total_months_including_internships"] == 48
    assert "staff_or_lead_title" in res["leadership_signals"]


def test_profile_relevance_and_chronological_order_warning():
    cv_text = """
Work Experience
Frontend Developer | Web Co | 2018 - 2020
- React and CSS interfaces.

Senior Data Engineer | Cloud Co | Jan 2021 - Jan 2025
- Built dbt, Airflow, and Spark pipelines.
"""
    profile = {
        "slug": "data-eng",
        "name": "Data Eng",
        "criteria": {
            "role_focus_terms": ["data engineer"],
            "target_titles": ["Senior Data Engineer"],
            "role_signal_terms": ["dbt", "airflow", "spark"],
        },
    }
    res = cv_experience.analyze_experience(cv_text, profile=profile, as_of=date(2026, 1, 1))
    assert res["total_months"] == 24 + 49
    assert res["relevant_months"] == 49
    assert res["relevant_years"] == 4.1
    assert res["chronology_ok"] is False
    assert res["chronology_order"] == "chronological"


def test_fallback_without_experience_heading_and_edge_cases():
    cv_text = """
Jane Doe
Senior Backend Engineer | 2025 - 2023
Software Engineer | 2021 - 2021
Education
BSc Computer Science | 2017 - 2021
"""
    res = cv_experience.analyze_experience(cv_text, as_of=date(2026, 1, 1))
    assert "no_explicit_experience_section" in res["warnings"]
    # Inverted 2025 - 2023 is ignored; 2021 - 2021 counts 6 months; Education dates excluded
    assert res["total_months"] == 6
    assert res["highest_education"] == "bachelors"
    assert res["inferred_seniority_band"] == "junior"
    # Sentences mentioning experience are not section headings
    assert cv_experience.match_section_heading("I have five years of experience building distributed systems.") is None


def test_year_only_range_preserves_reported_end_year_and_duration():
    cv_text = """
Work Experience
Senior Engineer | Acme | 2020 - 2024
"""
    res = cv_experience.analyze_experience(cv_text, as_of=date(2026, 1, 1))
    assert res["total_months"] == 48
    assert res["roles"][0]["start"] == "2020"
    assert res["roles"][0]["end"] == "2024"

    cv_adj = """
Work Experience
Staff Engineer | Beta | 2024 - 2026
Senior Engineer | Acme | 2020 - 2024
"""
    res_adj = cv_experience.analyze_experience(cv_adj, as_of=date(2026, 6, 1))
    assert res_adj["total_months"] == 72
    assert [(r["start"], r["end"]) for r in res_adj["roles"]] == [("2024", "2026"), ("2020", "2024")]


def test_two_line_role_heading_with_location_on_date_line():
    cv_text = """
## Experience
### Senior ML Engineer — Beta
#### Jan 2022 - Present | Madrid, Spain
- Trained PyTorch models.

### Data Science Intern — Acme
#### Jun 2021 - Dec 2021 | Madrid, Spain
- Analyzed tabular features.
"""
    profile = {
        "slug": "ml-eng",
        "name": "ML Eng",
        "criteria": {
            "role_focus_terms": ["ml engineer"],
            "target_titles": ["Senior ML Engineer"],
            "role_signal_terms": ["pytorch"],
        },
    }
    res = cv_experience.analyze_experience(cv_text, profile=profile, as_of=date(2026, 1, 1))
    assert res["roles_count"] == 2
    ml_role, intern_role = res["roles"]
    assert ml_role["is_internship"] is False
    assert ml_role["matches_profile"] is True
    assert "Senior ML Engineer" in ml_role["title_hint"]
    assert intern_role["is_internship"] is True
    assert "Data Science Intern" in intern_role["title_hint"]
    # 2022-01..2026-01 = 49 months; internship excluded from total_months
    assert res["total_months"] == 49
    assert res["internship_months"] == 7


def test_dual_section_headings_with_ampersand_and_slash_after_education():
    for heading in ("Experience & Projects", "Experience / Projects"):
        cv_text = f"""
## Education
BSc Computer Science | 2015 - 2019

## {heading}
Senior Engineer | Acme | Jan 2020 - Dec 2024
- Built distributed systems.
"""
        res = cv_experience.analyze_experience(cv_text, as_of=date(2026, 1, 1))
        assert "experience" in res["sections_present"]
        assert res["total_months"] == 60


def test_profile_relevance_stops_before_next_role_heading():
    cv_text = """
## Experience
Customer Support
Jan 2022 - Dec 2023
- Handled customer tickets.

Backend Engineer
Jan 2020 - Dec 2021
- Built Python APIs.
"""
    profile = {
        "slug": "backend-eng",
        "name": "Backend Eng",
        "criteria": {
            "role_focus_terms": ["backend engineer"],
            "target_titles": ["Backend Engineer"],
        },
    }
    res = cv_experience.analyze_experience(cv_text, profile=profile, as_of=date(2026, 1, 1))
    assert res["roles_count"] == 2
    assert res["roles"][0]["matches_profile"] is False
    assert res["roles"][1]["matches_profile"] is True
    assert res["relevant_months"] == 24


def test_role_title_with_and_is_not_read_as_a_section_heading():
    # LinkedIn PDF export: a company groups several roles under its total tenure, and one title
    # contains "Research and Development", which must not switch to a "research" section.
    cv_text = """
Experiencia
Example Fibers Ltd
Product Development Engineer
septiembre de 2020 - Present (5 años 8 meses)
- Led industrial trials.
Example Boards S.A.
5 años 10 meses
Research and Development (R&D) Process Engineer
diciembre de 2014 - junio de 2019 (4 años 7 meses)
- Improved resin impregnation processes.
Internship (Operations and R&D)
septiembre de 2013 - diciembre de 2014 (1 año 4 meses)
- Supported production.
Educación
PhD in Materials Science · (2012 - 2019)
"""
    res = cv_experience.analyze_experience(cv_text, as_of=date(2026, 11, 1))
    assert [h["section"] for h in res["detected_headings"]] == ["experience", "education"]
    titles = [r["title_hint"] for r in res["roles"]]
    assert titles == [
        "Product Development Engineer",
        "Research and Development (R&D) Process Engineer",
        "Internship (Operations and R&D)",
    ]
    assert [r["is_internship"] for r in res["roles"]] == [False, False, True]
    # 2020-09..2026-11 = 75 months; 2014-12..2019-06 = 55 months
    assert res["total_months"] == 130
    assert res["internship_months"] == 16


def test_linkedin_web_copy_uses_title_line_and_employment_type():
    cv_text = """
Experience
Example Cloud logo

Data Engineer

Example Cloud · Full-time

Jul 2023 - Present · 3 yrs 5 mos

Present

Remote
Software developer

Example Services · Full-time

Sep 2018 - Mar 2019 · 7 mos
Prácticas de desarrollo de software

Example Services · Internship

Apr 2018 - May 2018 · 2 mos
"""
    res = cv_experience.analyze_experience(cv_text, as_of=date(2026, 11, 1))
    roles = res["roles"]
    assert [r["title_hint"] for r in roles] == [
        "Data Engineer · Example Cloud",
        "Software developer · Example Services",
        "Prácticas de desarrollo de software · Example Services",
    ]
    assert [r["is_internship"] for r in roles] == [False, False, True]
    # 2023-07..2026-11 = 41 months; 2018-09..2019-03 = 7 months
    assert res["total_months"] == 48
    assert res["internship_months"] == 2


def test_internship_detected_from_employment_type_line_only():
    cv_text = """
Experience
Software Engineer
Example Labs · Internship
Jun 2021 - Aug 2021 · 3 mos
"""
    res = cv_experience.analyze_experience(cv_text, as_of=date(2026, 1, 1))
    assert res["roles"][0]["is_internship"] is True
    assert res["roles"][0]["title_hint"] == "Software Engineer · Example Labs"
    assert res["total_months"] == 0
    assert res["internship_months"] == 3


def test_duration_only_fragments_never_become_title_hints():
    for duration in ("3 yrs 5 mos", "(4 años 7 meses)", "1 an 2 mois", "2 Jahre 1 Monat", "7 mos"):
        assert cv_experience._clean_title_hint(duration) == ""
    assert cv_experience._clean_title_hint("Data Engineer · 3 yrs 5 mos") == "Data Engineer"


def test_dual_headings_still_match_but_role_titles_do_not():
    assert cv_experience.match_section_heading("Experience & Projects") == "experience"
    assert cv_experience.match_section_heading("Skills / Tools") == "skills"
    assert cv_experience.match_section_heading("Research and Development (R&D) Process Engineer") is None
    assert cv_experience.match_section_heading("Research & Development") is None
    assert cv_experience.match_section_heading("Research and Development Engineer") is None
    assert cv_experience.match_section_heading("Projects / Data Engineer") is None


def test_wrapped_bullet_tail_is_not_prefixed_to_next_role_title():
    cv_text = """
Work Experience
Lecturer March 2024 - May 2024
- Co-designed a new course, creating syllabus, labs, and evaluation
rubrics.
PhD researcher in AI & Applied ML October 2019 - December 2023
- Built graph neural networks.
"""
    res = cv_experience.analyze_experience(cv_text, as_of=date(2026, 1, 1))
    assert [r["title_hint"] for r in res["roles"]] == ["Lecturer", "PhD researcher in AI & Applied ML"]
