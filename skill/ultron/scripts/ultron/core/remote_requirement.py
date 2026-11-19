"""Single source of truth for a profile's ``criteria.remote_policy.required`` value."""

from __future__ import annotations

from typing import Any

# A profile that omits ``criteria.remote_policy.required`` requires fully remote roles.
DEFAULT_REMOTE_REQUIRED = "full_remote"
_REMOTE_NOT_REQUIRED_VALUES = frozenset({"any", "none", "optional", "hybrid", "onsite", "on-site", "false"})


def remote_required_value(criteria: dict[str, Any] | None) -> str:
    """Return the normalized ``remote_policy.required`` value, or ``DEFAULT_REMOTE_REQUIRED`` when missing."""
    policy = (criteria or {}).get("remote_policy")
    raw = policy.get("required") if isinstance(policy, dict) else None
    return str(raw or DEFAULT_REMOTE_REQUIRED).strip().lower()


def is_remote_required(criteria: dict[str, Any] | None) -> bool:
    """True unless the profile explicitly accepts non-remote roles."""
    return remote_required_value(criteria) not in _REMOTE_NOT_REQUIRED_VALUES
