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


class _CountingList(list):
    """A list that counts how many items any traversal pulls from it."""

    pulled = 0

    def __iter__(self):
        for item in super().__iter__():
            type(self).pulled += 1
            yield item

    def __reversed__(self):
        for item in super().__reversed__():
            type(self).pulled += 1
            yield item


def test_iter_json_dicts_reads_wide_containers_lazily():
    _CountingList.pulled = 0
    wide = _CountingList({} for _ in range(10_000))
    assert len(list(iter_json_dicts(wide, max_nodes=4))) == 3
    assert _CountingList.pulled <= 4  # the node cap also bounds what is read and kept pending


def test_iter_json_dicts_edge_caps():
    assert list(iter_json_dicts({"a": {}}, max_nodes=0)) == []
    root = {"a": {"b": {}}}
    assert list(iter_json_dicts(root, max_depth=0)) == [root]
    assert list(iter_json_dicts(root, max_nodes=1)) == [root]
    assert list(iter_json_dicts(root, max_depth=1)) == [root, root["a"]]
