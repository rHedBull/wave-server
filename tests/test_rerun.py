"""Tests for selective task rerun with cascade / isolated modes.

Covers:
- compute_dirty_closure: explicit DAG edges, implicit cross-section cascading, isolated mode
- Rerun via wave executor: skip set computation, correct tasks re-executed
- Rerun API endpoint: validation, trigger type, error cases
"""

from __future__ import annotations


from wave_server.engine.dag import compute_dirty_closure
from wave_server.engine.types import Feature, Plan, Task, Wave


# ── Helpers ────────────────────────────────────────────────────


def _task(
    id: str, depends: list[str] | None = None, files: list[str] | None = None
) -> Task:
    return Task(id=id, title=f"Task {id}", depends=depends or [], files=files or [])


_REQUIRED = {
    "project_structure": "## Project Structure\nsrc/",
    "data_schemas": "## Data Schemas\nplaceholder",
}


def _example_plan() -> Plan:
    """Plan from the concrete example in the design doc.

    Wave 1:
      Foundation: w1-f1, w1-f2 (depends: w1-f1)
      Feature users: w1-users-t1 (depends: w1-users-t2), w1-users-t2
      Feature billing: w1-billing-t1 (depends: w1-billing-t2), w1-billing-t2
      Integration: w1-int-1
    """
    return Plan(
        **_REQUIRED,
        waves=[
            Wave(
                name="W1",
                foundation=[
                    _task("w1-f1"),
                    _task("w1-f2", depends=["w1-f1"]),
                ],
                features=[
                    Feature(
                        name="users",
                        tasks=[
                            _task("w1-users-t1", depends=["w1-users-t2"]),
                            _task("w1-users-t2"),
                        ],
                    ),
                    Feature(
                        name="billing",
                        tasks=[
                            _task("w1-billing-t1", depends=["w1-billing-t2"]),
                            _task("w1-billing-t2"),
                        ],
                    ),
                ],
                integration=[_task("w1-int-1")],
            )
        ],
    )


# ── compute_dirty_closure: isolated mode ───────────────────────


class TestDirtyClosureIsolated:
    def test_returns_only_selected_tasks(self):
        plan = _example_plan()
        dirty = compute_dirty_closure(plan, {"w1-f2", "w1-billing-t2"}, cascade=False)
        assert dirty == {"w1-f2", "w1-billing-t2"}

    def test_single_task_isolated(self):
        plan = _example_plan()
        dirty = compute_dirty_closure(plan, {"w1-int-1"}, cascade=False)
        assert dirty == {"w1-int-1"}

    def test_empty_rerun_ids(self):
        plan = _example_plan()
        dirty = compute_dirty_closure(plan, set(), cascade=False)
        assert dirty == set()


# ── compute_dirty_closure: cascade mode ────────────────────────


class TestDirtyClosureCascade:
    def test_foundation_cascades_to_features_and_integration(self):
        """Dirty foundation task → all features + integration dirty."""
        plan = _example_plan()
        dirty = compute_dirty_closure(plan, {"w1-f2"}, cascade=True)
        # w1-f2 is foundation → all features + integration become dirty
        assert "w1-f2" in dirty
        assert "w1-users-t1" in dirty
        assert "w1-users-t2" in dirty
        assert "w1-billing-t1" in dirty
        assert "w1-billing-t2" in dirty
        assert "w1-int-1" in dirty
        # w1-f1 should NOT be dirty (upstream of w1-f2, not downstream)
        assert "w1-f1" not in dirty

    def test_feature_task_cascades_to_integration(self):
        """Dirty feature task → integration dirty, other features untouched."""
        plan = _example_plan()
        dirty = compute_dirty_closure(plan, {"w1-billing-t2"}, cascade=True)
        # w1-billing-t2 is in features → integration becomes dirty
        assert "w1-billing-t2" in dirty
        assert "w1-billing-t1" in dirty  # explicit dep on w1-billing-t2
        assert "w1-int-1" in dirty  # implicit: features → integration
        # Foundation and other features NOT dirty
        assert "w1-f1" not in dirty
        assert "w1-f2" not in dirty
        assert "w1-users-t1" not in dirty
        assert "w1-users-t2" not in dirty

    def test_integration_task_no_extra_cascade(self):
        """Dirty integration task → only that task (+ explicit dependents)."""
        plan = _example_plan()
        dirty = compute_dirty_closure(plan, {"w1-int-1"}, cascade=True)
        assert dirty == {"w1-int-1"}

    def test_explicit_dag_edge_cascades(self):
        """Task with depends edge is marked dirty when its dependency is dirty."""
        plan = _example_plan()
        # w1-f1 → w1-f2 depends on it
        dirty = compute_dirty_closure(plan, {"w1-f1"}, cascade=True)
        assert "w1-f1" in dirty
        assert "w1-f2" in dirty  # explicit depends edge
        # Foundation dirty → all features + integration dirty too
        assert "w1-users-t1" in dirty
        assert "w1-int-1" in dirty

    def test_multiple_selected_tasks(self):
        """Selecting tasks from different sections."""
        plan = _example_plan()
        dirty = compute_dirty_closure(plan, {"w1-f2", "w1-billing-t2"}, cascade=True)
        # w1-f2 is foundation → cascades everything downstream
        # w1-billing-t2 is feature → adds billing-t1 + integration
        # Combined: everything except w1-f1
        assert "w1-f1" not in dirty
        assert len(dirty) == 6  # all except w1-f1

    def test_empty_rerun_ids_cascade(self):
        plan = _example_plan()
        dirty = compute_dirty_closure(plan, set(), cascade=True)
        assert dirty == set()


