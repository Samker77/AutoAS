"""E1 soft warning — flag new ideas similar to already-known tree directions.

A deterministic guard at idea-add time: compare a freshly-proposed hypothesis
against the tree's known nodes (pruned / done / merged / failed / needs_retry).
If it scores at/above the threshold against any known node *outside its own
lineage*, the idea is still added but the coordinator sees a warning with
evidence — so a deliberate re-test (with a stated counter-argument) is
distinguished from an accidental re-tread of a dead end.

Warning, never a block. Similarity is advisory evidence, and the lexical scorer
can anchor on a shared bigram (see the counterfactual tests) — so the guard's
job is to make the overlap visible and auditable, not to stop exploration. An
embedding scorer can be swapped into ``MemoryIndex`` for sharper precision
later (E2).
"""

from __future__ import annotations

from ..memory_index import Finding, MemoryIndex
from .idea_tree import IdeaTree, Node

# Statuses whose outcome is known — these seed the guard.
KNOWN_STATUSES = frozenset({"pruned", "done", "merged", "failed", "needs_retry"})


def _node_text(node: Node) -> str:
    return (node.hypothesis or node.insight or "").strip()


def build_guard_index(tree: IdeaTree, *, exclude_ids: set[str]) -> MemoryIndex:
    """Index the tree's known nodes as guard entries, minus ``exclude_ids``.

    Each known node contributes its hypothesis (falling back to its insight) as
    the searchable text, tagged with its id and status so a match carries
    provenance.
    """
    index = MemoryIndex()
    for node in tree.get_all_nodes():
        if node.id in exclude_ids:
            continue
        if node.status not in KNOWN_STATUSES:
            continue
        text = _node_text(node)
        if not text:
            continue
        index.add(Finding(
            note=text,
            status=node.status,
            session=node.id,  # provenance: which node seeded the match
            domain="",
        ))
    return index


def dedup_warning(
    tree: IdeaTree,
    parent_id: str,
    hypothesis: str,
    *,
    threshold: float = 0.4,
    k: int = 3,
) -> str:
    """Return an advisory warning block if ``hypothesis`` re-treads a known node.

    ``parent_id``'s lineage (itself + ancestors) is excluded: a child
    legitimately extends its own direction — the guard flags similarity to
    *other* branches. Returns ``""`` when nothing is similar.
    """
    hypothesis = (hypothesis or "").strip()
    if not hypothesis:
        return ""
    lineage = {n.id for n in tree.get_path_to_root(parent_id)} if parent_id else set()
    index = build_guard_index(tree, exclude_ids=lineage)
    matches = [m for m in index.query(hypothesis, k=k) if m.score >= threshold]
    if not matches:
        return ""

    lines = [
        "WARNING (E1 dedup guard, advisory — idea was still added):",
        "  This idea resembles direction(s) already explored:",
    ]
    for m in matches:
        node = tree.get_node(m.finding.session)
        hyp = _node_text(node) if node else m.finding.session
        lines.append(
            f"  - {m.score:.2f} similar to node {m.finding.session} "
            f"({m.finding.status or 'known'}): \"{hyp[:70]}\"")
    lines.append(
        "  If this deliberately re-tests a failed direction, briefly state how "
        "it counters the prior lesson.")
    return "\n".join(lines)
