"""E1 dedup guard: advisory warning when a new idea re-treads a known node.

The guard must flag cross-branch re-treads, ignore the idea's own lineage, never
block the add, and be disableable via config.
"""

from __future__ import annotations

import asyncio

from arbor.coordinator.config import CoordinatorConfig, SearchConfig
from arbor.coordinator.dedup_guard import build_guard_index, dedup_warning
from arbor.coordinator.idea_tree import IdeaTree, Node
from arbor.coordinator.tools.tree_ops import TreeAddNodeTool

_THRESHOLD = 0.4


def _tree() -> IdeaTree:
    return IdeaTree(Node(id="ROOT", parent_id=None, depth=0))


def _add(tree: IdeaTree, nid: str, parent_id: str, *, hypothesis: str, status: str = "pending") -> Node:
    parent = tree.get_node(parent_id)
    node = Node(id=nid, parent_id=parent_id, depth=parent.depth + 1,
                hypothesis=hypothesis, status=status)
    tree.add_node(node)
    return node


def _cfg(*, dedup_on: bool = True) -> CoordinatorConfig:
    cfg = CoordinatorConfig(cwd=".")
    cfg.search.dedup_warning_on_add = dedup_on
    cfg.search.dedup_threshold = _THRESHOLD
    return cfg


# ── dedup_warning unit behaviour ─────────────────────────────────────────

def test_no_warning_for_genuinely_new_direction():
    tree = _tree()
    _add(tree, "1", "ROOT", hypothesis="bucketized select for kNN search", status="pruned")
    assert dedup_warning(tree, "ROOT", "post-training quantization to shrink the model",
                         threshold=_THRESHOLD) == ""


def test_warning_flags_sibling_retread_with_evidence():
    tree = _tree()
    _add(tree, "1", "ROOT", hypothesis="bucketized select for kNN search", status="pruned")
    warning = dedup_warning(tree, "ROOT", "bucketized selection to speed up kNN search",
                            threshold=_THRESHOLD)
    assert "WARNING (E1 dedup guard" in warning
    assert "node 1" in warning
    assert "pruned" in warning
    assert "0." in warning  # carries a score


def test_no_warning_within_own_lineage():
    """A child of a pruned node legitimately extends it — no self-flag."""
    tree = _tree()
    _add(tree, "1", "ROOT", hypothesis="bucketized select for kNN search", status="pruned")
    assert dedup_warning(tree, "1", "bucketized selection to speed up kNN search",
                         threshold=_THRESHOLD) == ""


def test_descendant_warned_against_other_branch():
    """Lineage exclusion is per-branch: a child under one branch can still be
    flagged against a *different* branch's known node."""
    tree = _tree()
    _add(tree, "1", "ROOT", hypothesis="bucketized select for kNN search", status="pruned")
    _add(tree, "2", "ROOT", hypothesis="homogeneous GEMM kernel", status="pruned")
    warning = dedup_warning(tree, "1", "homogeneous matrix multiply kernel",
                            threshold=_THRESHOLD)
    assert "node 2" in warning  # similar to branch 2, not its own branch 1


def test_empty_hypothesis_no_warning():
    tree = _tree()
    _add(tree, "1", "ROOT", hypothesis="anything", status="pruned")
    assert dedup_warning(tree, "ROOT", "   ") == ""


def test_build_guard_excludes_pending_empty_and_excluded():
    tree = _tree()
    _add(tree, "1", "ROOT", hypothesis="bucketized select", status="pruned")
    _add(tree, "2", "ROOT", hypothesis="block-min tournament", status="done")
    _add(tree, "3", "ROOT", hypothesis="still pending idea", status="pending")
    _add(tree, "4", "ROOT", hypothesis="", status="pruned")  # no text -> skipped
    index = build_guard_index(tree, exclude_ids={"1"})
    sessions = {f.session for f in index.findings}
    assert sessions == {"2"}


# ── TreeAddNode integration ──────────────────────────────────────────────

def test_tree_add_warns_but_does_not_block():
    tree = _tree()
    _add(tree, "1", "ROOT", hypothesis="bucketized select for kNN search", status="pruned")
    tool = TreeAddNodeTool(cwd=".", tree=tree, config=_cfg(dedup_on=True), provider=None)

    async def _go():
        return await tool.execute(parent_id="ROOT",
                                  hypothesis="bucketized selection to speed up kNN search")

    msg = asyncio.run(_go())
    assert "Added node 2" in msg
    assert "WARNING (E1 dedup guard" in msg
    # the idea was still added — advisory, never a block
    assert tree.get_node("2").hypothesis == "bucketized selection to speed up kNN search"


def test_tree_add_no_warning_when_disabled():
    tree = _tree()
    _add(tree, "1", "ROOT", hypothesis="bucketized select for kNN search", status="pruned")
    tool = TreeAddNodeTool(cwd=".", tree=tree, config=_cfg(dedup_on=False), provider=None)

    async def _go():
        return await tool.execute(parent_id="ROOT",
                                  hypothesis="bucketized selection to speed up kNN search")

    msg = asyncio.run(_go())
    assert "WARNING (E1 dedup guard" not in msg
    assert tree.get_node("2").hypothesis == "bucketized selection to speed up kNN search"


def test_tree_add_without_config_skips_guard():
    tree = _tree()
    _add(tree, "1", "ROOT", hypothesis="bucketized select for kNN search", status="pruned")
    tool = TreeAddNodeTool(cwd=".", tree=tree, config=None, provider=None)

    async def _go():
        return await tool.execute(parent_id="ROOT",
                                  hypothesis="bucketized selection to speed up kNN search")

    msg = asyncio.run(_go())
    assert "WARNING" not in msg
    assert "Added node 2" in msg


# ── config defaults ──────────────────────────────────────────────────────

def test_search_config_dedup_defaults():
    cfg = SearchConfig()
    assert cfg.dedup_warning_on_add is True
    assert cfg.dedup_threshold == 0.4
