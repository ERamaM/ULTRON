"""Compose the extraction strategy for a recognized ATS vendor."""

from __future__ import annotations

from .ashby import parse as parse_ashby
from .candidatemanager import parse as parse_candidatemanager
from .hirehive import parse as parse_hirehive
from .parser_contract import ParserFn
from .selectors import make_selector_parser
from .smartrecruiters import parse as parse_smartrecruiters


def parser_for_vendor(vendor: str) -> ParserFn:
    """Use structured parsing for vendors with dedicated extractors."""
    if vendor == "ashby":
        return parse_ashby
    if vendor == "smartrecruiters":
        return parse_smartrecruiters
    if vendor == "hirehive":
        return parse_hirehive
    if vendor == "candidatemanager":
        return parse_candidatemanager
    return make_selector_parser(vendor)
