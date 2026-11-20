"""The 60-case corpus is labelled, and its measured confusion counts are pinned.

The pin makes every decision change visible as a measured delta. When a change moves these numbers
on purpose, run ``uv run python tools/measure_quality.py``, copy the new ``counts`` and rates into
``PINNED_COUNTS`` / ``PINNED_RATES`` below, and explain the delta (which cases moved and why) in the
pull request. Never edit ``algorithm_quality_labels.json`` to make the pin pass: the labels are
human-owned ground truth, independent of what the code outputs.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import measure_quality
import pytest

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

# Updated with the closed-status / non-EU / Spanish-title filter fixes: relevant 14 kept + 3 discarded
# -> 17 kept (es-positive-shape-01, q-es-senior-remoto-espana, en-positive-shape-07 now kept);
# not_relevant 5 kept -> 1 kept (closed postings en/es-negative-shape-08/07 and non-EU remote ones
# now discarded). Ambiguous cases and score bands are unchanged.
PINNED_COUNTS = {
    "relevant": {"kept": 17, "unsure": 0, "discarded": 0},
    "not_relevant": {"kept": 1, "unsure": 0, "discarded": 20},
    "ambiguous": {"kept": 6, "unsure": 3, "discarded": 13},
}
PINNED_RATES = {"relevant_kept_or_unsure_rate": 17 / 17, "not_relevant_kept_rate": 1 / 21}
PINNED_BANDS = {
    "relevant": {"strong": 17, "good": 0, "borderline": 0, "ineligible": 0},
    "not_relevant": {"strong": 9, "good": 5, "borderline": 7, "ineligible": 0},
    "ambiguous": {"strong": 15, "good": 6, "borderline": 1, "ineligible": 0},
}


def _labels() -> dict:
    return json.loads((FIXTURES / "algorithm_quality_labels.json").read_text(encoding="utf-8"))


def test_every_case_has_exactly_one_valid_label():
    corpus = json.loads((FIXTURES / "heuristic_characterization_corpus.json").read_text(encoding="utf-8"))
    labels = _labels()["labels"]
    assert set(labels) == {case["id"] for case in corpus["cases"]}
    assert all(
        item["label"] in {"relevant", "not_relevant", "ambiguous"} and item["reason"].strip()
        for item in labels.values()
    )


def test_labels_are_synthetic_and_carry_no_personal_data():
    data = _labels()
    assert "synthetic" in data["purpose"].lower()
    text = json.dumps(data)
    assert not re.search(r"[\w.+-]+@[\w-]+\.\w+|https?://", text)


def test_measured_confusion_counts_are_pinned():
    report = measure_quality.measure()
    assert report["counts"] == PINNED_COUNTS
    assert report["score_band_by_label"] == PINNED_BANDS
    for key, value in PINNED_RATES.items():
        assert abs(report[key] - value) < 1e-12, key
    assert len(report["cases"]) == 60


def test_measurement_is_deterministic():
    first, second = measure_quality.measure(), measure_quality.measure()
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    ids = [case["id"] for case in first["cases"]]
    assert ids == sorted(ids), "cases are reported in a stable id order"


@pytest.mark.parametrize("name", ["../escape.json", "sub/quality.json", "/abs/quality.json", ".."])
def test_output_must_be_a_plain_file_name(name, reports_dir, capsys):
    assert measure_quality.main(["--output", name]) == 2
    assert "plain file name" in json.loads(capsys.readouterr().out)["error"]
    assert not reports_dir.exists()


def test_cli_writes_the_same_report(reports_dir, capsys):
    assert measure_quality.main(["--output", "quality.json"]) == 0
    assert json.loads(capsys.readouterr().out)["report"] == str(reports_dir / "quality.json")
    assert json.loads((reports_dir / "quality.json").read_text(encoding="utf-8")) == measure_quality.measure()
    assert measure_quality.main([]) == 0
    assert json.loads(capsys.readouterr().out)["counts"] == PINNED_COUNTS
