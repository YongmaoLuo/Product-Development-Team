"""Task-graph primitives: dangling references and true cycles.

Why this module exists (2026-09-21, a production plan)
-----------------------------------------------------------------------
Two guards — ``task_manager._ensure_acyclic`` and
``agent._validate_dependencies`` — both used Kahn's residual to answer
"is this task graph cyclic?" and both got it wrong in the same way.

Kahn's residual answers a *different* question: "which nodes can never be
scheduled?" A node lands in the residual for any of three reasons:

1. it is in a cycle;
2. a ``depends_on`` entry names an id that does not exist — the in-degree
   is counted from ``len(deps)`` while the reverse adjacency is only
   built for ids that *do* exist, so the count never reaches zero;
3. it is downstream of (1) or (2).

Only (1) is a cycle. Reporting (2) and (3) as "Cycle detected" made one
dangling reference look like an 18-node cycle: the refiner split parent
``3`` into ``3-1..3-4`` and left a single stale reference to the removed
parent, and every task downstream of it was named as a cycle member. The
resulting ``CycleInTaskGraph`` had no handler on the refinement path, so
the executor died with ``No schedulable micro-layer found``.

So the three questions are now asked separately and precisely:

* :func:`find_dangling_references` — which ``depends_on`` entries name a
  task that is not in the list (the actionable, almost-always-true cause);
* :func:`find_cycle_groups` — which nodes are *actually* in a cycle
  (strongly-connected components of size ≥ 2, plus self-loops);
* everything else is simply "blocked", and callers report it as such.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Sequence, Tuple


class DanglingTaskDependency(ValueError):
    """A ``depends_on`` entry names a task id that does not exist.

    Inherits ``ValueError`` so existing ``except ValueError`` blocks keep
    working — this is a malformed-input error, not a crash.

    Distinct from :class:`task_manager.CycleInTaskGraph` because the two
    need different responses: a dangling reference is a *bookkeeping* bug
    (the producer referenced a task that was renamed / removed / split)
    and is repairable by rewriting the edge, whereas a cycle is a
    structural bug in the plan itself.
    """

    def __init__(self, pairs: Sequence[Tuple[str, str]]):
        self.pairs = [(str(t), str(d)) for t, d in pairs]
        preview = ", ".join(f"{t} -> {d}" for t, d in self.pairs[:5])
        more = (
            f" (+{len(self.pairs) - 5} more)"
            if len(self.pairs) > 5
            else ""
        )
        super().__init__(
            f"Task {self.pairs[0][0]} depends on missing task "
            f"{self.pairs[0][1]}"
            + (f"; also: {preview}{more}" if len(self.pairs) > 1 else "")
        )


def _deps_of(task) -> List[str]:
    deps = getattr(task, "depends_on", None) or []
    return [d for d in deps if isinstance(d, str)]


def find_dangling_references(
    tasks: Iterable,
) -> List[Tuple[str, str]]:
    """Return ``[(task_id, missing_dep_id), ...]`` in input order.

    Empty when every ``depends_on`` entry resolves. Never raises — a task
    without a usable ``depends_on`` simply contributes nothing.
    """
    tasks = list(tasks)
    ids = {t.id for t in tasks}
    out: List[Tuple[str, str]] = []
    for task in tasks:
        for dep in _deps_of(task):
            if dep not in ids:
                out.append((task.id, dep))
    return out


def find_self_loops(tasks: Iterable) -> List[str]:
    """Return the ids of tasks that list themselves in ``depends_on``."""
    return [t.id for t in tasks if t.id in _deps_of(t)]


def _strongly_connected(
    nodes: List[str], adjacency: Dict[str, List[str]]
) -> List[List[str]]:
    """Iterative Tarjan SCC (recursion would blow the stack on long
    hierarchical chains — a 4-deep split is common, an accidental
    self-referential chain is not impossible)."""
    index: Dict[str, int] = {}
    low: Dict[str, int] = {}
    on_stack: set = set()
    stack: List[str] = []
    components: List[List[str]] = []
    counter = 0

    for root in nodes:
        if root in index:
            continue
        work: List[List] = [[root, 0]]
        while work:
            frame = work[-1]
            node, pointer = frame
            if pointer == 0:
                index[node] = low[node] = counter
                counter += 1
                stack.append(node)
                on_stack.add(node)

            neighbours = adjacency.get(node, [])
            advanced = False
            while pointer < len(neighbours):
                nxt = neighbours[pointer]
                pointer += 1
                if nxt not in index:
                    frame[1] = pointer
                    work.append([nxt, 0])
                    advanced = True
                    break
                if nxt in on_stack:
                    low[node] = min(low[node], index[nxt])
                    frame[1] = pointer
                    continue
                frame[1] = pointer
            if advanced:
                continue

            if low[node] == index[node]:
                component: List[str] = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.append(member)
                    if member == node:
                        break
                components.append(component)

            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])

    return components


def find_cycle_groups(tasks: Iterable) -> List[List[str]]:
    """Return the ids that are genuinely inside a cycle, grouped.

    A group is a strongly-connected component of size ≥ 2, or a single
    node that depends on itself. Nodes merely *downstream* of a cycle are
    NOT included — they are blocked, not cyclic, and conflating the two is
    exactly the bug this module was written to end.

    Groups are sorted (by their sorted member list) so the reporting order
    is deterministic.
    """
    tasks = list(tasks)
    if not tasks:
        return []

    ids = {t.id for t in tasks}
    adjacency: Dict[str, List[str]] = {t.id: [] for t in tasks}
    for task in tasks:
        for dep in _deps_of(task):
            if dep in ids:
                adjacency[dep].append(task.id)

    # ``ids`` is a set, so iterate it SORTED: Tarjan emits components in
    # traversal order, and PYTHONHASHSEED makes set order differ between
    # processes. Feeding it an unsorted set made the member order inside a
    # group flap between runs (``['b', 'a']`` vs ``['a', 'b']``), which is
    # visible in the operator-facing "cycle in tasks.json" message.
    groups = [
        sorted(c) for c in _strongly_connected(sorted(ids), adjacency) if len(c) > 1
    ]
    groups.extend([[tid] for tid in find_self_loops(tasks)])
    return sorted(groups, key=lambda g: sorted(g))
