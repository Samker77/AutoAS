"""Counterfactual replay of the dedup guard (Direction-1 validation tool).

Replays a completed run's dispatch history and answers the question: "had a
similarity guard existed at dispatch time, which executor runs would it have
flagged as near-duplicates of already-known failed/succeeded directions?"

This is how we measure the design's value *before* it has accumulated history:
instead of waiting for real runs to build memory, we replay the history a run
already left behind and count what the guard would have caught. Same idea works
on a real ``idea_tree.json`` (nodes replayed in creation order) or on a
synthetic scenario that mirrors a documented waste pattern.

Deterministic: same nodes + same threshold → same flags, so it is testable.
"""

from __future__ import annotations

from typing import Any, Iterable

from .memory_index import Finding, MemoryIndex

# A node that has consumed executor budget (was dispatched as an experiment).
_DISPATCHED = {"done", "merged", "pruned", "failed", "needs_retry"}
# A node whose outcome is now *known* and should seed the guard (has a lesson).
_GUARDED = {"pruned", "done", "merged", "failed", "needs_retry"}


def _infer_parent(node_id: str) -> str | None:
    """Parent id from the dotted scheme ("1.1.1" -> "1.1"); None for a root."""
    if "." in node_id:
        return node_id.rsplit(".", 1)[0]
    return None


def _lineage(all_nodes: dict[str, dict[str, Any]], node_id: str) -> set[str]:
    """The node id plus its ancestor chain (used to ignore in-lineage matches)."""
    ids: set[str] = set()
    cur = node_id
    while cur and cur not in ids:
        ids.add(cur)
        node = all_nodes.get(cur)
        if node is None:
            break
        cur = node.get("parent_id") or _infer_parent(cur)
    return ids


def counterfactual_replay(
    nodes: Iterable[dict[str, Any]],
    *,
    threshold: float = 0.5,
    k: int = 3,
    domain: str | None = None,
) -> dict[str, Any]:
    """Replay dispatch history and report which runs the guard would have flagged.

    ``nodes`` is an iterable of node records in dispatch/creation order; each
    carries at least ``hypothesis`` (str) and ``status`` (str). ``id`` (str) is
    used for provenance in the report, and ``parent_id`` (str, optional) marks
    the tree structure. When ``parent_id`` is absent it is inferred from the
    dotted id scheme ("1.1" is a child of "1").

    Semantics: walk the history in order. A dispatched node is checked against
    every *earlier* node whose outcome is known (pruned/done/merged/failed). If
    any of them scores at/above ``threshold``, the dispatch is flagged. Once a
    node's outcome is known it seeds the guard for everything that follows. A
    node's own lineage (its ancestors) is excluded from the check, mirroring the
    production ``dedup_warning`` guard — a child legitimately extends its parent.

    Each flag carries the *full* set of matching prior nodes as evidence, so the
    guard's reasoning is auditable — including when it anchors on the wrong node
    (a known weakness of the lexical proxy the report surfaces honestly).

    Returns ``{"considered": int, "flagged": [...], "caught": int}`` where each
    flagged entry is
    ``{"node_id", "hypothesis", "matches": [{node_id, status, score}], "score"}``
    with ``matches`` ordered best-first and ``score`` the best match.
    """
    index = MemoryIndex()
    all_nodes = {str(n.get("id")): n for n in nodes if n.get("id")}
    flagged: list[dict[str, Any]] = []
    considered = 0
    for n in nodes:
        hypothesis = (n.get("hypothesis") or "").strip()
        if not hypothesis:
            continue
        status = n.get("status") or "pending"
        node_id = str(n.get("id") or "?")
        if status in _DISPATCHED:
            considered += 1
            lineage = _lineage(all_nodes, node_id)
            matches = [
                m for m in index.query(hypothesis, k=k, domain=domain)
                if m.score >= threshold and m.finding.session not in lineage
            ]
            if matches:
                flagged.append({
                    "node_id": node_id,
                    "hypothesis": hypothesis,
                    "matches": [
                        {
                            "node_id": m.finding.session or m.finding.source or "?",
                            "status": m.finding.status or "known",
                            "score": round(m.score, 3),
                        }
                        for m in matches
                    ],
                    "score": round(matches[0].score, 3),
                })
        if status in _GUARDED:
            index.add(Finding(
                note=hypothesis,
                domain=domain or "",
                status=status,
                session=node_id,
                source="replay",
            ))
    return {"considered": considered, "flagged": flagged, "caught": len(flagged)}


def tree_nodes_in_order(tree: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract nodes from an ``idea_tree.json`` payload in replayable order.

    Accepts both on-disk shapes: the current *envelope* format
    (``{"version": 3, "meta": {...}, "nodes": {nid: node}}``, written since
    coordinator v3) and the older flat format (nodes directly at top level).
    ``ROOT`` is dropped either way — it is the task statement, not an experiment.

    Node ids in Arbor look like "1.1", "1.2", "2.1": creation order is
    depth-first-ish but not strictly encoded. Best-effort: sort by depth then id
    lexically, which is close enough for validation. Node records are returned
    as-is.
    """
    payload = tree or {}
    if isinstance(payload.get("nodes"), dict):
        payload = payload["nodes"]
    nodes: list[dict[str, Any]] = []
    for nid, rec in payload.items():
        if nid == "ROOT" or not isinstance(rec, dict):
            continue
        node = dict(rec)
        node.setdefault("id", nid)
        node.setdefault("hypothesis", "")
        nodes.append(node)
    nodes.sort(key=lambda r: (r.get("depth", 0), str(r.get("id", ""))))
    return nodes
