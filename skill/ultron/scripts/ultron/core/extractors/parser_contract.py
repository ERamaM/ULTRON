"""Callable contract shared by ATS extraction strategies."""

from typing import Protocol

from ultron.core.models import ExtractedJob, JobRawPage


class ParserFn(Protocol):
    """Protocol implemented by vendor-specific job page extractors."""
    def __call__(self, page: JobRawPage, ats_hints: dict[str, str] | None = None) -> ExtractedJob:
        """Extract a job posting from a fetched page, using optional ATS hints."""
