from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, PrivateAttr, field_validator

from ultron.core.sectors import Sector

# ``remote_other``: full remote outside Europe, only produced for non-European profile scopes.
RemotePolicy = Literal["remote_spain", "remote_eu", "remote_europe", "remote_emea", "remote_other", "unknown"]
# Profile geographic scope: European rules apply only to ``"eu"`` (see ``rules/geography.py``).
GeoScope = Literal["eu", "other", "global"]
Eligibility = Literal["confirmed", "high", "medium", "unknown"]
EvaluationVerdict = Literal["keep", "discard", "unsure"]
DiscardCategory = Literal[
    "seniority", "role_family", "remote_policy", "eligibility_scope", "closed_or_invalid", "other"
]
ConfidenceLevel = Literal["low", "medium", "high"]
EvaluationSource = Literal["agent", "llm_plus_guardrail", "heuristic_fallback", "heuristic_fallback_llm_error"]
COMPANY_NOT_FOUND = "company_not_found"
TITLE_NOT_FOUND = "title_not_found"
LOCATION_NOT_SHOWN = "location_not_shown"
SignalSource = Literal[
    "ats_hint",
    "jsonld",
    "meta_og",
    "meta_twitter",
    "selector",
    "h1",
    "title_tag",
    "line_heuristic",
    "domain_fallback",
    "unknown",
]
SignalConfidence = Literal["high", "medium", "low"]


class SearchHit(BaseModel):
    query_id: str
    query: str
    rank: int
    title: str
    snippet: str
    url: str
    ats_title: str = ""
    ats_location: str = ""
    ats_remote_hint: str = ""
    ats_company: str = ""
    source_type: str = "unknown"
    source_key: str = ""
    source_label: str = ""
    query_bucket: str = ""
    title_family: str = ""
    site_family: str = ""
    company_hint: str = ""
    search_pass: str = ""
    matched_vendor: str = ""
    query_yield_bucket: str = ""


class CandidateUrl(BaseModel):
    query_id: str
    query: str
    source_url: str
    normalized_url: str
    domain: str
    ats_title: str = ""
    ats_location: str = ""
    ats_remote_hint: str = ""
    ats_company: str = ""
    source_type: str = "unknown"
    source_key: str = ""
    source_label: str = ""
    query_bucket: str = ""
    title_family: str = ""
    site_family: str = ""
    company_hint: str = ""
    search_pass: str = ""
    hit_title: str = ""
    snippet: str = ""
    matched_vendor: str = ""
    company_seen_in_ats_run: bool = False
    query_yield_bucket: str = ""


class AtsBoardSeed(BaseModel):
    seed_id: str
    company: str
    ats: Literal[
        "greenhouse",
        "lever",
        "ashby",
        "workable",
        "personio",
        "recruitee",
        "teamtailor",
        "smartrecruiters",
        "homerun",
        "jobvite",
        "bamboohr",
        "breezy",
        "jazzhr",
        "factorial",
        "pinpoint",
        "comeet",
        "workday",
        "icims",
        "taleo",
        "hirehive",
        "occupop",
        "candidatemanager",
    ]
    board_identifier: str
    enabled: bool = True
    priority: int = 100
    tags: list[str] = Field(default_factory=list)
    profiles: list[str] = Field(default_factory=list)
    sectors: list[Sector] = Field(default_factory=list)
    country_scope: str = "EU"
    source_type: str = "manual_yaml"
    source_label: str = "Manual YAML"
    source_url: str = ""
    verification: dict[str, Any] = Field(default_factory=dict)

    @field_validator("profiles")
    @classmethod
    def normalize_profiles(cls, values: list[str]) -> list[str]:
        if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
            raise ValueError("profiles must be a list of profile slugs")
        return list(
            dict.fromkeys(
                re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-") or "search-profile" for value in values
            )
        )


class JobRawPage(BaseModel):
    url: str
    final_url: str
    status_code: int
    html: str
    canonical_url: str | None = None
    fetched_with: Literal["http", "playwright"] = "http"


