"""Compose the extraction strategy for a recognized ATS vendor."""

from __future__ import annotations

from .ashby import parse as parse_ashby
from .parser_contract import ParserFn
from .selectors import make_selector_parser


def parser_for_vendor(vendor: str) -> ParserFn:
    """Use Ashby's structured payload or the vendor's declarative selectors."""
    if vendor == "ashby":
        return parse_ashby
    return make_selector_parser(vendor)
