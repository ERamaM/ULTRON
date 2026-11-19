from __future__ import annotations

import pytest

from ultron.core.models import FinalRow
from ultron.core.privacy import REDACTED, environment_secret_values, redact
from ultron.core.reporting.export import _escape_cell, render_results_markdown


def test_escape_cell_strips_cr_control_and_zero_width_chars():
    raw = "Senior\r\nEngineer\r\x00\x07\x7f\u200b\u200d\u202e\ufeff"
    assert _escape_cell(raw) == "Senior Engineer"


def test_escape_cell_neutralises_markdown_links_images_and_html():
    assert _escape_cell("[click](https://evil.example)") == r"\[click\](https://evil.example)"
    assert _escape_cell("![img](https://evil.example/x.png)") == r"!\[img\](https://evil.example/x.png)"
    assert _escape_cell(r"\[bypass](https://evil.example)") == r"\\\[bypass\](https://evil.example)"
    assert _escape_cell('<a href="https://evil.example">x</a>') == '&lt;a href="https://evil.example"&gt;x&lt;/a&gt;'
    assert _escape_cell("<https://evil.example>") == "&lt;https://evil.example&gt;"
    assert _escape_cell("a | b") == r"a \| b"


def test_render_results_markdown_prevents_injected_links_and_keeps_plain_url():
    row = FinalRow(
        title='[Apply here](https://evil.example) <a href="https://evil.example">x</a>\u200b',
        company="España & Co",
        apply_url="https://boards.greenhouse.io/acme/jobs/123",
        location="Remote",
        remote_policy="remote_eu",
        eligibility="high",
        eligibility_evidence="Remote EU",
        fit_signals=["Python"],
        freshness="recent",
        match_score=90,
        match_band="strong",
    )
    md = render_results_markdown([row])
    assert r"\[Apply here\](https://evil.example)" in md
    assert '&lt;a href="https://evil.example"&gt;x&lt;/a&gt;' in md
    assert "\u200b" not in md
    assert "https://boards.greenhouse.io/acme/jobs/123" in md
    assert "España & Co" in md


def test_privacy_redacts_anthropic_and_google_gemini_env_keys(monkeypatch):
    for name, val in (
        ("ANTHROPIC_API_KEY", "secret-anthropic-value-999"),
        ("GEMINI_API_KEY", "secret-gemini-value-999"),
        ("GOOGLE_API_KEY", "secret-google-value-999"),
    ):
        monkeypatch.setenv(name, val)
    env_vals = environment_secret_values()
    assert "secret-anthropic-value-999" in env_vals
    assert "secret-gemini-value-999" in env_vals
    assert "secret-google-value-999" in env_vals
    assert redact("used secret-anthropic-value-999") == f"used {REDACTED}"
    assert redact("used secret-gemini-value-999") == f"used {REDACTED}"
    assert redact("used secret-google-value-999") == f"used {REDACTED}"


def test_privacy_redacts_sk_ant_and_aiza_token_patterns():
    assert redact("key=sk-ant-api03-abcdef123456") == f"key={REDACTED}"
    assert redact("key=AIzaSyDummyKey1234567890") == f"key={REDACTED}"


def test_privacy_does_not_mangle_normal_text():
    for phrase in (
        "Risk-Management Analyst",
        "Basic knowledge of SQL",
        "ask-anything",
        "Bearer of good news",
        "Basic English/Spanish/French required",
        "Basic Python/JavaScript/TypeScript",
        "Basic Kubernetes-administration skills",
        "sk-learn-compatible-pipelines",
        "Basic Requirements and Qualifications",
        "Bearer responsibilities",
    ):
        assert redact(phrase) == phrase
        assert _escape_cell(phrase) == phrase


def test_privacy_redacts_real_looking_keys_and_auth_headers():
    assert redact("key=sk-ant-api03-abcdef1234567890") == f"key={REDACTED}"
    assert redact("key=sk-proj-abc123def456ghi789jkl012") == f"key={REDACTED}"
    assert redact(f"key=sk-{'a1B2c3D4' * 4}") == f"key={REDACTED}"
    assert redact("OPENAI_sk-abc123def456ghi789jkl012") == f"OPENAI_{REDACTED}"
    assert redact("token=ghp_1234567890abcdef") == f"token={REDACTED}"
    assert redact("key=AIzaSyDummyKey1234567890") == f"key={REDACTED}"
    assert redact("Authorization: Basic dXNlcjpwYXNzd29yZA==") == f"Authorization: {REDACTED}"
    assert redact("Basic dXNlcjpwYXNz") == REDACTED
    assert redact("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.sig") == f"Authorization: {REDACTED}"
    assert redact("Bearer abc123def456ghi789") == REDACTED


@pytest.mark.parametrize(
    "text",
    [
        "Basic " + "a" * 50000,
        "Basic " + "aB" * 25000,
        "Basic " + "a" * 50000 + "-x",
        "Bearer " + "a" * 50000,
        "sk-" + "a" * 50000,
        "sk-" * 20000,
        "Basic " * 10000,
    ],
)
def test_redact_is_linear_on_hostile_long_text(text):
    import time

    start = time.perf_counter()
    redact(text)
    assert time.perf_counter() - start < 0.5
