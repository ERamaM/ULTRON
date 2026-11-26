"""Display formatting for disclosed vacancy compensation."""

from __future__ import annotations

from ultron.salary.disclosure import extract_salaries_from_text

SALARY_SCAN_CHARS = 20_000

_PERIOD_LABEL = {"annual": "year", "monthly": "month", "daily": "day", "hourly": "hour"}


def _amount(value: float) -> str:
    return f"{value:,.2f}".rstrip("0").rstrip(".")


def format_salary_disclosure(body_text: str) -> str:
    """Format the first usable disclosure in its stated period."""
    hits = extract_salaries_from_text((body_text or "")[:SALARY_SCAN_CHARS])
    if not hits:
        return ""
    hit = hits[0]
    low, high = hit.get("low"), hit.get("high")
    if low is None and high is None:
        return ""
    bound = low if low is not None else high
    amounts = _amount(bound)
    if low is not None and high is not None and low != high:
        amounts += "\u2013" + _amount(high)
    return f"{hit['currency']} {amounts} / {_PERIOD_LABEL[hit['period']]}"
