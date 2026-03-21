"""Unit tests for phalanx/workflow/dag.py"""
from __future__ import annotations

import pytest

from phalanx.workflow.dag import DagNode, DagPlan, DagResolver


def _node(task_id, role="builder", mins=30, deps=None):
    n = DagNode(task_id=task_id, agent_role=role, estimated_minutes=mins)
    n.deps = deps or {}
    return n


class TestDagResolverResolve:
    def test_empty_nodes_returns_empty_plan(self):
        r = DagResolver()
        plan = r.resolve({})
        assert plan.groups == []
        assert plan.critical_path_minutes == 0

    def test_single_node(self):
        r = DagResolver()
        plan = r.resolve({"a": _node("a", mins=20)})
        assert plan.groups == [["a"]]
        assert plan.critical_path_minutes == 20

    def test_linear_chain(self):
        r = DagResolver()
        nodes = {
            "a": _node("a", mins=10),
            "b": _node("b", mins=20, deps={"a": "full"}),
            "c": _node("c", mins=15, deps={"b": "full"}),
        }
        plan = r.resolve(nodes)
        assert plan.groups[0] == ["a"]
        assert plan.groups[1] == ["b"]
        assert plan.groups[2] == ["c"]
        assert plan.critical_path_minutes == 45  # 10+20+15

    def test_parallel_tasks_same_group(self):
        r = DagResolver()
        nodes = {
            "a": _node("a", mins=10),
            "b": _node("b", mins=10),
        }
        plan = r.resolve(nodes)
        assert len(plan.groups) == 1
        assert sorted(plan.groups[0]) == ["a", "b"]

    def test_diamond_dag(self):
        r = DagResolver()
        nodes = {
            "root": _node("root", mins=5),
            "left": _node("left", mins=20, deps={"root": "full"}),
            "right": _node("right", mins=10, deps={"root": "full"}),
            "merge": _node("merge", mins=5, deps={"left": "full", "right": "full"}),
        }
        plan = r.resolve(nodes)
        # Critical path: root(5) + left(20) + merge(5) = 30
        assert plan.critical_path_minutes == 30

    def test_cycle_raises(self):
        r = DagResolver()
        nodes = {
            "a": _node("a", deps={"b": "full"}),
            "b": _node("b", deps={"a": "full"}),
        }
        with pytest.raises(ValueError, match="cycle"):
            r.resolve(nodes)


class TestGetReady:
    def test_returns_nodes_with_all_deps_complete(self):
        r = DagResolver()
        nodes = {
            "a": _node("a"),
            "b": _node("b", deps={"a": "full"}),
            "c": _node("c", deps={"a": "full", "b": "full"}),
        }
        assert r.get_ready(nodes, set()) == ["a"]
        assert sorted(r.get_ready(nodes, {"a"})) == ["b"]
        assert sorted(r.get_ready(nodes, {"a", "b"})) == ["c"]

    def test_excludes_already_completed(self):
        r = DagResolver()
        nodes = {"a": _node("a"), "b": _node("b")}
        assert r.get_ready(nodes, {"a"}) == ["b"]


class TestBuildNodes:
    def test_builds_from_orm_objects(self):
        from unittest.mock import MagicMock
        r = DagResolver()

        t1 = MagicMock(id="t1", agent_role="builder", estimated_minutes=30)
        t2 = MagicMock(id="t2", agent_role="reviewer", estimated_minutes=15)
        dep = MagicMock(task_id="t2", depends_on_id="t1", dependency_type="full")

        nodes = r.build_nodes([t1, t2], [dep])
        assert "t1" in nodes
        assert "t2" in nodes
        assert nodes["t2"].deps == {"t1": "full"}

    def test_ignores_dep_with_unknown_task_id(self):
        from unittest.mock import MagicMock
        r = DagResolver()

        t1 = MagicMock(id="t1", agent_role="builder", estimated_minutes=30)
        dep = MagicMock(task_id="t99", depends_on_id="t1", dependency_type="full")

        nodes = r.build_nodes([t1], [dep])
        assert "t99" not in nodes
