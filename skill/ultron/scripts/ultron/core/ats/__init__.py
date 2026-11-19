"""ATS vendor package.

The registry (which imports every vendor client + extractor) is loaded lazily via
a module ``__getattr__`` (PEP 562). This lets leaf modules ``import
ultron.core.ats.vendors`` without dragging in the whole client/extractor graph —
which previously created an import cycle (vendors -> __init__ -> registry ->
clients -> vendors, mid-init). ``vendors`` itself is a leaf and stays eager.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ultron.core.ats.vendors import (
    SUPPORTED_ATS_DOMAINS,
    SUPPORTED_ATS_NAMES,
    canonical_domain_for_vendor,
)

# Names served lazily from ultron.core.ats.registry on first access.
_REGISTRY_EXPORTS = frozenset(
    {
        "AtsVendorSpec",
        "extract_seed_from_url",
        "get_vendor_spec",
        "infer_vendor_from_host",
        "is_direct_supported_ats_host",
        "iter_vendor_specs",
        "match_vendor_spec_by_domain",
        "match_vendor_spec_by_fingerprint",
        "parse_ats_page",
        "supported_ats_probe_vendors",
    }
)


def __getattr__(name: str):  # PEP 562
    if name in _REGISTRY_EXPORTS:
        from ultron.core.ats import registry

        return getattr(registry, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(__all__)


if TYPE_CHECKING:  # for type checkers / IDEs, without importing at runtime
    from ultron.core.ats.registry import (
        AtsVendorSpec,
        extract_seed_from_url,
        get_vendor_spec,
        infer_vendor_from_host,
        is_direct_supported_ats_host,
        iter_vendor_specs,
        match_vendor_spec_by_domain,
        match_vendor_spec_by_fingerprint,
        parse_ats_page,
        supported_ats_probe_vendors,
    )

__all__ = [
    "SUPPORTED_ATS_DOMAINS",
    "SUPPORTED_ATS_NAMES",
    "AtsVendorSpec",
    "canonical_domain_for_vendor",
    "extract_seed_from_url",
    "get_vendor_spec",
    "infer_vendor_from_host",
    "is_direct_supported_ats_host",
    "iter_vendor_specs",
    "match_vendor_spec_by_domain",
    "match_vendor_spec_by_fingerprint",
    "parse_ats_page",
    "supported_ats_probe_vendors",
]
