"""Run intensity presets. ``HttpClient`` max_requests = max_pages + max_seeds_per_run * 2."""

from __future__ import annotations

DEFAULT_PRESET = "balanced"

PRESETS: dict[str, dict] = {
    "fast": {
        "min_roles": 3, "max_roles": 5, "max_pages": 70, "ats_api_timeout": 10,
        "ats_api_max_jobs_per_seed": 18, "max_requests_per_host": 8, "max_seeds_per_run": 120,
        "max_search_queries": 20, "llm_ambiguity_threshold": 55, "new_companies_per_run": 10,
    },
    "balanced": {
        "min_roles": 15, "max_roles": 250, "max_pages": 400, "ats_api_timeout": 12,
        "ats_api_max_jobs_per_seed": 35, "max_requests_per_host": 30, "max_seeds_per_run": 180,
        "max_search_queries": 40, "llm_ambiguity_threshold": 50, "new_companies_per_run": 25,
    },
    "deep": {
        "min_roles": 15, "max_roles": 250, "max_pages": 4000, "ats_api_timeout": 12,
        "ats_api_max_jobs_per_seed": 55, "max_requests_per_host": 60, "max_seeds_per_run": 400,
        "max_search_queries": 120, "llm_ambiguity_threshold": 45, "new_companies_per_run": 60,
    },
    "seed_boost": {
        "min_roles": 15, "max_roles": 250, "max_pages": 4000, "ats_api_timeout": 16,
        "ats_api_max_jobs_per_seed": 70, "max_requests_per_host": 40, "max_seeds_per_run": 400,
        "max_search_queries": 220, "llm_ambiguity_threshold": 45, "new_companies_per_run": 120,
    },
}


def resolve(name: str) -> dict:
    """Return a copy of the named preset; ``KeyError`` if unknown."""
    if name not in PRESETS:
        raise KeyError(f"unknown preset: {name!r} (choose from {', '.join(PRESETS)})")
    return dict(PRESETS[name])


def validate_overrides(overrides: dict, reference: dict | None = None) -> dict:
    """Return a copy of ``overrides`` after checking keys and value types against ``reference`` (default balanced)."""
    reference = PRESETS[DEFAULT_PRESET] if reference is None else reference
    unknown = sorted(set(overrides) - set(reference))
    if unknown:
        raise ValueError(f"unknown override keys: {', '.join(unknown)}")
    out = {}
    for key, value in overrides.items():
        expected = reference[key]
        if isinstance(value, bool):
            raise ValueError(f"override {key!r} must be a number, got {value!r}")  # noqa: TRY004
        if isinstance(expected, float):
            if not isinstance(value, int | float):
                raise ValueError(f"override {key!r} must be a number, got {value!r}")  # noqa: TRY004
        elif not isinstance(value, int):
            raise ValueError(f"override {key!r} must be an integer, got {value!r}")  # noqa: TRY004
        if value <= 0:
            raise ValueError(f"override {key!r} must be positive, got {value!r}")
        out[key] = value
    return out
