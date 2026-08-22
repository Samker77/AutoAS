"""Counterfactual replay validation: would the dedup guard have caught waste?

Replays a dispatch history (synthetic mirror of the AB_REPORT waste pattern:
pruned directions re-tread by later executors) and checks the guard flags the
near-duplicates while leaving genuinely new directions alone — and documents
the lexical proxy's honest limitation (it can fire on a shared bigram and anchor
on the wrong node), which is why E1 warns rather than blocks.
"""

from __future__ import annotations

from arbor.counterfactual import counterfactual_replay, tree_nodes_in_order

# Re-treads share clear terms with their failed source; 3.3 is genuinely new;
# 1.2 is a *different* successful idea that only shares "kNN".
_SCENARIO = [
    {"id": "1.1", "hypothesis": "bucketized select for kNN search", "status": "pruned"},
    {"id": "1.2", "hypothesis": "block-min tournament for kNN selection", "status": "done"},
    {"id": "2.1", "hypothesis": "homogeneous GEMM kernel", "status": "pruned"},
    {"id": "3.1", "hypothesis": "bucketized selection to speed up kNN search", "status": "pruned"},
    {"id": "3.2", "hypothesis": "homogeneous matrix multiply kernel", "status": "done"},
    {"id": "3.3", "hypothesis": "post-training quantization to shrink the model", "status": "done"},
    {"id": "4.1", "hypothesis": "bucketized variant of kNN selection", "status": "pruned"},
]

_THRESHOLD = 0.4


def test_guard_flags_retreads_not_new_directions():
    r = counterfactual_replay(_SCENARIO, threshold=_THRESHOLD)
    assert r["considered"] == 7
    assert {f["node_id"] for f in r["flagged"]} == {"3.1", "3.2", "4.1"}
    # the genuinely new direction (quantization) is not flagged
    assert all(f["node_id"] != "3.3" for f in r["flagged"])


def test_guard_does_not_flag_distinct_successful_idea():
    r = counterfactual_replay(_SCENARIO, threshold=_THRESHOLD)
    # 1.2 shares only "kNN" with the bucketized re-treads — must not trip.
    assert all(f["node_id"] != "1.2" for f in r["flagged"])


def test_clean_catches_carry_pruned_evidence():
    r = counterfactual_replay(_SCENARIO, threshold=_THRESHOLD)
    by_id = {f["node_id"]: f for f in r["flagged"]}
    # 3.1 re-treads 1.1 (pruned), 3.2 re-treads 2.1 (pruned): clean anchors.
    assert by_id["3.1"]["matches"][0]["node_id"] == "1.1"
    assert by_id["3.1"]["matches"][0]["status"] == "pruned"
    assert by_id["3.2"]["matches"][0]["node_id"] == "2.1"
    assert by_id["3.2"]["matches"][0]["status"] == "pruned"


def test_proxy_can_anchor_on_wrong_node_but_still_has_pruned_evidence():
    """Honest limitation: 4.1 is correctly flagged, but its best anchor is a
    *done* node (shared 'kNN selection' bigram), not the pruned source. The
    pruned evidence is still present in the list — so the warning can be audited
    and the coordinator can decide. This is why E1 warns rather than blocks."""
    r = counterfactual_replay(_SCENARIO, threshold=_THRESHOLD)
    f = next(x for x in r["flagged"] if x["node_id"] == "4.1")
    assert f["score"] >= _THRESHOLD
    statuses = [m["status"] for m in f["matches"]]
    assert "done" in statuses   # anchored on 1.2 (the weak reason)...
    assert "pruned" in statuses  # ...but the failed re-tread is also surfaced


def test_replay_is_deterministic():
    a = counterfactual_replay(_SCENARIO, threshold=_THRESHOLD)
    b = counterfactual_replay(_SCENARIO, threshold=_THRESHOLD)
    assert a == b


def test_no_hypothesis_nodes_skipped():
    r = counterfactual_replay([{"id": "1", "hypothesis": "", "status": "pruned"}])
    assert r["considered"] == 0 and r["caught"] == 0


def test_empty_history():
    assert counterfactual_replay([]) == {"considered": 0, "flagged": [], "caught": 0}


def test_tree_nodes_in_order_extracts_and_sorts():
    tree = {
        "ROOT": {"meta": {"baseline_score": 1.0}},
        "1.2": {"hypothesis": "b", "status": "done", "depth": 1},
        "2.1": {"hypothesis": "c", "status": "pruned", "depth": 2},
        "1.1": {"hypothesis": "a", "status": "pruned", "depth": 1},
    }
    nodes = tree_nodes_in_order(tree)
    assert [n["id"] for n in nodes] == ["1.1", "1.2", "2.1"]
    assert all("hypothesis" in n for n in nodes)


def test_tree_nodes_in_order_handles_missing_fields():
    nodes = tree_nodes_in_order({"ROOT": {}, "9.1": {"status": "done"}})
    assert len(nodes) == 1
    assert nodes[0]["hypothesis"] == ""


def test_tree_nodes_in_order_accepts_envelope_format():
    """Real on-disk trees are the envelope shape: version/meta/root_id/max_depth
    around a ``nodes`` map. The extractor must unwrap it, not treat ``meta`` and
    ``nodes`` themselves as nodes."""
    tree = {
        "version": 3,
        "meta": {"baseline_score": 1.01},
        "root_id": "ROOT",
        "max_depth": 2,
        "nodes": {
            "ROOT": {"id": "ROOT", "hypothesis": "the task", "status": "done"},
            "1": {"id": "1", "hypothesis": "direction one", "status": "pending", "depth": 1},
            "1.1": {"id": "1.1", "hypothesis": "mechanism a", "status": "pruned", "depth": 2},
            "2.1": {"id": "2.1", "hypothesis": "mechanism b", "status": "done", "depth": 2},
        },
    }
    nodes = tree_nodes_in_order(tree)
    assert [n["id"] for n in nodes] == ["1", "1.1", "2.1"]
    assert all("hypothesis" in n for n in nodes)


def test_envelope_replay_ignores_meta_and_root():
    """The full counterfactual over an envelope-shaped tree: ROOT's task text
    must not seed the guard, and the meta map must not be counted as a node."""
    tree = {
        "version": 3,
        "meta": {"baseline_score": 1.0},
        "root_id": "ROOT",
        "max_depth": 2,
        "nodes": {
            "ROOT": {"id": "ROOT", "hypothesis": "speed up kNN search", "status": "done"},
            "1.1": {"id": "1.1", "hypothesis": "bucketized select for kNN", "status": "pruned"},
            "2.1": {"id": "2.1", "hypothesis": "bucketized selection re-tread", "status": "pruned"},
        },
    }
    r = counterfactual_replay(tree_nodes_in_order(tree), threshold=_THRESHOLD)
    assert r["considered"] == 2
    assert r["caught"] == 1
    assert r["flagged"][0]["node_id"] == "2.1"
