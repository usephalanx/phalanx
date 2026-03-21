"""
DAG resolver — builds and traverses the task dependency graph.

DagNode: one task in the graph, with its upstream deps.
DagResolver: topological sort + critical-path calculation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import structlog

log = structlog.get_logger(__name__)


@dataclass
class DagNode:
    task_id: str
    agent_role: str
    estimated_minutes: int = 30
    # {depends_on_task_id: dep_type}
    deps: dict[str, str] = field(default_factory=dict)


@dataclass
class DagPlan:
    groups: list[list[str]]          # parallel groups in execution order
    critical_path_minutes: int


class DagResolver:
    """
    Resolves a dict[task_id, DagNode] into execution groups and critical path.
    """

    def resolve(self, nodes: dict[str, DagNode]) -> DagPlan:
        """
        Topological sort (Kahn's algorithm) → parallel groups.
        Critical path = longest chain by estimated_minutes.
        """
        if not nodes:
            return DagPlan(groups=[], critical_path_minutes=0)

        in_degree: dict[str, int] = {tid: 0 for tid in nodes}
        for node in nodes.values():
            for dep_id in node.deps:
                if dep_id in in_degree:
                    in_degree[node.task_id] = in_degree[node.task_id] + 1

        # Recalculate properly
        in_degree = {tid: 0 for tid in nodes}
        for node in nodes.values():
            for dep_id in node.deps:
                if dep_id in nodes:
                    in_degree[node.task_id] += 1

        groups: list[list[str]] = []
        completed: set[str] = set()

        while len(completed) < len(nodes):
            ready = [
                tid for tid, deg in in_degree.items()
                if deg == 0 and tid not in completed
            ]
            if not ready:
                remaining = set(nodes) - completed
                raise ValueError(f"DAG cycle detected, remaining: {remaining}")

            groups.append(sorted(ready))
            for tid in ready:
                completed.add(tid)
                # Reduce in-degree for dependents
                for node in nodes.values():
                    if tid in node.deps and node.task_id not in completed:
                        in_degree[node.task_id] -= 1

        critical_path = self._critical_path(nodes)
        log.info("dag.resolved", tasks=len(nodes), groups=len(groups),
                 critical_path_minutes=critical_path)
        return DagPlan(groups=groups, critical_path_minutes=critical_path)

    def _critical_path(self, nodes: dict[str, DagNode]) -> int:
        """Longest path by estimated_minutes (DP)."""
        cache: dict[str, int] = {}

        def dp(tid: str) -> int:
            if tid in cache:
                return cache[tid]
            node = nodes[tid]
            if not node.deps:
                cache[tid] = node.estimated_minutes
            else:
                max_upstream = max(
                    dp(dep_id) for dep_id in node.deps if dep_id in nodes
                ) if any(d in nodes for d in node.deps) else 0
                cache[tid] = max_upstream + node.estimated_minutes
            return cache[tid]

        return max((dp(tid) for tid in nodes), default=0)

    def build_nodes(
        self,
        tasks: list[Any],
        deps: list[Any],
    ) -> dict[str, DagNode]:
        """Build a DagNode dict from ORM Task + TaskDependency lists."""
        nodes: dict[str, DagNode] = {
            t.id: DagNode(
                task_id=t.id,
                agent_role=t.agent_role,
                estimated_minutes=getattr(t, "estimated_minutes", 30) or 30,
            )
            for t in tasks
        }
        for dep in deps:
            if dep.task_id in nodes and dep.depends_on_id in nodes:
                nodes[dep.task_id].deps[dep.depends_on_id] = getattr(
                    dep, "dependency_type", "full"
                )
        return nodes

    def get_ready(
        self,
        nodes: dict[str, DagNode],
        completed_ids: set[str],
    ) -> list[str]:
        """Return task IDs whose deps are all in completed_ids."""
        return [
            tid for tid, node in nodes.items()
            if tid not in completed_ids
            and all(dep_id in completed_ids for dep_id in node.deps)
        ]
