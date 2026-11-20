"""Run intensity presets. ``HttpClient`` max_requests = max_pages + max_seeds_per_run * 2."""

from __future__ import annotations

DEFAULT_PRESET = "balanced"

# ``seed_target``: fitting seeds a run should have before discover (``ultron seeds plan`` asks the agent for the
# rest, in up to ``seed_rounds`` rounds). It is about what a profile covered by the bundled technology pack queries
# per run under the same seed and per-host caps (40 fast, 83 balanced, 113 deep, measured in session 3), so a
# profile without a matching pack aims at about the same coverage. It stays about 10% below those counts so a
# technology-pack profile needs no round even after a few dead boards are pruned or capped (the smaller regional
# packs do not reach it, so their profiles run rounds too); seed_boost aims higher to grow the inventory.

PRESETS: dict[str, dict] = {
    "fast": {
        "min_roles": 3, "max_roles": 5, "max_pages": 70, "ats_api_timeout": 10,
        "ats_api_max_jobs_per_seed": 18, "max_requests_per_host": 8, "max_seeds_per_run": 120,
        "max_search_queries": 20, "llm_ambiguity_threshold": 55, "new_companies_per_run": 10,
        "seed_target": 35, "seed_rounds": 2,
    },
    "balanced": {
        "min_roles": 15, "max_roles": 250, "max_pages": 400, "ats_api_timeout": 12,
        "ats_api_max_jobs_per_seed": 35, "max_requests_per_host": 30, "max_seeds_per_run": 180,
        "max_search_queries": 40, "llm_ambiguity_threshold": 50, "new_companies_per_run": 25,
        "seed_target": 75, "seed_rounds": 3,
    },
    "deep": {
        "min_roles": 15, "max_roles": 250, "max_pages": 4000, "ats_api_timeout": 12,
        "ats_api_max_jobs_per_seed": 55, "max_requests_per_host": 60, "max_seeds_per_run": 400,
        "max_search_queries": 120, "llm_ambiguity_threshold": 45, "new_companies_per_run": 60,
        "seed_target": 100, "seed_rounds": 3,
    },
    "seed_boost": {
        "min_roles": 15, "max_roles": 250, "max_pages": 4000, "ats_api_timeout": 16,
        "ats_api_max_jobs_per_seed": 70, "max_requests_per_host": 40, "max_seeds_per_run": 400,
        "max_search_queries": 220, "llm_ambiguity_threshold": 45, "new_companies_per_run": 120,
        "seed_target": 130, "seed_rounds": 4,
    },
}


def resolve(name: str) -> dict:
    """Return a copy of the named preset; ``KeyError`` if unknown."""
    if name not in PRESETS:
        raise KeyError(f"unknown preset: {name!r} (choose from {', '.join(PRESETS)})")
    return dict(PRESETS[name])


# Inclusive upper bounds for overrides that are percentages or scores.
_MAX_VALUES = {"llm_ambiguity_threshold": 100}


class PresetOverrideError(ValueError):
    """An invalid preset override (a usage error); ``key`` names the offending setting."""

    def __init__(self, message: str, *, key: str = "") -> None:
        super().__init__(message)
        self.key = key


def validate_overrides(overrides: dict, reference: dict | None = None) -> dict:
    """Return a copy of ``overrides`` after checking keys, types and bounds against ``reference`` (default balanced).

    ``reference`` is the preset the overrides apply to; ``min_roles`` is checked against its merged ``max_roles``.
    """
    reference = PRESETS[DEFAULT_PRESET] if reference is None else reference
    unknown = sorted(set(overrides) - set(reference))
    if unknown:
        raise PresetOverrideError(f"unknown override keys: {', '.join(unknown)}", key=unknown[0])
    out = {}
    for key, value in overrides.items():
        expected = reference[key]
        if isinstance(value, bool):
            raise PresetOverrideError(f"override {key!r} must be a number, got {value!r}", key=key)
        if isinstance(expected, float):
            if not isinstance(value, int | float):
                raise PresetOverrideError(f"override {key!r} must be a number, got {value!r}", key=key)
        elif not isinstance(value, int):
            raise PresetOverrideError(f"override {key!r} must be an integer, got {value!r}", key=key)
        if value <= 0:
            raise PresetOverrideError(f"override {key!r} must be positive, got {value!r}", key=key)
        if key in _MAX_VALUES and value > _MAX_VALUES[key]:
            raise PresetOverrideError(f"override {key!r} must be at most {_MAX_VALUES[key]}, got {value!r}", key=key)
        out[key] = value
    # min_roles is not read by any stage, so only an explicit min_roles is checked: lowering max_roles alone stays valid.
    if "min_roles" in out and "max_roles" in reference:
        max_roles = out.get("max_roles", reference["max_roles"])
        if out["min_roles"] > max_roles:
            raise PresetOverrideError(
                f"override 'min_roles' ({out['min_roles']}) must not exceed max_roles ({max_roles})", key="min_roles"
            )
    return out
