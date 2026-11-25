"""Arm comparison pooling: clusters, stable job ids, blind labels and the spot check."""

from __future__ import annotations

import arm_pool as ap
import pytest
import yaml


def _row(n, company="Acme", title=None, location="Remote EU", url=None):
    return {"title": title or f"ML Engineer {n}", "company": company,
            "apply_url": url or f"https://jobs.example.test/{n}", "location": location}


def _state(arms, top_n=10, job_ids=None):
    return {"top_n": top_n, "arms": {arm: {rep: {"rows": rows} for rep, rows in reps.items()}
                                     for arm, reps in arms.items()}, "job_ids": job_ids or {}}


def test_alias_merges_same_posting_on_two_urls():
    alias = _row(9, title="ML Engineer 1", url="https://careers.example.test/x/1")
    pooled = ap.assign_ids(_state({"baseline": {"1": [_row(1)]}, "ultron": {"1": [alias]}}))
    assert len(pooled["jobs"]) == 1
    assert len(next(iter(pooled["jobs"].values()))["variants"]) == 2


@pytest.mark.parametrize("other", [
    _row(2, title="ML Engineer 1", location="New York"),  # same title, another country
    _row(2, title="ML Engineer 1", company="Not found"),  # placeholder company
    _row(2, title="(Remote)", company="Acme"),  # nothing left after normalizing
    _row(2, title="ML Engineer 1", location=""),  # no location to compare
    _row(2, title="ML Engineer 1", company="company_not_found"),  # ULTRON's missing-company sentinel
    _row(2, title="title_not_found"),  # ULTRON's missing-title sentinel
])
def test_distinct_requisitions_stay_apart(other):
    first = {**_row(1), "title": other["title"], "company": other["company"]}
    if other["title"] == "ML Engineer 1" and other["company"] == "Acme":
        first = _row(1)  # differs from ``other`` only by location
    pooled = ap.assign_ids(_state({"baseline": {"1": [first, other]}}))
    assert len(pooled["jobs"]) == 2


def test_bridge_row_joins_clusters_transitively():
    a = _row(1)
    b = _row(2, title="ML Engineer 3", url="https://other.example.test/b")
    bridge = {**_row(3), "apply_url": a["apply_url"]}  # URL of a, title of b
    pooled = ap.assign_ids(_state({"baseline": {"1": [a, b]}, "ultron": {"1": [bridge]}}))
    assert len(pooled["jobs"]) == 1


def test_ids_are_stable_whatever_the_ingestion_order():
    state = _state({"ultron": {"1": [_row(1, url="https://u.example.test/1")]}})
    first = ap.assign_ids(state)
    job = next(iter(first["jobs"]))
    state["arms"]["baseline"] = {"1": {"rows": [_row(1)]}}  # alias of the same job, sorts first
    second = ap.assign_ids(state)
    assert list(second["jobs"]) == [job] and second["relabel"] == []


def test_merged_ids_are_reported():
    state = _state({"baseline": {"1": [_row(1)]}, "ultron": {"1": [_row(2)]}})
    ap.assign_ids(state)
    state["arms"]["ultron"]["2"] = {"rows": [{**_row(1), "apply_url": _row(2)["apply_url"]}]}
    assert len(ap.assign_ids(state)["relabel"]) == 1


def test_split_cluster_gets_distinct_ids_and_relabel():
    state = _state({"baseline": {"1": [_row(1)]}, "ultron": {"1": [_row(2)]}})
    state["arms"]["ultron"]["2"] = {"rows": [{**_row(1), "apply_url": _row(2)["apply_url"]}]}  # bridge
    merged = ap.assign_ids(state)
    assert len(merged["jobs"]) == 1
    del state["arms"]["ultron"]["2"]  # the bridge repetition is discarded
    split = ap.assign_ids(state)
    assert len(split["jobs"]) == 2 and len(set(split["rows"].values())) == 2
    assert split["relabel"] and set(split["relabel"][0]) == set(split["jobs"])


def test_unused_id_skips_taken_ids():
    taken = {ap._job_id("id:a")}
    assert ap._unused_id(["id:a"], taken) not in taken


def test_only_top_n_rows_are_pooled():
    pooled = ap.assign_ids(_state({"ultron": {"1": [_row(1), _row(2), _row(3)]}}, top_n=2))
    assert len(pooled["jobs"]) == 2 and ("ultron", "1", 2) not in pooled["rows"]


def _labels_text(jobs, session="s1"):
    return yaml.safe_dump({"session": session, "jobs": jobs})


def test_parse_labels_accepts_pending_and_reads_it_as_unknown():
    labels = ap.parse_labels(_labels_text([{"id": "j1", "relevant": True, "open": True}]), "s1")
    assert labels["j1"]["eligible"] == ap.PENDING
    assert ap.scoring_labels(labels)["j1"] == {"relevant": True, "open": True, "eligible": None, "role_fit": None}
    assert ap.pending_jobs(labels, {"j1": {}, "j2": {}}) == ["j1", "j2"]


@pytest.mark.parametrize("text, message", [
    ("jobs: [\n", "invalid YAML at line"),
    ("jobs: 3\nsession: s1\n", "'jobs' list"),
    (_labels_text([{"id": "j1"}], session="other"), "belongs to session"),
    (_labels_text([{"relevant": True}]), "needs an id"),
    (_labels_text([{"id": "j1"}, {"id": "j1"}]), "listed twice"),
    (_labels_text([{"id": "j1", "relevant": 1}]), "true, false, null or pending"),
    (_labels_text([{"id": "j1", "relevant": True, "open": False}]), "relevant is true"),
])
def test_parse_labels_rejects_bad_files(text, message):
    with pytest.raises(ap.LabelError, match=message):
        ap.parse_labels(text, "s1")


def test_yaml_error_does_not_echo_the_file():
    with pytest.raises(ap.LabelError) as excinfo:
        ap.parse_labels("jobs: [\nSECRET-SENTINEL: {", "s1")
    assert "SECRET" not in str(excinfo.value)


def test_labels_yaml_is_blind_and_keeps_known_labels():
    jobs = {"j1": {"title": "T", "company": "C", "variants": []}, "j2": {"title": "U", "company": "C", "variants": []}}
    text = ap.labels_yaml("s1", jobs, {"j1": {"relevant": False, "open": True, "eligible": True, "role_fit": True}})
    parsed = ap.parse_labels(text, "s1")
    assert parsed["j1"]["relevant"] is False and parsed["j2"]["relevant"] == ap.PENDING
    assert "ultron" not in text and "baseline" not in text


def test_spotcheck_sample_and_agreement():
    jobs = {f"j{i}": {"title": "T", "company": "C", "variants": []} for i in range(5)}
    sample = ap.parse_labels(ap.spotcheck_yaml("s1", jobs, 3), "s1", fields=("relevant",))
    assert len(sample) == 3 and ap.spotcheck_yaml("s1", jobs, 3) == ap.spotcheck_yaml("s1", jobs, 3)
    labels = {"j0": {"relevant": True}, "j1": {"relevant": False}, "j2": {"relevant": None}}
    spot = {"j0": {"relevant": True}, "j1": {"relevant": True}, "j2": {"relevant": True}, "j3": {"relevant": "pending"}}
    result = ap.agreement(labels, spot, 0.15)
    assert result == {"compared": 2, "pending": 1, "disagreements": ["j1"], "disagreement_rate": 0.5, "relabel": True}
    assert ap.agreement({}, {}, 0.15)["disagreement_rate"] is None