class ExtractedJob(BaseModel):
    _field_provenance: dict[str, str] = PrivateAttr(default_factory=dict)
    apply_url: str
    domain: str
    title: str = ""
    company: str = ""
    location: str = ""
    remote_text: str = ""
    body_text: str = ""
    apply_available: bool = False
    posting_date: str | None = None
    is_open_hint: bool = True
    # True only when JSON-LD ``validThrough`` parses to a past deadline.
    valid_through_expired: bool = False


class ExtractionMeta(BaseModel):
    title_source: SignalSource = "unknown"
    title_confidence: SignalConfidence = "low"
    company_source: SignalSource = "unknown"
    company_confidence: SignalConfidence = "low"
    location_source: SignalSource = "unknown"
    location_confidence: SignalConfidence = "low"
    remote_source: SignalSource = "unknown"
    remote_confidence: SignalConfidence = "low"
    field_conflicts: list[str] = Field(default_factory=list)


class ClassificationSignals(BaseModel):
    role_score: int = 0
    role_signals: list[str] = Field(default_factory=list)
    role_ambiguous: bool = False
    remote_scope_hint: RemotePolicy | None = None
    remote_conflict: bool | None = None
    eligibility_hint: Eligibility | None = None
    seniority_ambiguous: bool = False
    signal_sources: list[str] = Field(default_factory=list)


class JobEvaluation(BaseModel):
    keep: bool
    discard_reason: str | None = None
    verdict: EvaluationVerdict = "unsure"
    discard_category: DiscardCategory | None = None
    confidence: ConfidenceLevel = "medium"
    evaluation_source: EvaluationSource = "heuristic_fallback"
    is_senior: bool
    is_allowed_family: bool
    is_full_remote: bool
    eligibility: Eligibility | None = None
    eligibility_evidence: str = ""
    fit_signals: list[str] = Field(default_factory=list)
    open_status_ok: bool = False
    seniority_evidence: str = ""
    remote_evidence: str = ""
    role_evidence: str = ""
    open_status_evidence: str = ""
    raw_provider_payload: dict[str, Any] = Field(default_factory=dict)


class FinalRow(BaseModel):
    title: str
    company: str
    apply_url: str
    location: str
    remote_policy: RemotePolicy
    eligibility: Eligibility
    eligibility_evidence: str
    fit_signals: list[str]
    freshness: str
    evaluation_verdict: EvaluationVerdict = "keep"
    source_type: str = "unknown"
    source_key: str = ""
    source_label: str = ""
    match_score: int | None = None
    match_band: Literal["strong", "good", "borderline", "ineligible"] | None = None
    score_version: str = ""
    match_dimensions: dict[str, Any] = Field(default_factory=dict)
    matched_requirements: list[str] = Field(default_factory=list)
    missing_requirements: list[str] = Field(default_factory=list)
    uncertain_requirements: list[str] = Field(default_factory=list)


class DiscardRecord(BaseModel):
    apply_url: str
    reason: str
    detail: str = ""
    title: str = ""
    company: str = ""
    location: str = ""
    remote_text: str = ""


class RunSummary(BaseModel):
    run_id: str
    started_at: datetime
    ended_at: datetime
    ats_pages_opened: int
    roles_kept: int
    discard_counts: dict[str, int]
    full_query_log: list[dict[str, Any]]
    seed_source_counts: dict[str, dict[str, Any]] = Field(default_factory=dict)
    detected_seeds: list[dict[str, Any]] = Field(default_factory=list)
    ats_seed_metrics: dict[str, Any] = Field(default_factory=dict)
    search_query_metrics: dict[str, Any] = Field(default_factory=dict)
    combined_discovery_metrics: dict[str, Any] = Field(default_factory=dict)
    llm_diagnostics: dict[str, Any] = Field(default_factory=dict)
    guardrail_saves: int = 0
    unsure_to_keep_count: int = 0
    hard_filter_overrides: int = 0
    search_fallback_triggered: bool = False
    search_fallback_hits: int = 0
    soft_filtered_count: int = 0


class PipelineResult(BaseModel):
    run_id: str
    roles: list[FinalRow]
    summary: RunSummary
    discards: list[DiscardRecord]
    output_markdown_path: str
    output_json_path: str
    output_summary_path: str