class TestDirtyClosureMultiWave:
    def test_dirty_in_wave1_does_not_affect_wave2(self):
        """Tasks in wave 2 are independent — dirty in wave 1 doesn't cascade to wave 2."""
        plan = Plan(
            **_REQUIRED,
            waves=[
                Wave(
                    name="W1",
                    foundation=[_task("w1-f1")],
                    features=[Feature(name="default", tasks=[_task("w1-t1")])],
                    integration=[_task("w1-i1")],
                ),
                Wave(
                    name="W2",
                    foundation=[_task("w2-f1")],
                    features=[Feature(name="default", tasks=[_task("w2-t1")])],
                    integration=[_task("w2-i1")],
                ),
            ],
        )
        dirty = compute_dirty_closure(plan, {"w1-f1"}, cascade=True)
        # Wave 1: foundation dirty → cascades to features + integration
        assert "w1-f1" in dirty
        assert "w1-t1" in dirty
        assert "w1-i1" in dirty
        # Wave 2: untouched
        assert "w2-f1" not in dirty
        assert "w2-t1" not in dirty
        assert "w2-i1" not in dirty

    def test_dirty_spans_multiple_waves(self):
        """Selecting tasks in both waves makes both dirty independently."""
        plan = Plan(
            **_REQUIRED,
            waves=[
                Wave(
                    name="W1",
                    foundation=[_task("w1-f1")],
                    integration=[_task("w1-i1")],
                ),
                Wave(
                    name="W2",
                    foundation=[_task("w2-f1")],
                    integration=[_task("w2-i1")],
                ),
            ],
        )
        dirty = compute_dirty_closure(plan, {"w1-i1", "w2-f1"}, cascade=True)
        assert dirty == {"w1-i1", "w2-f1", "w2-i1"}


class TestDirtyClosureDeepDAG:
    def test_transitive_chain(self):
        """a → b → c: dirtying a makes b and c dirty."""
        plan = Plan(
            **_REQUIRED,
            waves=[
                Wave(
                    name="W1",
                    foundation=[
                        _task("a"),
                        _task("b", depends=["a"]),
                        _task("c", depends=["b"]),
                    ],
                )
            ],
        )
        dirty = compute_dirty_closure(plan, {"a"}, cascade=True)
        assert dirty == {"a", "b", "c"}

    def test_diamond_dependency(self):
        """Diamond: a → b, a → c, b+c → d. Dirtying a cascades to all."""
        plan = Plan(
            **_REQUIRED,
            waves=[
                Wave(
                    name="W1",
                    foundation=[
                        _task("a"),
                        _task("b", depends=["a"]),
                        _task("c", depends=["a"]),
                        _task("d", depends=["b", "c"]),
                    ],
                )
            ],
        )
        dirty = compute_dirty_closure(plan, {"a"}, cascade=True)
        assert dirty == {"a", "b", "c", "d"}

    def test_middle_of_chain(self):
        """a → b → c: dirtying b makes c dirty but NOT a."""
        plan = Plan(
            **_REQUIRED,
            waves=[
                Wave(
                    name="W1",
                    foundation=[
                        _task("a"),
                        _task("b", depends=["a"]),
                        _task("c", depends=["b"]),
                    ],
                )
            ],
        )
        dirty = compute_dirty_closure(plan, {"b"}, cascade=True)
        assert dirty == {"b", "c"}
        assert "a" not in dirty


# ── Wave executor integration: skip set with rerun ─────────────


class TestRerunSkipSet:
    """Test that the skip-set logic correctly handles rerun."""

    def test_skip_set_cascade(self):
        """Simulate: 7 completed tasks, rerun w1-billing-t2 with cascade."""
        plan = _example_plan()
        all_completed = {
            "w1-f1",
            "w1-f2",
            "w1-users-t1",
            "w1-users-t2",
            "w1-billing-t1",
            "w1-billing-t2",
            "w1-int-1",
        }
        rerun_ids = {"w1-billing-t2"}
        dirty = compute_dirty_closure(plan, rerun_ids, cascade=True)
        skip = all_completed - dirty

        # Only w1-f1, w1-f2, w1-users-t1, w1-users-t2 should be skipped
        assert skip == {"w1-f1", "w1-f2", "w1-users-t1", "w1-users-t2"}
        # These should run: billing-t2, billing-t1, int-1
        assert dirty == {"w1-billing-t2", "w1-billing-t1", "w1-int-1"}

    def test_skip_set_isolated(self):
        """Simulate: 7 completed tasks, rerun w1-billing-t2 isolated."""
        plan = _example_plan()
        all_completed = {
            "w1-f1",
            "w1-f2",
            "w1-users-t1",
            "w1-users-t2",
            "w1-billing-t1",
            "w1-billing-t2",
            "w1-int-1",
        }
        rerun_ids = {"w1-billing-t2"}
        dirty = compute_dirty_closure(plan, rerun_ids, cascade=False)
        skip = all_completed - dirty

        # Only w1-billing-t2 reruns, everything else skipped
        assert dirty == {"w1-billing-t2"}
        assert skip == all_completed - {"w1-billing-t2"}

    def test_skip_set_foundation_cascade(self):
        """Rerun a foundation task with cascade — most tasks should run."""
        plan = _example_plan()
        all_completed = {
            "w1-f1",
            "w1-f2",
            "w1-users-t1",
            "w1-users-t2",
            "w1-billing-t1",
            "w1-billing-t2",
            "w1-int-1",
        }
        rerun_ids = {"w1-f2"}
        dirty = compute_dirty_closure(plan, rerun_ids, cascade=True)
        skip = all_completed - dirty

        # Only w1-f1 skipped (not downstream of w1-f2)
        assert skip == {"w1-f1"}
