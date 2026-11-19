from __future__ import annotations

from ultron.core.json_walk import iter_json_dicts, load_json_or_none


def test_load_json_or_none_rejects_invalid_and_too_deep_json():
    assert load_json_or_none('{"a": 1}') == {"a": 1}
    assert load_json_or_none("{not json") is None
    assert load_json_or_none("[" * 200000) is None


def test_iter_json_dicts_keeps_pre_order():
    payload = {"id": 0, "a": [{"id": 1, "b": {"id": 2}}, {"id": 3}], "c": {"id": 4}}
    assert [node["id"] for node in iter_json_dicts(payload)] == [0, 1, 2, 3, 4]


def test_iter_json_dicts_respects_depth_and_node_caps():
    nested: dict = {"id": "leaf"}
    for _ in range(10):
        nested = {"child": nested}
    assert all("id" not in node for node in iter_json_dicts(nested, max_depth=5))
    assert len(list(iter_json_dicts([{} for _ in range(100)], max_nodes=11))) == 10
    # Scalars do not consume the node budget.
    assert len(list(iter_json_dicts([{"i": i, "s": "x"} for i in range(100)], max_nodes=11))) == 10
    assert list(iter_json_dicts("scalar")) == []
