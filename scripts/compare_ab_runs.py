"""Compare two A/B run sessions for the search-value experiment.

Usage:
  python scripts/compare_ab_runs.py <session_off_dir> <session_on_dir>

Sessions are the coordinator workspace dirs (e.g. .../.arbor/sessions/ab_search_off
and .../.arbor/sessions/ab_search_on). Each must contain idea_tree.json,
run_stats.json (optional), and trajectory.jsonl (optional).

Prints a side-by-side comparison focused on the search-value question:
  - final dev/test trunk scores
  - exploration shape (nodes by status, scored nodes)
  - where the best idea came from / what the best solutions did
  - whether grounding / research-search was used (treatment arm)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def _load(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def summarize_session(label: str, session_dir: Path) -> None:
    print(f"\n{'=' * 64}")
    print(f"  {label}: {session_dir}")
    print(f"{'=' * 64}")
    tree = _load(session_dir / "idea_tree.json")
    if not tree:
        print("  (no idea_tree.json — run incomplete?)")
        return

    nodes = tree.get("nodes", {})
    meta = tree.get("meta", {})
    root = nodes.get(tree.get("root_id", "ROOT"), {})

    print("  Final scores:")
    for k in ("baseline_score", "trunk_score", "test_baseline_score", "test_trunk_score"):
        v = meta.get(k)
        if v is not None:
            print(f"    {k:22s} = {v:.3f}")
    if root.get("insight"):
        print(f"  Global insight: {root['insight'][:300]}")

    scored = sorted(
        (n for n in nodes.values()
         if n.get("id") != "ROOT" and n.get("score") is not None),
        key=lambda n: n.get("score", 0), reverse=True,
    )
    print(f"\n  Scored experiments ({len(scored)}):")
    for n in scored[:8]:
        g = n.get("grounding") or []
        hyp = (n.get("hypothesis") or "")[:60]
        print(f"    {n['id']:<6} score={n.get('score'):>8.2f} status={n.get('status'):<8} "
              f"groundings={len(g)}  {hyp}")

    from collections import Counter
    statuses = Counter(n.get("status", "?") for n in nodes.values() if n.get("id") != "ROOT")
    print(f"\n  Node statuses: {dict(statuses)}")
    print(f"  Nodes with grounding: "
          f"{sum(1 for n in nodes.values() if n.get('grounding'))}")

    # Research-search usage signals
    traj = _load(session_dir / "trajectory.jsonl")
    search_hits = 0
    for line in (session_dir / "trajectory.jsonl").read_text(
        encoding="utf-8", errors="replace"
    ).splitlines() if (session_dir / "trajectory.jsonl").exists() else []:
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        s = json.dumps(rec)
        for marker in ("ResearchSearch", "SearchIdeaContext", "grounded_ideation"):
            if marker in s:
                search_hits += 1
                break
    print(f"  Trajectory mentions of search tools/fields: {search_hits}")


def main() -> None:
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(2)
    off = Path(sys.argv[1])
    on = Path(sys.argv[2])
    summarize_session("A/B arm A (search OFF)", off)
    summarize_session("A/B arm B (search ON)", on)


if __name__ == "__main__":
    main()
