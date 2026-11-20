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

# Relabelled on 2026-11-20 (owner-delegated, see docs/self-improvement.md): en/es-positive-shape-04 and
# q-emea-only (remote EMEA, a preferred region of the profile) and q-distinguished (a preferred level) move from
# ambiguous to relevant; q-leuven-region-name (a Belgian city as the only location) moves from relevant to
# ambiguous. The measured outcomes of all 60 cases are unchanged; only the label buckets moved.
PINNED_COUNTS = {
    "relevant": {"kept": 20, "unsure": 0, "discarded": 0},
    "not_relevant": {"kept": 1, "unsure": 0, "discarded": 20},
    "ambiguous": {"kept": 3, "unsure": 3, "discarded": 13},
}
PINNED_RATES = {"relevant_kept_or_unsure_rate": 20 / 20, "not_relevant_kept_rate": 1 / 21}
PINNED_BANDS = {
    "relevant": {"strong": 20, "good": 0, "borderline": 0, "ineligible": 0},
    "not_relevant": {"strong": 9, "good": 5, "borderline": 7, "ineligible": 0},
    "ambiguous": {"strong": 12, "good": 6, "borderline": 1, "ineligible": 0},
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
