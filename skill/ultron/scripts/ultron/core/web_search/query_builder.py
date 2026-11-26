"""Build bounded, profile-driven search queries and relaxed variants."""

from itertools import product

from ultron.core.extractors.text import collapse_whitespace
from ultron.core.rules.geography import location_scope

ATS_HOSTS = (
    "boards.greenhouse.io", "jobs.ashbyhq.com", "jobs.lever.co", "apply.workable.com",
    "jobs.smartrecruiters.com", "recruitee.com", "teamtailor.com", "jobs.personio.de",
    "job-boards.greenhouse.io",
)
_ALIASES = {
    "boards.greenhouse.io": "job-boards.greenhouse.io",
    "job-boards.greenhouse.io": "boards.greenhouse.io",
    "jobs.lever.co": "lever.co",
    "lever.co": "jobs.lever.co",
}


def _terms(*values: object) -> tuple[str, ...]:
    flattened = []
    for value in values:
        items = value[:40] if isinstance(value, list | tuple) else [value]
        flattened.extend(collapse_whitespace(item[:120]).replace('"', "") for item in items if isinstance(item, str))
    return tuple(dict.fromkeys(item for item in flattened if item))


def _interleave(broad: list[str], scoped: list[str]) -> list[str]:
    pools = (iter(scoped), iter(broad))
    result = []
    while len(result) < len(broad) + len(scoped):
        for index in (0, 1, 0, 1, 0):
            if query := next(pools[index], ""):
                result.append(query)
    return result


def work_mode_term(profile: dict) -> str:
    """Convert the profile work-mode preference into a search term."""
    criteria = profile.get("criteria") or {}
    policy = criteria.get("remote_policy")
    if isinstance(policy, dict) and policy.get("required") == "full_remote":
        return "remote"
    modes = _terms(criteria.get("work_mode"))
    return modes[0] if len(modes) == 1 else ""


def build_queries(profile: dict, max_queries: int, *, query: str | None = None) -> list[str]:
    """Build up to 40 queries from role, region and work mode preferences."""
    limit = min(40, max(0, max_queries))
    templates = profile.get("query_templates") or {}
    criteria = profile.get("criteria") or {}
    titles = _terms(templates.get("core_titles"), criteria.get("target_titles"),
                    criteria.get("role_focus_terms"), templates.get("titles"))
    regions = _terms(location_scope(profile)["preferred"], templates.get("regions")) or ("",)
    mode = work_mode_term(profile)
    phrases = (f'"{title}" {region} {mode}' for region, title in product(regions, titles))
    broad = [collapse_whitespace(phrase) for phrase in phrases] if query is None else [collapse_whitespace(query[:300])]
    broad = list(dict.fromkeys(phrase for phrase in broad if phrase))[:limit]
    if not broad:
        return []
    scopes = _terms(templates.get("site_scopes"), ATS_HOSTS)
    hosts = tuple(dict.fromkeys(scope.removeprefix("site:") for scope in scopes if scope.removeprefix("site:")))
    scoped = [f"site:{host} {broad[(index + rotation) % len(broad)]}"
              for rotation in range(len(broad)) for index, host in enumerate(hosts)]
    plan = _interleave(broad, scoped[:limit])
    if query is not None:
        plan = [*broad, *(item for item in plan if item not in broad)]
    return plan[:limit]


def relaxed_queries(query: str) -> tuple[str, ...]:
    """Return unquoted and alternate ATS host variants for a zero-hit query."""
    unquoted = query.replace('"', "")
    variants = [unquoted]
    scope, separator, rest = unquoted.partition(" ")
    if separator and (alias := _ALIASES.get(scope.removeprefix("site:"))) and scope.startswith("site:"):
        variants.append(f"site:{alias} {rest}")
    return tuple(dict.fromkeys(variant for variant in variants if variant != query))
