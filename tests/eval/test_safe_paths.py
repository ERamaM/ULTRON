"""Report files are private to the user and redacted."""

from __future__ import annotations


def test_reports_are_redacted_and_private(tmp_path, monkeypatch):
    import os
    import stat

    import safe_paths

    monkeypatch.setattr(safe_paths, "REPORTS_DIR", str(tmp_path / "ultron-eval-test"))
    token = "abcdef" + "123456"
    path = safe_paths.write_report("arm.json", f'{{"identity_key": "url://x?access_token={token}"}}')
    text = path.read_text(encoding="utf-8")
    assert token not in text
    if hasattr(os, "getuid"):
        assert stat.S_IMODE(os.stat(safe_paths.REPORTS_DIR).st_mode) == 0o700
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_redacted_json_report_still_parses_and_keys_are_redacted(tmp_path, monkeypatch):
    import json

    import safe_paths

    monkeypatch.setattr(safe_paths, "REPORTS_DIR", str(tmp_path / "ultron-eval-test"))
    token = "abcdef" + "123456"
    key = f"url://jobs.example.test/1?access_token={token}"
    report = {"identities": {key: {"recorded": "keep"}}, "changes": [{"identity_key": key, "after": "discard"}]}
    path = safe_paths.write_report("arm.json", json.dumps(report))
    data = json.loads(path.read_text(encoding="utf-8"))
    assert token not in json.dumps(data)
    assert len(data["identities"]) == 1 and data["changes"][0]["after"] == "discard"
