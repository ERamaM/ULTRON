from ultron.core.models import ExtractedJob, JobRawPage


def parse_ats_page(page: JobRawPage, ats_hints: dict[str, str] | None = None) -> ExtractedJob:
    """Delegate ATS page parsing to ``ultron.core.ats.registry.parse_ats_page``."""
    from ultron.core.ats.registry import parse_ats_page as _parse_ats_page

    return _parse_ats_page(page, ats_hints=ats_hints)


__all__ = ["parse_ats_page"]
