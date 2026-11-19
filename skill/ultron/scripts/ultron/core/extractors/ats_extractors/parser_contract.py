"""Callable contract shared by ATS extraction strategies."""

from typing import Protocol

from ultron.core.models import ExtractedJob, JobRawPage


class ParserFn(Protocol):
    def __call__(self, page: JobRawPage, ats_hints: dict[str, str] | None = None) -> ExtractedJob: ...
