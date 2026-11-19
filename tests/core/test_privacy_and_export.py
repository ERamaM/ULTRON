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


def test_redact_keeps_greenhouse_job_token_but_redacts_real_secrets():
    link = "https://boards.greenhouse.io/embed/job_app?for=acme&token=4567"
    assert redact(link) == link
    assert redact({"apply_url": link}) == {"apply_url": link}
    assert redact(f"Apply at {link} today") == f"Apply at {link} today"
    other = "https://job-boards.greenhouse.io/embed/job_app?token=4567&for=acme"
    assert redact(other) == other

    assert redact(f"{link}&access_token=abc123") == f"{link}&access_token={REDACTED}"
    assert redact("https://boards.greenhouse.io/x?api_key=987654") == f"https://boards.greenhouse.io/x?api_key={REDACTED}"
    # Non-numeric tokens, and numeric tokens on other hosts, stay redacted.
    assert redact("https://boards.greenhouse.io/x?token=abcDEF123") == f"https://boards.greenhouse.io/x?token={REDACTED}"
    assert redact("https://api.example.com/x?token=4567") == f"https://api.example.com/x?token={REDACTED}"
    assert redact("https://greenhouse.io.evil.example/x?token=4567") == (
        f"https://greenhouse.io.evil.example/x?token={REDACTED}"
    )
    assert redact("token=4567 near https://boards.greenhouse.io/x?token=12") == (
        "token=4567 near https://boards.greenhouse.io/x?token=12"
    )



def test_redact_keeps_greenhouse_job_token_before_closing_delimiters():
    link = "https://boards.greenhouse.io/x?token=4567"
    for text in (
        f"Apply ({link})",
        f"[Apply]({link})",
        f"<{link}>",
        f'<a href="{link}">Apply</a>',
        f"Apply at {link}. Thanks",
        f"Apply at {link}, today",
    ):
        assert redact(text) == text
    assert redact("https://boards.greenhouse.io/x?token=4567.secret") == f"https://boards.greenhouse.io/x?token={REDACTED}"
    assert redact("https://boards.greenhouse.io/x?token=4567abc") == f"https://boards.greenhouse.io/x?token={REDACTED}"
    assert redact("https://boards.greenhouse.io/x?token=4567)abcDEF123") == (
        f"https://boards.greenhouse.io/x?token={REDACTED}"
    )
    assert redact(f"See ({link}).") == f"See ({link})."
    assert redact(f"[Apply]({link}), then wait") == f"[Apply]({link}), then wait"


def test_redact_checks_the_host_of_the_containing_url():
    assert redact("https://api.example.com/(https://boards.greenhouse.io/x?token=4567") == (
        f"https://api.example.com/(https://boards.greenhouse.io/x?token={REDACTED}"
    )
    assert redact("https://api.example.com/x[https://boards.greenhouse.io/x?token=4567") == (
        f"https://api.example.com/x[https://boards.greenhouse.io/x?token={REDACTED}"
    )
    tail = "https://greenhouse.io/".ljust(2048, "x")
    crafted = "https://api.example.com/" + tail + "?token=4567"
    assert redact(crafted) == "https://api.example.com/" + tail + f"?token={REDACTED}"
    # A long text is fine as long as the URL itself starts inside the scanned window.
    link = "https://boards.greenhouse.io/x?token=4567"
    assert redact("x" * 5000 + " " + link) == "x" * 5000 + " " + link


def test_redact_keeps_greenhouse_job_token_but_redacts_following_urls():
    link = "https://boards.greenhouse.io/x?token=4567"
    assert redact(f"{link} https://api.example.com/x?password=abcDEF123") == (
        f"{link} https://api.example.com/x?password={REDACTED}"
    )
    # A redacted value still consumes the rest of the text up to ``&``/``#`` (unchanged behavior).
    assert redact(f"[a]({link}) and [b](https://api.example.com/x?token=4567)") == (
        f"[a]({link}) and [b](https://api.example.com/x?token={REDACTED}"
    )
    assert redact(f"{link} then {link}") == f"{link} then {link}"

def test_render_results_markdown_keeps_greenhouse_token_apply_link():
    link = "https://boards.greenhouse.io/embed/job_app?for=acme&token=4567"
    row = FinalRow(
        title="Engineer",
        company="Acme",
        apply_url=link,
        location="Remote",
        remote_policy="remote_eu",
        eligibility="high",
        eligibility_evidence="Remote EU",
        fit_signals=["Python"],
        freshness="recent",
        match_score=90,
        match_band="strong",
    )
    assert link in render_results_markdown([row])


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
