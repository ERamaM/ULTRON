"""Web-search query plan built from a profile's query templates."""

from __future__ import annotations


def build_query_plan(profile: dict, max_queries: int) -> list[str]:
    if max_queries <= 0:
        return []
    templates = profile.get("query_templates") or {}
    titles = list(templates.get("core_titles") or (profile.get("criteria") or {}).get("target_titles") or [])
    supplementary = templates.get("titles")
    if isinstance(supplementary, list):
        titles.extend(title.strip() for title in supplementary if isinstance(title, str) and title.strip())
    regions = list(templates.get("regions") or [])
    combos = [f"{t} {r}" for t in titles for r in regions] if regions else [str(t) for t in titles]
    broad = list(dict.fromkeys(c.strip() for c in combos if str(c).strip()))
    scopes = list(dict.fromkeys(
        str(s).strip().removeprefix("site:").strip()
        for s in (templates.get("site_scopes") or [])
        if str(s).strip().removeprefix("site:").strip()
    ))
    if not scopes or not broad:
        return broad[:max_queries]
    scoped: list[str] = []
    for round_idx in range(len(broad)):
        for scope_idx, scope in enumerate(scopes):
            scoped.append(f"site:{scope} {broad[(scope_idx + round_idx) % len(broad)]}")
            if len(scoped) >= max_queries:
                break
        if len(scoped) >= max_queries:
            break
    # Interleave broad and site:-scoped ATS queries so even small presets include both.
    plan: list[str] = []
    seen: set[str] = set()
    for i in range(max(len(broad), len(scoped))):
        for pool in (broad, scoped):
            if i < len(pool) and pool[i] not in seen:
                seen.add(pool[i])
                plan.append(pool[i])
                if len(plan) >= max_queries:
                    return plan
    return plan
