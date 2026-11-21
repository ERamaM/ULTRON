"""Company seeds: the inventory, the auto-seed file, company names and on-the-fly verification.

The package re-exports the public names of its modules, so callers use ``seeds.load_inventory`` and the like.
Patch a function where it is defined (for example ``seeds.inventory.packs_dir``), not on this package.
"""

from __future__ import annotations

from ultron.seeds.inventory import (
    OTHER_COMPANY_SOURCE_TYPE,
    UNCONFIRMED_SEED_PRIORITY,
    UNCONFIRMED_SOURCE_TYPE,
    auto_seeds_path,
    is_unconfirmed,
    load_inventory,
    make_seed_id,
    packs_dir,
    read_yaml_seeds,
)
from ultron.seeds.naming import (
    BLOCKED_GENERIC_SLUGS,
    COUNTRY_SLDS,
    LEGAL_COMPANY_SUFFIXES,
    THIRD_PARTY_DOMAINS,
    ascii_fold,
    company_key,
    domain_slug,
    registrable_domain,
    slug_candidates,
)
from ultron.seeds.store import (
    SEED_LOCK_TIMEOUT,
    SeedFileError,
    add_seeds,
)
from ultron.seeds.verify import (
    FEED_PROPOSALS_FILE,
    PAGE_HOP_MAX_BOARDS,
    PAGE_HOP_PER_HOST,
    PAGE_HOP_TOTAL,
    PROBE_VENDORS,
    SEARCH_PROPOSALS_FILE,
    PageHops,
    Proposal,
    VerifyResult,
    as_feed_company,
    guessed_board_ownership,
    stored_seed,
    unconfirmed_seed,
    verify_proposal,
)

__all__ = [
    "BLOCKED_GENERIC_SLUGS",
    "COUNTRY_SLDS",
    "FEED_PROPOSALS_FILE",
    "LEGAL_COMPANY_SUFFIXES",
    "OTHER_COMPANY_SOURCE_TYPE",
    "PAGE_HOP_MAX_BOARDS",
    "PAGE_HOP_PER_HOST",
    "PAGE_HOP_TOTAL",
    "PROBE_VENDORS",
    "SEARCH_PROPOSALS_FILE",
    "SEED_LOCK_TIMEOUT",
    "THIRD_PARTY_DOMAINS",
    "UNCONFIRMED_SEED_PRIORITY",
    "UNCONFIRMED_SOURCE_TYPE",
    "PageHops",
    "Proposal",
    "SeedFileError",
    "VerifyResult",
    "add_seeds",
    "as_feed_company",
    "ascii_fold",
    "auto_seeds_path",
    "company_key",
    "domain_slug",
    "guessed_board_ownership",
    "is_unconfirmed",
    "load_inventory",
    "make_seed_id",
    "packs_dir",
    "read_yaml_seeds",
    "registrable_domain",
    "slug_candidates",
    "stored_seed",
    "unconfirmed_seed",
    "verify_proposal",
]
