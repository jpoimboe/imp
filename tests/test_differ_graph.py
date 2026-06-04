from __future__ import annotations

from imp.engine.differ_graph import find_differ_from_cycle, topo_order


def test_cycle_none_when_acyclic():
    assert find_differ_from_cycle({"a": "b"}) is None


def test_cycle_self_reference():
    cyc = find_differ_from_cycle({"a": "a"})
    assert cyc is not None and cyc[0] == "a"


def test_cycle_two_node():
    cyc = find_differ_from_cycle({"a": "b", "b": "a"})
    assert cyc is not None
    assert set(cyc) == {"a", "b"}


def test_cycle_three_node():
    cyc = find_differ_from_cycle({"a": "b", "b": "c", "c": "a"})
    assert cyc is not None
    assert set(cyc) == {"a", "b", "c"}


def test_topo_order_targets_precede_dependents():
    order = topo_order(["x", "y", "z"], {"y": "x", "z": "y"})
    assert order.index("x") < order.index("y") < order.index("z")


def test_topo_order_no_edges_is_sorted():
    assert topo_order(["b", "a", "c"], {}) == ["a", "b", "c"]
