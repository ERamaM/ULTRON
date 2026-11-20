"""Tests for deterministic CV section splitting and experience timeline analysis."""

from __future__ import annotations

from datetime import date

from ultron import cv_experience


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
