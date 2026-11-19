import pytest


@pytest.fixture(autouse=True)
def _isolated_ultron_home(tmp_path, monkeypatch):
    """Never let a test touch the real ~/.ultron."""
    monkeypatch.setenv("ULTRON_HOME", str(tmp_path / "ultron_home"))


@pytest.fixture(autouse=True)
def _reset_http_overrides():
    from ultron import http_factory

    http_factory.reset_test_overrides()
    yield
    http_factory.reset_test_overrides()
