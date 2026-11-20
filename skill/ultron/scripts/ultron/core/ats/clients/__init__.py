from __future__ import annotations

from collections.abc import Callable

from ultron.core.ats.board_policy import VendorScopedClient, board_is_valid
from ultron.core.ats.clients.ashby import fetch_ashby_jobs
from ultron.core.ats.clients.bamboohr import fetch_bamboohr_jobs
from ultron.core.ats.clients.base import AtsClientError, AtsHTTPClient, AtsPosting
from ultron.core.ats.clients.breezy import fetch_breezy_jobs
from ultron.core.ats.clients.comeet import fetch_comeet_jobs
from ultron.core.ats.clients.factorial import fetch_factorial_jobs
from ultron.core.ats.clients.greenhouse import fetch_greenhouse_jobs
from ultron.core.ats.clients.hirehive import fetch_hirehive_jobs
from ultron.core.ats.clients.homerun import fetch_homerun_jobs
from ultron.core.ats.clients.icims import fetch_icims_jobs
from ultron.core.ats.clients.jazzhr import fetch_jazzhr_jobs
from ultron.core.ats.clients.jobvite import fetch_jobvite_jobs
from ultron.core.ats.clients.lever import fetch_lever_jobs
from ultron.core.ats.clients.personio import fetch_personio_jobs
from ultron.core.ats.clients.pinpoint import fetch_pinpoint_jobs
from ultron.core.ats.clients.recruitee import fetch_recruitee_jobs
from ultron.core.ats.clients.smartrecruiters import fetch_smartrecruiters_jobs
from ultron.core.ats.clients.taleo import fetch_taleo_jobs
from ultron.core.ats.clients.teamtailor import fetch_teamtailor_jobs
from ultron.core.ats.clients.workable import fetch_workable_jobs
from ultron.core.ats.clients.workday import fetch_workday_jobs

FETCHERS: dict[str, Callable] = {
    "ashby": fetch_ashby_jobs,
    "bamboohr": fetch_bamboohr_jobs,
    "breezy": fetch_breezy_jobs,
    "comeet": fetch_comeet_jobs,
    "factorial": fetch_factorial_jobs,
    "greenhouse": fetch_greenhouse_jobs,
    "hirehive": fetch_hirehive_jobs,
    "homerun": fetch_homerun_jobs,
    "icims": fetch_icims_jobs,
    "jazzhr": fetch_jazzhr_jobs,
    "jobvite": fetch_jobvite_jobs,
    "lever": fetch_lever_jobs,
    "personio": fetch_personio_jobs,
    "pinpoint": fetch_pinpoint_jobs,
    "recruitee": fetch_recruitee_jobs,
    "smartrecruiters": fetch_smartrecruiters_jobs,
    "taleo": fetch_taleo_jobs,
    "teamtailor": fetch_teamtailor_jobs,
    "workable": fetch_workable_jobs,
    "workday": fetch_workday_jobs,
}


def fetch_vendor_jobs(
    vendor: str, board: str, *, client: AtsHTTPClient, timeout: int, verify_only: bool = False,
    preferred_locations: tuple[str, ...] = (), max_pages: int | None = None,
) -> list[AtsPosting]:
    """Spec §5.7: validate ``board`` for ``vendor`` and keep every call on the vendor's hosts.

    ``max_pages`` bounds Workday pagination (discover passes the board's share of the vendor budget)."""
    fetcher = FETCHERS.get(vendor)
    if fetcher is None:
        raise AtsClientError("vendor_not_supported_yet")
    if not board_is_valid(vendor, board):
        raise AtsClientError("invalid_board")
    scoped_client = VendorScopedClient(client, vendor)
    if vendor == "workday":
        return fetcher(
            board, timeout, client=scoped_client,
            max_pages=2 if verify_only else max_pages,
            preferred_locations=() if verify_only else preferred_locations,
        )
    return fetcher(board, timeout, client=scoped_client)


__all__ = [
    "FETCHERS",
    "AtsClientError",
    "AtsHTTPClient",
    "AtsPosting",
    "fetch_ashby_jobs",
    "fetch_bamboohr_jobs",
    "fetch_breezy_jobs",
    "fetch_comeet_jobs",
    "fetch_factorial_jobs",
    "fetch_greenhouse_jobs",
    "fetch_hirehive_jobs",
    "fetch_homerun_jobs",
    "fetch_icims_jobs",
    "fetch_jazzhr_jobs",
    "fetch_jobvite_jobs",
    "fetch_lever_jobs",
    "fetch_personio_jobs",
    "fetch_pinpoint_jobs",
    "fetch_recruitee_jobs",
    "fetch_smartrecruiters_jobs",
    "fetch_taleo_jobs",
    "fetch_teamtailor_jobs",
    "fetch_vendor_jobs",
    "fetch_workable_jobs",
    "fetch_workday_jobs",
]
