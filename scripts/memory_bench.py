"""Direction-1 effectiveness benchmarks (Tests A/B/C) + real-tree replay CLI.

Measures the *mechanism proxies* of the semantic-memory work — the predictive
signals of its real value — on crafted scenarios (no real runs needed):

  python scripts/memory_bench.py corpus   # A: retrieval quality, old Jaccard vs new semantic recall
  python scripts/memory_bench.py guard    # B: E1 guard precision / recall
  python scripts/memory_bench.py scale    # C: E3 read-cost scaling + accumulation properties
  python scripts/memory_bench.py replay TREE.json [--threshold 0.4]
                                         # real-tree counterfactual: what E1 would have caught

Honest caveats are printed inline: the corpus/scenarios are hand-authored, so
these numbers say "the mechanism behaves as designed on realistic crafted
cases", not "it improves real runs" — the latter needs shadow-mode data on real
runs. Where the lexical scorer loses (e.g. a relevant finding with no shared
terms), the report says so: that is the gap embeddings close in E2.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from arbor.counterfactual import counterfactual_replay, tree_nodes_in_order  # noqa: E402
from arbor.global_memory import GlobalMemory  # noqa: E402
from arbor.memory_index import Finding, MemoryIndex, findings_from_session  # noqa: E402
from arbor.recall import (  # noqa: E402
    find_similar,
    find_similar_findings,
)


# ═══════════════════════════════════════════════════════════════════════════
# Test A — retrieval quality (old whole-file Jaccard vs new bullet-level recall)
# ═══════════════════════════════════════════════════════════════════════════

# (fid, kind, about, note, session)
_CORPUS = [
    # kNN — clean session
    ("f1", "leverage", "kNN indexing", "bucketized select speeds up kNN search", "knn-clean"),
    ("f3", "pitfall", "kNN precision", "kNN with L2 normalization hurts accuracy in high dimensions", "knn-clean"),
    ("f5", "pitfall", "kNN indexing", "bucketized indexing drops kNN search precision, do not combine with pruning", "knn-clean"),
    ("f6", "pitfall", "kNN neighbors", "100 neighbors is slower but far more stable than 10", "knn-clean"),
    # kNN — diluted session (relevant findings buried in database noise + a
    # "common-word trap" bullet that says kNN search but means serving latency)
    ("f2", "leverage", "kNN search", "block-min tournament speeds up kNN nearest neighbor search", "knn-diluted"),
    ("f4", "leverage", "kNN kernels", "homogeneous GEMM kernel accelerates kNN distance computation", "knn-diluted"),
    ("n1", "leverage", "databases", "row-level database indexing speeds up read queries", "knn-diluted"),
    ("n2", "leverage", "databases", "B-tree page size tuning matters more than cache for lookups", "knn-diluted"),
    ("n3", "pitfall", "databases", "thread pool sizing causes contention under load", "knn-diluted"),
    ("n4", "leverage", "databases", "covering indexes avoid row lookups entirely", "knn-diluted"),
    ("n7", "pitfall", "kNN serving", "kNN search on the web service has high latency, unrelated to accuracy", "knn-diluted"),
    # transformers — clean session
    ("f7", "leverage", "training stability", "gradient clipping mitigates exploding gradients during training", "tf-clean"),
    ("f8", "leverage", "training schedule", "warmup steps reduce early-training instability", "tf-clean"),
    ("f9", "leverage", "memory", "mixed precision halves memory but needs loss scaling", "tf-clean"),
    ("f10", "leverage", "attention", "attention dropout helps long sequences stay stable", "tf-clean"),
    ("f11", "leverage", "layer norm", "layer norm placement controls gradient stability more than init scale", "tf-clean"),
    ("f12", "pitfall", "schedule", "learning-rate schedule matters more than batch size for convergence", "tf-clean"),
    # datapipeline — clean session
    ("f13", "leverage", "dataset", "labels are noisy above index 9000, drop them", "data-clean"),
    ("f14", "leverage", "dataset split", "shuffling before the split avoids leakage", "data-clean"),
    ("f15", "pitfall", "eval metric", "class imbalance biases the eval metric, use macro average", "data-clean"),
    ("f16", "leverage", "iteration", "caching preprocessed features speeds iteration by 10x", "data-clean"),
]

_SESSION_DOMAINS = {
    "knn-clean": "knn",
    "knn-diluted": "knn",
    "tf-clean": "transformers",
    "data-clean": "datapipeline",
}

# Query -> ids of ground-truth relevant findings.
_QUERIES = {
    "exploding gradient mitigation": {"f7", "f11"},
    "speed up kNN nearest neighbor search": {"f1", "f2", "f4"},
    "handling noisy labels in the dataset": {"f13"},
    "mixed precision training to save memory": {"f9"},
    "kNN search precision tradeoffs with indexing": {"f3", "f5"},
    "database read query performance": {"n1", "n2", "n4"},
}

_ID_TO_NOTE = {fid: note for fid, _k, _a, note, _s in _CORPUS}


def _build_sessions(root: Path) -> Path:
    """Write the corpus as session EXPERIENCE.md files (what distill writes)."""
    by_session: dict[str, list] = {}
    for fid, kind, about, note, session in _CORPUS:
        by_session.setdefault(session, []).append((kind, about, note))
    for session, entries in by_session.items():
        sd = root / ".arbor" / "sessions" / session
        (sd / ".coordinator").mkdir(parents=True, exist_ok=True)
        (sd / ".coordinator" / "idea_tree.json").write_text(json.dumps(
            {"ROOT": {"meta": {"domain": _SESSION_DOMAINS[session]}}}), encoding="utf-8")
        bullets = "\n".join(
            f"- **[{kind}] {about}** — {note}" for kind, about, note in entries)
        (sd / "EXPERIENCE.md").write_text(f"# Findings\n\n{bullets}\n", encoding="utf-8")
    return root


def _relevant_notes(ids) -> set[str]:
    return {_ID_TO_NOTE[i] for i in ids if i in _ID_TO_NOTE}


def _old_surfaced(cwd: str, topic: str) -> tuple[set[str], int]:
    """What the old pipeline surfaces: all findings in the sessions its keyword
    gate recalls (this is what compose_for_topic dumps into context)."""
    hits = find_similar(cwd, topic)
    surfaced: set[str] = set()
    for h in hits:
        sd = Path(cwd) / ".arbor" / "sessions" / h["name"]
        for f in findings_from_session(sd):
            surfaced.add(f.note)
    return surfaced, len(surfaced)


def _new_surfaced(cwd: str, topic: str, k: int) -> set[str]:
    hits = find_similar_findings(cwd, topic, limit=k, threshold=0.1)
    return {h["finding"].note for h in hits}


def _metrics(surfaced: set[str], relevant: set[str]) -> tuple[float, float]:
    total = len(relevant)
    recall = len(surfaced & relevant) / total if total else 0.0
    precision = len(surfaced & relevant) / len(surfaced) if surfaced else 0.0
    return recall, precision


def cmd_corpus(_args) -> int:
    with tempfile.TemporaryDirectory() as td:
        root = _build_sessions(Path(td))
        cwd = str(root)

        print("=" * 78)
        print("Test A — retrieval quality: old whole-file Jaccard vs new bullet-level recall")
        print("=" * 78)
        print(f"{'query':<45} {'old R':>6} {'old P':>6} {'old#':>5} | {'new R@5':>8} {'new P@5':>8}")
        agg_old_r = agg_old_p = agg_new_r = agg_new_p = 0.0
        n = 0
        for topic, relevant_ids in _QUERIES.items():
            relevant = _relevant_notes(relevant_ids)
            old_surf, old_n = _old_surfaced(cwd, topic)
            new_surf = _new_surfaced(cwd, topic, k=5)
            old_r, old_p = _metrics(old_surf, relevant)
            new_r, new_p = _metrics(new_surf, relevant)
            # MRR for the new path: 1/rank of the first relevant finding.
            hits = find_similar_findings(cwd, topic, limit=10, threshold=0.0)
            mrr = 0.0
            for rank, h in enumerate(hits, start=1):
                if h["finding"].note in relevant:
                    mrr = 1.0 / rank
                    break
            print(f"{topic[:43]:<45} {old_r:6.2f} {old_p:6.2f} {old_n:5d} | {new_r:8.2f} {new_p:8.2f}   mrr={mrr:.2f}")
            agg_old_r += old_r; agg_old_p += old_p
            agg_new_r += new_r; agg_new_p += new_p
            n += 1
        print("-" * 78)
        print(f"{'macro average':<45} {agg_old_r/n:6.2f} {agg_old_p/n:6.2f} {'':5} | {agg_new_r/n:8.2f} {agg_new_p/n:8.2f}")
        print()
        print("old# = bullets the old pipeline drags into context per query (the noise cost).")
        print("old recalls whole sessions (high R, low P, lots of noise); new recalls findings")
        print("directly (high P, R capped at k). Losses (e.g. f4 'GEMM accelerates kNN distance'")
        print("has no shared terms with the query) are the gap embeddings close in E2.")
    return 0


# ═══════════════════════════════════════════════════════════════════════════
# Test B — E1 guard precision / recall on scripted tree scenarios
# ═══════════════════════════════════════════════════════════════════════════

# Each scenario: nodes in dispatch order + ids we author as TRUE re-treads.
_GUARD_SCENARIOS = [
    {
        "name": "AB-style re-treads",
        "nodes": [
            {"id": "1.1", "hypothesis": "bucketized select for kNN search", "status": "pruned"},
            {"id": "1.2", "hypothesis": "block-min tournament for kNN selection", "status": "done"},
            {"id": "2.1", "hypothesis": "homogeneous GEMM kernel", "status": "pruned"},
            {"id": "3.1", "hypothesis": "bucketized selection to speed up kNN search", "status": "pruned"},
            {"id": "3.2", "hypothesis": "homogeneous matrix multiply kernel", "status": "done"},
            {"id": "3.3", "hypothesis": "post-training quantization to shrink the model", "status": "done"},
            {"id": "4.1", "hypothesis": "bucketized variant of kNN selection", "status": "pruned"},
        ],
        "true_retreads": {"3.1", "3.2", "4.1"},
    },
    {
        "name": "lineage-heavy (children resemble parents)",
        "nodes": [
            {"id": "1", "hypothesis": "faster kNN search via indexing", "status": "pruned"},
            {"id": "1.1", "hypothesis": "bucketized select for kNN search", "status": "pruned"},
            {"id": "1.1.1", "hypothesis": "bucketized select with caching for kNN search", "status": "done"},
            {"id": "2", "hypothesis": "gradient clipping for stable training", "status": "done"},
            {"id": "2.1", "hypothesis": "adaptive gradient clipping", "status": "done"},
            # cross-branch re-tread: 3.1 re-treads branch 1's pruned idea
            {"id": "3.1", "hypothesis": "bucketized select variant for kNN search", "status": "pruned"},
        ],
        "true_retreads": {"3.1"},
    },
    {
        "name": "cross-domain (nothing similar)",
        "nodes": [
            {"id": "1", "hypothesis": "bucketized select for kNN search", "status": "pruned"},
            {"id": "2", "hypothesis": "gradient clipping mitigates exploding gradients", "status": "pruned"},
            {"id": "3", "hypothesis": "labels noisy above index 9000", "status": "done"},
            {"id": "4", "hypothesis": "quantization to shrink the model", "status": "done"},
        ],
        "true_retreads": set(),
    },
    {
        "name": "shared-bigram near-miss (new direction)",
        "nodes": [
            {"id": "1.1", "hypothesis": "bucketized select for kNN search", "status": "pruned"},
            {"id": "1.2", "hypothesis": "post-training quantization to shrink the model", "status": "done"},
            # genuinely new concern that shares only the "kNN search" bigram
            {"id": "2.1", "hypothesis": "kNN search on the web service has high latency", "status": "done"},
        ],
        "true_retreads": set(),
    },
]


def cmd_guard(_args) -> int:
    print("=" * 78)
    print("Test B — E1 dedup-guard precision / recall on scripted scenarios")
    print("=" * 78)
    print(f"{'scenario':<38} {'flagged':>8} {'correct':>8} {'missed':>8} {'prec':>6} {'recall':>6}")
    tp = fp = fn = 0
    for sc in _GUARD_SCENARIOS:
        report = counterfactual_replay(sc["nodes"], threshold=0.4)
        flagged = {f["node_id"] for f in report["flagged"]}
        true_r = sc["true_retreads"]
        hit = flagged & true_r
        tp += len(hit); fp += len(flagged - true_r); fn += len(true_r - flagged)
        prec = len(hit) / len(flagged) if flagged else 1.0
        rec = len(hit) / len(true_r) if true_r else (1.0 if not flagged else 0.0)
        print(f"{sc['name']:<38} {len(flagged):>8} {len(hit):>8} {len(true_r - flagged):>8} "
              f"{prec:>6.2f} {rec:>6.2f}")
    print("-" * 78)
    print(f"{'TOTAL':<38} {tp+fp:>8} {tp:>8} {fn:>8} {tp/(tp+fp) if tp+fp else 1.0:>6.2f} "
          f"{tp/(tp+fn) if tp+fn else 1.0:>6.2f}")
    print()
    print("Precision = of the warnings fired, how many are true re-treads (do warnings")
    print("deserve trust?). Recall = of the re-treads, how many got warned. 'missed' on the")
    print("lineage scenario = deliberate in-lineage iteration the guard (correctly) ignores.")
    return 0


# ═══════════════════════════════════════════════════════════════════════════
# Test C — E3 read-cost scaling + accumulation properties
# ═══════════════════════════════════════════════════════════════════════════

def _gen_sessions(root: Path, n: int, per_session: int = 8) -> None:
    for i in range(n):
        sd = root / ".arbor" / "sessions" / f"run-{i}"
        (sd / ".coordinator").mkdir(parents=True, exist_ok=True)
        bullets = "\n".join(
            f"- **[leverage] topic{i}** — {topic} finding number {j} for run {i}"
            for j, topic in enumerate(["gradient clipping", "block-min tournament",
                                       "database indexing", "noisy labels",
                                       "mixed precision", "warmup steps",
                                       "attention dropout", "caching features"]))
        (sd / "EXPERIENCE.md").write_text(f"# Findings\n\n{bullets}\n", encoding="utf-8")


def _time(fn, iters: int = 3):
    best = float("inf")
    for _ in range(iters):
        t0 = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - t0)
    return best


def cmd_scale(_args) -> int:
    print("=" * 78)
    print("Test C — E3 read cost: scan-rebuild vs persistent-index load")
    print("=" * 78)
    print(f"{'sessions':>8} {'scan(ms)':>9} {'load(ms)':>9} {'speedup':>8}")
    for n in (10, 50, 100):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _gen_sessions(root, n)

            def scan():
                index = MemoryIndex()
                for sd in (root / ".arbor" / "sessions").iterdir():
                    index.extend(findings_from_session(sd))

            gm = GlobalMemory.at_cwd(str(root))
            sd_dirs = list((root / ".arbor" / "sessions").iterdir())
            gm.rebuild(sd_dirs)
            scan_ms = _time(scan) * 1000
            load_ms = _time(gm.load) * 1000
            speedup = scan_ms / load_ms if load_ms else float("inf")
            print(f"{n:>8} {scan_ms:>9.1f} {load_ms:>9.1f} {speedup:>7.1f}x")
    print()
    print("scan = parse every session's markdown + tree json; load = one JSONL file.")
    print("Timings are relative (min-of-3 on a dev box), not absolute.")
    print()
    print("Accumulation properties:")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        gm = GlobalMemory.at_cwd(str(root))
        for i in range(10):  # 10 simulated runs, each appending the same lesson
            gm.append([Finding(note=f"shared lesson {i % 3}", session=f"run-{i}")])
        print(f"  after 10 runs appending lessons (3 distinct across runs): "
              f"index has {len(gm)} records (expected 10: per-run dedup, cross-run kept)")
        before = len(gm)
        assert gm.append([Finding(note="shared lesson 1", session="run-1")]) == 0
        print(f"  re-appending the same (session, lesson) adds 0 -> idempotent "
              f"({len(gm) - before} new)")
    return 0


# ═══════════════════════════════════════════════════════════════════════════
# Real-tree counterfactual replay (for the A/B runs when their trees are found)
# ═══════════════════════════════════════════════════════════════════════════

def cmd_replay(args) -> int:
    tree = json.loads(Path(args.tree).read_text(encoding="utf-8"))
    nodes = tree_nodes_in_order(tree)
    report = counterfactual_replay(nodes, threshold=args.threshold)
    print(f"Tree: {args.tree}")
    print(f"Dispatched nodes considered: {report['considered']}")
    print(f"E1 guard (threshold={args.threshold}) would have flagged "
          f"{report['caught']} of them as re-treads of known directions:\n")
    for f in report["flagged"]:
        top = f["matches"][0]
        print(f"  {f['node_id']:>6}  {f['score']:.2f} similar to "
              f"{top['node_id']} ({top['status']}): {f['hypothesis'][:60]}")
        for m in f["matches"][1:]:
            print(f"          also similar to {m['node_id']} ({m['status']}, {m['score']:.2f})")
    if not report["flagged"]:
        print("  (no dispatches flagged — either no known nodes or no re-treads)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("corpus", help="Test A: retrieval quality, old vs new")
    sub.add_parser("guard", help="Test B: E1 guard precision/recall")
    sub.add_parser("scale", help="Test C: E3 read-cost scaling + properties")

    rep = sub.add_parser("replay", help="Real-tree counterfactual replay")
    rep.add_argument("tree", type=str, help="path to an idea_tree.json")
    rep.add_argument("--threshold", type=float, default=0.4)
    args = parser.parse_args()

    cmds = {"corpus": cmd_corpus, "guard": cmd_guard, "scale": cmd_scale, "replay": cmd_replay}
    return cmds[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
