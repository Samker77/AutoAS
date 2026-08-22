"""Offline smoke test for the grounded-ideation lane (ResearchSearch).

Exercises the REAL production path — user LLM config → provider →
``ResearchSearchTool.execute`` (isolated SearchAgent + alphaxiv backend +
honest-citation filter) — WITHOUT launching a full coordinator run. Use it to
verify the search function works and inspect digest quality before spending a
full run's budget.

Usage:
  python scripts/search_smoke.py [--config PROJECT/research_config.yaml] \\
      [--query "..."] [--intent survey] [--focus "prefer arxiv 2023+"] \\
      [--model OVERRIDE] [--timeout 180]

Notes
-----
- Requires the same environment a run would: an LLM endpoint in
  ``~/.arbor/config.yaml`` (provider/model/base_url/api_key).
- ``--model`` exercises ``search.agent_model`` (the cheap-model override):
  a fresh provider is built for the SearchAgent, exactly as in production.
- Real network + LLM cost: one SearchAgent run (bounded by agent_max_turns).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _build_config(yaml_path: str, model_override: str | None):
    from arbor.core.config_resolve import resolve_config
    from arbor.cli.user_config import llm_defaults

    c = resolve_config(yaml_path=yaml_path, role="coordinator")
    u = llm_defaults()
    c.llm.provider = u.get("provider") or c.llm.provider
    c.llm.model = u.get("model") or c.llm.model
    c.llm.base_url = u.get("base_url") or c.llm.base_url
    c.llm.api_key = u.get("api_key") or c.llm.api_key
    if model_override:
        c.search.agent_model = model_override  # exercises the small-model knob
    c.cwd = str(Path(yaml_path).resolve().parent)
    c.workspace_dir = str(Path(yaml_path).resolve().parent / ".arbor")
    return c


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default="D:/Agent/algotune_research/research_config.yaml")
    ap.add_argument("--query", default=(
        "Approximate nearest neighbor search at 1M scale, 128-dim, k=10: "
        "which index family (IVF-PQ, HNSW, graph, quantization) gives the best "
        "recall/latency tradeoff for exact-ish kNN?"
    ))
    ap.add_argument("--intent", default="survey")
    ap.add_argument("--focus", default="prefer arxiv 2023+")
    ap.add_argument("--model", default=None, help="override SearchAgent model (search.agent_model)")
    ap.add_argument("--timeout", type=int, default=240)
    args = ap.parse_args()

    config = _build_config(args.config, args.model)
    s = config.search
    print(f"config: {Path(args.config).name}")
    print(f"  search.enabled={s.enabled} | has_backend={s.has_backend} | "
          f"agent_model={s.agent_model or config.effective_meta_model!r} | "
          f"agent_max_turns={s.agent_max_turns} | backends={s.backends or 'legacy'}")
    if not s.enabled or not s.has_backend:
        print("ERROR: search not enabled / no backend. Aborting.", file=sys.stderr)
        return 2

    from arbor.coordinator.main import create_provider
    from arbor.coordinator.tools.research_ctx import ResearchSearchTool

    provider = create_provider(config)
    tool = ResearchSearchTool(cwd=config.cwd, config=config, provider=provider)

    print(f"query: {args.query[:120]}{'...' if len(args.query) > 120 else ''}")
    print(f"intent: {args.intent} | focus: {args.focus!r}")
    print("running SearchAgent (isolated context, alphaxiv backend)...")

    try:
        result = await asyncio.wait_for(
            tool.execute(query=args.query, intent=args.intent, focus=args.focus),
            timeout=args.timeout,
        )
    except asyncio.TimeoutError:
        print(f"ERROR: timed out after {args.timeout}s", file=sys.stderr)
        return 3

    print("\n" + "=" * 70)
    print("DIGEST")
    print("=" * 70)
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
