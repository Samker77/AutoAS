"""Core Agent — ReAct loop with tool execution, context compression, and experiment tracking."""

# pylint: disable=broad-exception-caught,protected-access

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import sys
import time
from contextlib import asynccontextmanager
from typing import Any

from .config import AgentConfig
from .context import ContextManager
from .experiment import ExperimentTracker, GitManager
from .llm.base import LLMProvider, LLMResponse, ThinkingBlock, ToolCall, ToolResultBlock
from .tools.base import Tool

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Executor convergence — commit classification
# ---------------------------------------------------------------------------

#: Substrings that mark an auto-committed path as a scratch / experiment
#: artifact rather than the executor's real implementation deliverable.
_SCRATCH_HINTS = (
    "scratch",
    "/tmp/",
    "\\tmp\\",
    "/logs/",
    "\\logs\\",
    "results/",
    "results\\",
    "experiments/",
    "cache/",
    "_ab_",
    "_probe_",
    "_duel_",
    "_breakdown_",
    "_cmp_",
    "backup",
    "/notes",
    "_trunk_",
)

#: File extensions that count as code (the executor's real deliverable).
_CODE_EXT_RE = re.compile(
    r"[\w.\-]+\.(?:py|js|ts|jsx|tsx|rs|go|c|cc|cpp|h|hpp|java|rb|sh|bash|sql|ipynb)\b"
)

#: Best-effort eval-score extraction from tool output (``bash eval.sh`` prints
#: ``score: 44.1``; RunTraining may print ``accuracy: 0.9``). Feeds the
#: convergence score-gate so only a commit that accompanies a *score
#: improvement* resets the idle clock.
_EVAL_SCORE_RE = re.compile(r"\b(?:score|accuracy)\s*[:=]\s*([0-9]*\.?[0-9]+)")


def _commit_is_code(file_path: str) -> bool:
    """Classify an auto-commit path as a *meaningful* code commit.

    The executor's deliverable is a working implementation in the target
    codebase; scratch A/B scripts (``_ab_scratch.py``), probe scripts, and
    ``results/`` artifacts are noise that must not reset the convergence
    idle-commit clock. ``file_path`` is the Write/Edit path or the Bash command
    string stored in the auto-commit message — best effort, conservative: a
    false "code" just keeps convergence pressure on; a false "scratch" only
    delays a nudge by a turn.
    """
    if not file_path:
        return False
    low = file_path.lower()
    if any(hint in low for hint in _SCRATCH_HINTS):
        return False
    m = _CODE_EXT_RE.search(low)
    if not m:
        return False
    name = re.split(r"[\\/]", m.group(0))[-1]
    if name.startswith(("_", ".")):
        return False
    return True


# Best-effort secret scrubbing for event previews — args/output flow to
# events.jsonl and the WebUI, so a Bash command like `export API_KEY=sk-…` or a
# printed `.env` must not leak (contract: previews are secret-free).
_SECRET_TOKEN_RE = re.compile(
    r"(?i)(?:sk-[a-z0-9._\-]{6,}|gh[pousr]_[a-z0-9]{16,}|bearer\s+[a-z0-9._\-]{8,})"
)
_SECRET_KV_RE = re.compile(
    r"(?i)(api[_-]?key|secret|password|passwd|token|auth[a-z_]*)"
    r"(['\"]?\s*[:=]\s*['\"]?)([^\s'\",]+)"
)


def _scrub_secrets(text: str) -> str:
    text = _SECRET_TOKEN_RE.sub("***", text)
    return _SECRET_KV_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}***", text)


def _preview(value: Any, limit: int = 200) -> str:
    """Short, whitespace-collapsed, secret-scrubbed, JSON-safe preview."""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except Exception:
            text = str(value)
    text = _scrub_secrets(" ".join(text.split()))
    return text if len(text) <= limit else text[:limit] + "…"


class AgentStats:
    """Process-wide aggregator of LLM usage across every Agent instance.

    Class-level counters survive the entire Python process, so coordinator +
    executors + search-agents all contribute to the same totals. Read via
    :meth:`snapshot` at the end of a run.
    """

    total_input_tokens: int = 0
    total_uncached_input_tokens: int = 0
    total_cache_read_tokens: int = 0
    total_cache_creation_tokens: int = 0
    total_output_tokens: int = 0
    total_llm_calls: int = 0
    total_agents_spawned: int = 0

    @classmethod
    def reset(cls) -> None:
        cls.total_input_tokens = 0
        cls.total_uncached_input_tokens = 0
        cls.total_cache_read_tokens = 0
        cls.total_cache_creation_tokens = 0
        cls.total_output_tokens = 0
        cls.total_llm_calls = 0
        cls.total_agents_spawned = 0

    @classmethod
    def record_agent(cls) -> None:
        cls.total_agents_spawned += 1

    @classmethod
    def record_call(
        cls,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int = 0,
        cache_creation_tokens: int = 0,
    ) -> None:
        uncached = int(input_tokens or 0)
        cache_read = int(cache_read_tokens or 0)
        cache_creation = int(cache_creation_tokens or 0)
        cls.total_uncached_input_tokens += uncached
        cls.total_cache_read_tokens += cache_read
        cls.total_cache_creation_tokens += cache_creation
        cls.total_input_tokens += uncached + cache_read + cache_creation
        cls.total_output_tokens += int(output_tokens or 0)
        cls.total_llm_calls += 1

    @classmethod
    def snapshot(cls) -> dict[str, int]:
        return {
            "total_input_tokens": cls.total_input_tokens,
            "total_uncached_input_tokens": cls.total_uncached_input_tokens,
            "total_cache_read_tokens": cls.total_cache_read_tokens,
            "total_cache_creation_tokens": cls.total_cache_creation_tokens,
            "total_output_tokens": cls.total_output_tokens,
            "total_tokens": cls.total_input_tokens + cls.total_output_tokens,
            "total_llm_calls": cls.total_llm_calls,
            "total_agents_spawned": cls.total_agents_spawned,
        }

    @classmethod
    def cache_hit_rate(cls) -> float:
        """Fraction of logical input tokens served from cache (#13).

        0.0 when nothing has been processed yet. ``total_input_tokens`` already
        includes cache reads/writes, so this is read / total.
        """
        total = cls.total_input_tokens
        return (cls.total_cache_read_tokens / total) if total else 0.0


def record_llm_usage(
    response: LLMResponse,
    *,
    bus: Any | None = None,
    provider: str | None = None,
    model: str | None = None,
    source: str | None = None,
    turn: int | None = None,
    agent_cwd: str | None = None,
    track_stats: bool = True,
) -> tuple[int, int]:
    """Record a provider response in the process-wide usage counter.

    Returns ``(total_input_tokens, output_tokens)`` using the same logical
    input-token definition as ``Agent`` instances. When ``track_stats`` is
    False the AgentStats counter is left untouched (read-only side agents).
    """
    usage = response.usage
    input_tokens = usage.total_input_tokens
    output_tokens = int(usage.output_tokens or 0)
    if track_stats:
        AgentStats.record_call(
            usage.input_tokens,
            output_tokens,
            usage.cache_read_tokens,
            usage.cache_creation_tokens,
        )
    if bus is not None:
        from ..events.types import LLM_CALL, CACHE_STAT  # local import keeps events optional
        payload: dict[str, Any] = {
            "input_tokens": input_tokens,
            "uncached_input_tokens": int(usage.input_tokens or 0),
            "cache_read_tokens": int(usage.cache_read_tokens or 0),
            "cache_creation_tokens": int(usage.cache_creation_tokens or 0),
            "output_tokens": output_tokens,
        }
        if provider is not None:
            payload["provider"] = provider
        payload["model"] = model or response.model
        if source is not None:
            payload["source"] = source
        if turn is not None:
            payload["turn"] = turn
        if agent_cwd is not None:
            payload["agent_cwd"] = agent_cwd
        try:
            bus.emit(LLM_CALL, payload)
        except Exception:  # pylint: disable=broad-exception-caught
            pass
        # Aggregate cache view for the dashboard / #13 (contract 2 CACHE_STAT).
        try:
            bus.emit(CACHE_STAT, {
                "cache_read": int(usage.cache_read_tokens or 0),
                "cache_write": int(usage.cache_creation_tokens or 0),
                "miss": int(usage.input_tokens or 0),
                "total": input_tokens,
            })
        except Exception:  # pylint: disable=broad-exception-caught
            pass
    return input_tokens, output_tokens


class Agent:
    """ReAct agent that iteratively calls an LLM, executes tools, and feeds results back.

    Mirrors the core loop of Claude Code (QueryEngine → queryLoop) but in a
    simplified, research-focused Python implementation.
    """

    def __init__(
        self,
        *,
        provider: LLMProvider,
        tools: list[Tool],
        system_prompt: str,
        config: AgentConfig,
    ):
        self.provider = provider
        self.tools: dict[str, Tool] = {t.name: t for t in tools}
        self.system_prompt = system_prompt
        self.config = config
        self.messages: list[dict[str, Any]] = []
        # Normalized, provider-agnostic transcript of what the model produced,
        # so callers can recover the last good output even when run() returns a
        # placeholder (max_turns) or is cancelled by a timeout. `messages`
        # stores provider-specific raw_content; these two store the normalized
        # views (plain text and {name,input} tool calls).
        self.assistant_texts: list[str] = []
        self.tool_uses: list[dict[str, Any]] = []
        self.suppressed_tool_uses: list[dict[str, Any]] = []
        self.total_turns = 0
        # Why the loop exited, surfaced to callers alongside total_turns:
        #   "finished"  — model produced a final answer with no tool calls
        #   "awaiting_user" — interactive text yielded control to the frontend
        #   "max_turns" — exhausted the turn budget without a final answer
        # Stays None if the run was cancelled mid-loop (e.g. timeout), since the
        # coroutine never reaches a return. Callers detect that case otherwise.
        self.stop_reason: str | None = None
        self.total_input_tokens = 0
        self.total_uncached_input_tokens = 0
        self.total_cache_read_tokens = 0
        self.total_cache_creation_tokens = 0
        self.total_output_tokens = 0
        self._max_tokens_recovery_count = 0

        # ── Executor convergence state ─────────────────────────────────
        # Tracks how long the agent has gone without committing a "meaningful"
        # code change (see _record_code_commit), so the loop can nudge an
        # executor to finalize instead of drifting into side-experiments.
        self._has_code_commit = False
        self._last_code_commit_turn = 0
        self._finalize_nudges = 0
        self._horizon_reminded = False

        # v2 — eval-score gating. Once any eval score is observed, a code
        # commit resets the idle clock ONLY when it accompanies a score
        # improvement (see _record_code_commit / _record_eval_scores). A
        # node-3.1-style executor that commits every few turns without
        # improving no longer resets the clock.
        self._best_eval_score = 0.0
        self._scores_observed = False
        self._score_at_last_reset = 0.0

        if config.track_stats:
            AgentStats.record_agent()

        self.context_manager = ContextManager(
            provider=provider,
            context_window=config.context_window,
            compact_threshold=config.compact_threshold,
            keep_recent=config.compact_keep_recent,
            event_bus=config.event_bus,
            provider_name=config.provider,
            agent_cwd=config.cwd,
        )
        self.experiment_tracker = ExperimentTracker(log_dir=config.agent_dir)
        self.git_manager = GitManager(
            cwd=config.cwd,
            branch_prefix=config.git_branch_prefix,
            enabled=config.auto_git,
            idea=config.idea,
        )

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    async def run(self, user_message: str) -> str:
        """Run the agent loop until it finishes or hits max turns.

        Returns the final text response from the LLM.

        ``user_message`` is appended as a fresh user turn. Pass an empty string
        to continue from a pre-seeded ``self.messages`` (e.g. a resumed run that
        already ends on a user turn) without breaking user/assistant alternation.
        """
        try:
            return await self._run_loop(user_message)
        finally:
            await self._aclose_tools()

    async def _aclose_tools(self) -> None:
        """Release tool-owned background resources when the agent finishes.

        Fire-and-forget Bash jobs (#8) outlive a single tool call by design, but
        must not outlive the agent — otherwise the event loop GCs pending tasks
        ("Task was destroyed but it is pending") and leaves orphan subprocesses.
        """
        for tool in self.tools.values():
            try:
                await tool.aclose()
            except Exception:
                pass

    async def _run_loop(self, user_message: str) -> str:
        if user_message:
            self.messages.append({"role": "user", "content": user_message})
            _print_user(user_message)
        no_tool_nudges = 0

        for turn in range(1, self.config.max_turns + 1):
            self.total_turns = turn

            # 0. Drain any user messages queued by an outer UI (e.g. the
            # live dashboard's stdin reader). They become fresh user
            # turns the model sees on the next LLM call.
            if self.config.inter_turn_user_messages is not None:
                try:
                    extra = self.config.inter_turn_user_messages() or []
                except Exception:
                    extra = []
                for note in extra:
                    self.messages.append({
                        "role": "user",
                        "content": f"[user note] {note}",
                    })

            # 0b. Deliver any finished background work the tools pushed, so the
            # model never has to poll for it.
            for tool in self.tools.values():
                try:
                    notes = tool.drain_notifications()
                except Exception:
                    notes = []
                for note in notes:
                    self.messages.append({"role": "user", "content": note})

            # 1. Context compression if needed
            self.messages = await self.context_manager.maybe_compact(
                self.messages, self.system_prompt
            )

            # 1b. Persist a resume checkpoint at this clean turn boundary —
            # self.messages ends on a user/tool_result message here (#1).
            if self.config.checkpoint_hook is not None:
                try:
                    self.config.checkpoint_hook(self.messages, turn)
                except Exception:
                    pass

            # 2. Call LLM (heartbeat keeps observers alive during a long call)
            _print_status(f"Turn {turn}: calling {self.provider.model}...")
            t0 = time.monotonic()

            async with self._heartbeat("llm"):
                response = await self._call_llm_with_recovery(turn)
            if response is None:
                return "Error: LLM call failed after all recovery attempts."

            elapsed = time.monotonic() - t0
            usage = response.usage
            input_tokens = usage.total_input_tokens
            output_tokens = int(usage.output_tokens or 0)
            self.total_input_tokens += input_tokens
            self.total_uncached_input_tokens += int(usage.input_tokens or 0)
            self.total_cache_read_tokens += int(usage.cache_read_tokens or 0)
            self.total_cache_creation_tokens += int(usage.cache_creation_tokens or 0)
            self.total_output_tokens += output_tokens
            record_llm_usage(
                response,
                bus=self.config.event_bus,
                provider=self.config.provider,
                model=self.provider.model,
                source="agent",
                turn=turn,
                agent_cwd=self.config.cwd,
                track_stats=self.config.track_stats,
            )
            if self.config.token_trace_path:
                from ..trajectory import append_token_record
                append_token_record(
                    self.config.token_trace_path,
                    messages=self.messages,
                    response=response,
                    turn=turn,
                    model=self.provider.model,
                )

            # 3. Append assistant message
            self.messages.append({"role": "assistant", "content": response.raw_content})

            # Stream the model's reasoning to observers (contract 2). The loop
            # is non-streaming, so each thinking block is emitted whole.
            for block in response.content:
                if isinstance(block, ThinkingBlock) and block.text:
                    self._emit_event("llm.thinking_delta", {
                        "node_id": self.config.node_id,
                        "text": block.text,
                        "agent": self.config.agent_label,
                    })

            # 4. Handle max_tokens cutoff — retry with recovery message
            if response.stop_reason == "max_tokens":
                _print_status("Output was cut off (max_tokens). Sending recovery prompt...")
                self._max_tokens_recovery_count += 1
                if self._max_tokens_recovery_count <= 3:
                    self.messages.append({
                        "role": "user",
                        "content": (
                            "Your output was cut off due to the token limit. "
                            "Please continue exactly where you left off. "
                            "Do not apologize or repeat — just resume."
                        ),
                    })
                    continue  # Loop back to call LLM again
                else:
                    _print_status("Max tokens recovery limit reached (3 attempts).")

            # Reset recovery counter on successful non-truncated response
            if response.stop_reason != "max_tokens":
                self._max_tokens_recovery_count = 0

            text = response.get_text()
            tool_calls = response.get_tool_calls()

            # Interactive frontends use visible text as the explicit boundary
            # between agent work and the human's next turn. A model may return
            # text and tool calls together; executing those calls after showing
            # a question makes the UI look idle while the agent keeps acting.
            # Keep the prose/reasoning in history, remove the unexecuted calls
            # so every persisted tool call remains paired with a result, and
            # yield control immediately. Autonomous agents retain the original
            # text+tool behavior because this option defaults to False.
            if self.config.yield_on_text and text.strip():
                if tool_calls:
                    self.suppressed_tool_uses.extend(
                        {"name": call.name, "input": call.input}
                        for call in tool_calls
                    )
                    self.messages[-1] = {
                        "role": "assistant",
                        "content": _without_tool_calls(response.raw_content),
                    }
                    _print_status(
                        "Visible assistant text ended the interactive turn; "
                        f"discarded {len(tool_calls)} accompanying tool call(s)."
                    )
                _print_assistant(text)
                self.assistant_texts.append(text)
                self.stop_reason = "awaiting_user"
                return text

            # 5. Print assistant text (if any)
            if text:
                _print_assistant(text)
                self.assistant_texts.append(text)

            # 6. Check tool calls
            if not tool_calls:
                if (
                    self.config.premature_stop_nudges
                    and
                    no_tool_nudges < 3
                    and turn < self.config.max_turns
                    and _looks_like_premature_no_tool_stop(text)
                ):
                    no_tool_nudges += 1
                    _print_status(
                        "Assistant stopped without tool calls while describing unfinished work; nudging to continue."
                    )
                    self.messages.append({
                        "role": "user",
                        "_internal": "premature_stop_nudge",
                        "content": (
                            "Your previous response described future work but did not call any tools. "
                            "Continue now by calling the appropriate tool(s). Do not report completion "
                            "until you have executed the next concrete step and have evidence from the tool output."
                        ),
                    })
                    continue
                # Diagnose why we're exiting so the user can tell a real
                # completion from a heuristic miss.
                if no_tool_nudges >= 3:
                    reason = "nudge limit (3) reached — agent kept describing work without tool calls"
                elif _looks_like_premature_no_tool_stop(text):
                    reason = "premature-stop heuristic matched but turn cap blocked nudge"
                else:
                    reason = "no future-intent markers — treating as genuine completion"
                _print_status(
                    f"Done after {turn} turns ({reason}). "
                    f"Tokens: {self.total_input_tokens} in / {self.total_output_tokens} out"
                )
                self.stop_reason = "finished"
                return text

            no_tool_nudges = 0

            # A frontend control tool changes interaction state and must be the
            # only action in its response. Reject the entire batch rather than
            # executing ordinary tools before/alongside a staged launch.
            if self.config.yield_on_text:
                control_calls = [
                    call
                    for call in tool_calls
                    if getattr(self.tools.get(call.name), "yield_after_execute", False)
                ]
                if control_calls and len(tool_calls) != 1:
                    self.suppressed_tool_uses.extend(
                        {"name": call.name, "input": call.input}
                        for call in tool_calls
                    )
                    self.messages.append({
                        "role": "user",
                        "content": [
                            ToolResultBlock(
                                tool_use_id=call.id,
                                content=(
                                    "Error: interactive control tools must be "
                                    "called alone; no tools in this batch were executed."
                                ),
                                is_error=True,
                            ).to_content_block()
                            for call in tool_calls
                        ],
                    })
                    continue

            for _tc in tool_calls:
                self.tool_uses.append({"name": _tc.name, "input": _tc.input})

            # 6. Execute tools
            _print_status(
                f"Turn {turn}: executing {len(tool_calls)} tool(s) "
                f"({elapsed:.1f}s LLM call)"
            )
            tool_names = ",".join(tc.name for tc in tool_calls)
            async with self._heartbeat(f"tool:{tool_names}"):
                results = await self._execute_tools(tool_calls)

            # 7. Append tool results as user message
            self.messages.append({
                "role": "user",
                "content": [r.to_content_block() for r in results],
            })
            # Executor convergence v2: feed eval scores to the score-gate so a
            # productive commit (score improved) resets the idle clock before
            # the step-8 convergence check. No-op unless converge_after_commit.
            self._record_eval_scores(results)
            result_by_id = {result.tool_use_id: result for result in results}
            if self.config.yield_on_text and any(
                tool is not None
                and (result := result_by_id.get(call.id)) is not None
                and tool.should_yield_after_execute(result.content)
                for call in tool_calls
                if (tool := self.tools.get(call.name)) is not None
            ):
                _print_status(
                    "Interactive control tool completed; yielding to the frontend."
                )
                self.stop_reason = "awaiting_user"
                return ""

            # 8. Executor convergence. Two triggers:
            #    * idle — real code was committed and then converge_idle_turns
            #      turns passed with no further code commit (drifting in A/B
            #      side-experiments). Escalates: nudge -> after
            #      converge_max_nudges ignored nudges, stop early so the
            #      coordinator measures the committed branch.
            #    * horizon — within converge_horizon turns of the cap. Emits a
            #      SINGLE soft reminder only; never hard-stops (a still-active
            #      executor that keeps committing code near the end must not be
            #      cut off prematurely).
            # Both are gated to executors (converge_after_commit); the
            # coordinator itself never converges early.
            if self.config.converge_after_commit:
                idle = self._convergence_idle()
                horizon = self._convergence_horizon()
                if idle:
                    if self._finalize_nudges < self.config.converge_max_nudges:
                        self._finalize_nudges += 1
                        self.messages.append({
                            "role": "user",
                            "_internal": "converge_finalize",
                            "content": (
                                "FINALIZE REQUEST: You have committed a working "
                                "implementation and have not changed your code in "
                                f"{self.total_turns - self._last_code_commit_turn} "
                                f"turns (turn {self.total_turns}/"
                                f"{self.config.max_turns}). Stop starting new "
                                "experiments. If the committed implementation is "
                                "your best result, write your FINAL REPORT now "
                                "(idea, changes, baseline vs result, analysis). "
                                "The coordinator measures the committed branch "
                                "itself — you do not need to prove more."
                            ),
                        })
                        _print_status(
                            f"Convergence nudge {self._finalize_nudges}/"
                            f"{self.config.converge_max_nudges} — asking executor to finalize"
                        )
                    else:
                        _print_status(
                            f"Executor ignored {self.config.converge_max_nudges} finalize "
                            "nudges; stopping early (converged) so the coordinator can "
                            "measure the committed branch."
                        )
                        self.stop_reason = "converged"
                        last_text = (
                            self.assistant_texts[-1]
                            if self.assistant_texts else ""
                        )
                        return last_text or (
                            f"[Executor converged: stopped early at turn {self.total_turns} "
                            f"with committed code but no final report. Coordinators should "
                            f"measure {self.config.git_branch_prefix} directly.]"
                        )
                elif horizon and not self._horizon_reminded:
                    self._horizon_reminded = True
                    self.messages.append({
                        "role": "user",
                        "_internal": "converge_horizon",
                        "content": (
                            f"You are near the turn cap (turn {self.total_turns}/"
                            f"{self.config.max_turns}). If your implementation is "
                            "committed and evaluated, write your FINAL REPORT now "
                            "rather than starting new experiments."
                        ),
                    })
                    _print_status(
                        "Convergence horizon reminder — near turn cap, asking executor to finalize"
                    )

        _print_status(f"Reached max turns ({self.config.max_turns}).")
        self.stop_reason = "max_turns"
        return f"Agent stopped after {self.config.max_turns} turns without a final answer."

    # ------------------------------------------------------------------
    # Executor convergence helpers
    # ------------------------------------------------------------------

    def _record_code_commit(self, tool: "Tool | None", file_path: str) -> None:
        """Note when an auto-commit touched a *meaningful* code file.

        Scratch / experiment artifacts (``_probe_*.py``, ``results/``, …) are
        excluded so that an executor running A/B side-experiments (which keep
        auto-committing scratch files) does not reset the idle-commit clock.

        v2 (score-gated): once an eval score has been observed, a *subsequent*
        code commit resets the idle clock ONLY when it accompanies an eval-score
        improvement (see :meth:`_record_eval_scores`). A commit without a score
        bump — the node-3.1 pattern of committing ``solution.py`` every few
        turns while running scratch A/B experiments — must not reset the clock.
        Before any score is observed we keep v1 behavior (any code commit
        resets), because without a score we cannot tell productive commits from
        drift, and cutting off a mid-verification executor would be worse.
        """
        if not _commit_is_code(file_path):
            return
        if not self._has_code_commit:
            # First meaningful code commit — always counts as productive (the
            # executor is establishing its baseline implementation).
            self._has_code_commit = True
            self._last_code_commit_turn = self.total_turns
        elif not self._scores_observed:
            # No eval score yet — cannot measure improvement; keep v1 behavior.
            self._last_code_commit_turn = self.total_turns
        # else: score-gated — only _record_eval_scores resets the clock now.

    def _convergence_idle(self) -> bool:
        """True when real code was committed and ``converge_idle_turns`` turns
        have passed with no further code commit — the executor is drifting in
        side-experiments. This is the trigger that escalates to a hard stop.
        """
        if not self.config.converge_after_commit or not self._has_code_commit:
            return False
        return (
            self.total_turns >= max(6, self.config.converge_idle_turns)
            and (self.total_turns - self._last_code_commit_turn)
            >= self.config.converge_idle_turns
        )

    def _convergence_horizon(self) -> bool:
        """True within ``converge_horizon`` turns of the hard cap.

        Only produces a single *soft* reminder — never a hard stop — so an
        executor that is still committing real code near the end (e.g. a
        promising deep exploration) is not cut off prematurely.
        """
        if not self.config.converge_after_commit or not self._has_code_commit:
            return False
        return self.total_turns >= (
            self.config.max_turns - self.config.converge_horizon
        )

    def _record_eval_scores(self, results: list) -> None:
        """Update the best observed eval score; reset the idle clock on
        improvement.

        ``bash eval.sh`` prints ``score: <X>`` in the tool result (RunTraining
        may print ``accuracy: <X>``). When the best score strictly exceeds the
        score at the last clock reset, the executor is productive — reset the
        idle-commit clock so it keeps running. Re-running the same score does
        nothing; a tie is not an improvement. No-op when ``converge_after_commit``
        is off (the coordinator), so this is cheap for non-executors.
        """
        if not self.config.converge_after_commit:
            return
        new_best: float | None = None
        for r in results:
            text = getattr(r, "content", "")
            if isinstance(text, list):
                text = " ".join(
                    str(b.get("text", "")) if isinstance(b, dict) else str(b)
                    for b in text
                )
            m = _EVAL_SCORE_RE.search(str(text))
            if m:
                val = float(m.group(1))
                if new_best is None or val > new_best:
                    new_best = val
        if new_best is None:
            return
        if not self._scores_observed:
            self._scores_observed = True
            self._best_eval_score = new_best
        elif new_best > self._best_eval_score:
            self._best_eval_score = new_best
        if (
            self._has_code_commit
            and self._best_eval_score > self._score_at_last_reset
        ):
            self._score_at_last_reset = self._best_eval_score
            self._last_code_commit_turn = self.total_turns

    # ------------------------------------------------------------------
    # LLM call with multi-tier error recovery
    # ------------------------------------------------------------------

    _MAX_RECOVERY_ATTEMPTS = 3

    async def _call_llm_with_recovery(self, turn: int) -> LLMResponse | None:
        """Call the LLM with multi-tier error recovery.

        Recovery strategy (mirrors Claude Code's query.ts):
        1. First failure with prompt-too-long → emergency compact → retry
        2. Second failure → harder compact (summarize + drop half) → retry
        3. Third failure → give up, return None
        For transient API errors: retry with bounded exponential backoff.
        For other API errors: retry briefly, then give up.
        """
        tools_schema = [t.to_api_schema() for t in self.tools.values()]

        max_attempts = max(1, int(self.config.llm_retry_attempts or 1))
        for attempt in range(1, max_attempts + 1):
            try:
                return await self.provider.create(
                    system=self.system_prompt,
                    messages=self.messages,
                    tools=tools_schema,
                    max_tokens=self.config.max_tokens,
                )
            except Exception as e:
                error_str = str(e).lower()
                is_context_error = any(
                    phrase in error_str
                    for phrase in ["prompt is too long", "context length", "max_tokens",
                                   "request too large", "context_length_exceeded"]
                )
                is_transient_error = any(
                    phrase in error_str
                    for phrase in [
                        "429",
                        "rate limit",
                        "too many requests",
                        "temporarily unavailable",
                        "service unavailable",
                        "gateway timeout",
                        "connection reset",
                        "connection aborted",
                        "timeout",
                        "timed out",
                    ]
                )

                log.error(
                    "LLM call failed (turn %d, attempt %d/%d): %s",
                    turn, attempt, max_attempts, e,
                )
                self._emit_event("llm.error", {
                    "provider": self.config.provider,
                    "model": self.provider.model,
                    "turn": turn,
                    "attempt": attempt,
                    "max_attempts": max_attempts,
                    "retrying": attempt < max_attempts,
                    "error": str(e),
                })

                if not is_context_error:
                    non_transient_max_attempts = min(2, max_attempts)
                    attempt_limit = max_attempts if is_transient_error else non_transient_max_attempts
                    if attempt >= attempt_limit:
                        _print_status(f"LLM error after {attempt} attempts: {e}")
                        return None
                    delay = _retry_delay(
                        attempt=attempt,
                        base_delay=self.config.llm_retry_base_delay if is_transient_error else 2.0,
                        max_delay=self.config.llm_retry_max_delay if is_transient_error else 5.0,
                    )
                    _print_status(f"LLM error, retrying in {delay}s: {e}")
                    await asyncio.sleep(delay)
                    continue

                # Context-related error: progressively more aggressive compaction
                if attempt == 1:
                    _print_status("Context too long. Applying emergency compaction (layers 1-3)...")
                    from .context import (
                        _truncate_long_tool_results,
                        _snip_old_tool_results,
                        _drop_redundant_reads,
                    )
                    self.messages = _truncate_long_tool_results(self.messages)
                    self.messages = _snip_old_tool_results(self.messages)
                    self.messages = _drop_redundant_reads(self.messages)
                elif attempt == 2:
                    _print_status("Still too long. Applying LLM summarization (layer 4)...")
                    self.messages = await self.context_manager._summarize_old_messages(
                        self.messages
                    )
                else:
                    _print_status("Failed after all recovery attempts.")
                    return None

        return None

    # ------------------------------------------------------------------
    # Tool execution with concurrency control
    # ------------------------------------------------------------------

    async def _execute_tools(self, tool_calls: list[ToolCall]) -> list[ToolResultBlock]:
        """Execute tool calls with read-only concurrency, write serialization."""
        # Partition into read-only and writable
        read_only_calls: list[tuple[ToolCall, Tool]] = []
        write_calls: list[tuple[ToolCall, Tool]] = []

        for tc in tool_calls:
            tool = self.tools.get(tc.name)
            if tool is None:
                # Unknown tool — return error
                read_only_calls.append((tc, None))  # type: ignore
                continue
            if tool.is_read_only:
                read_only_calls.append((tc, tool))
            else:
                write_calls.append((tc, tool))

        results: list[ToolResultBlock] = []

        # Execute read-only tools concurrently
        if read_only_calls:
            sem = asyncio.Semaphore(self.config.max_tool_concurrency)

            async def _run_read(tc: ToolCall, tool: Tool | None) -> ToolResultBlock:
                async with sem:
                    return await self._execute_single(tc, tool)

            read_results = await asyncio.gather(
                *[_run_read(tc, tool) for tc, tool in read_only_calls]
            )
            results.extend(read_results)

        # Execute write tools serially
        for tc, tool in write_calls:
            result = await self._execute_single(tc, tool)
            results.append(result)

            # Auto-commit after write operations
            if tool is not None and self.config.auto_git:
                file_path = tc.input.get("file_path", tc.input.get("command", ""))
                commit_hash = await self.git_manager.auto_commit(
                    f"agent: {tool.name}: {file_path[:80]}"
                )
                if commit_hash:
                    self.experiment_tracker.log_iteration(
                        turn=self.total_turns,
                        action=f"{tool.name}: {file_path[:100]}",
                        status="committed",
                        git_commit=commit_hash,
                    )
                    # Executor convergence: a meaningful code commit resets the
                    # idle-commit clock (scratch A/B files don't).
                    self._record_code_commit(tool, file_path)

        return results

    async def _execute_single(self, tc: ToolCall, tool: Tool | None) -> ToolResultBlock:
        """Execute a single tool call and return the result."""
        if tool is None:
            return ToolResultBlock(
                tool_use_id=tc.id,
                content=f"Error: Unknown tool '{tc.name}'. Available tools: {', '.join(self.tools.keys())}",
                is_error=True,
            )

        _print_tool(tc.name, tc.input)
        self._emit_event("tool.start", {
            "name": tc.name,
            "args_preview": _preview(tc.input),
            "agent": self.config.agent_label,
            "node_id": self.config.node_id,
        })
        t0 = time.monotonic()

        try:
            output = await tool.execute(**tc.input)
            # Smart result handling: persist large results, truncate as fallback
            output = tool.process_result(output)
            _print_tool_result(tc.name, output)
            self._emit_event("tool.end", {
                "name": tc.name,
                "ok": True,
                "duration": round(time.monotonic() - t0, 3),
                "output_preview": _preview(output),
                "agent": self.config.agent_label,
                "node_id": self.config.node_id,
            })
            return ToolResultBlock(tool_use_id=tc.id, content=output)
        except Exception as e:
            error_msg = f"Tool execution error: {type(e).__name__}: {e}"
            log.error("Tool %s failed: %s", tc.name, error_msg)
            _print_tool_result(tc.name, error_msg, is_error=True)
            self._emit_event("tool.end", {
                "name": tc.name,
                "ok": False,
                "duration": round(time.monotonic() - t0, 3),
                "output_preview": _preview(error_msg),
                "agent": self.config.agent_label,
                "node_id": self.config.node_id,
            })
            return ToolResultBlock(tool_use_id=tc.id, content=error_msg, is_error=True)

    def _emit_event(self, event_type: str, data: dict[str, Any]) -> None:
        bus = self.config.event_bus
        if bus is None:
            return
        try:
            bus.emit(event_type, data)
        except Exception:
            pass

    @asynccontextmanager
    async def _heartbeat(self, operation: str):
        """Emit ``HEARTBEAT`` liveness events while a long phase runs (#8).

        A background task pulses every ``heartbeat_interval`` seconds so the
        dashboard/WebUI can tell "working" from "hung" without the model
        polling. No-op when no bus is wired or the interval is non-positive.
        """
        interval = float(getattr(self.config, "heartbeat_interval", 30.0) or 0.0)
        if interval <= 0 or self.config.event_bus is None:
            yield
            return

        t0 = time.monotonic()

        async def _beat() -> None:
            try:
                while True:
                    await asyncio.sleep(interval)
                    self._emit_event("progress.heartbeat", {
                        "agent": self.config.agent_label,
                        "node_id": self.config.node_id,
                        "operation": operation,
                        "elapsed_seconds": round(time.monotonic() - t0, 1),
                        "detail": operation,
                    })
            except asyncio.CancelledError:
                pass

        task = asyncio.create_task(_beat())
        try:
            yield
        finally:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass


def _retry_delay(*, attempt: int, base_delay: float, max_delay: float) -> float:
    """Compute bounded exponential backoff with a small jitter."""
    delay = min(max_delay, base_delay * (2 ** max(0, attempt - 1)))
    if delay <= 0:
        return 0.0
    jitter = min(delay * 0.1, 3.0)
    return round(delay + random.uniform(0, jitter), 2)


_TOOL_CALL_BLOCK_TYPES = frozenset({
    "tool_use",
    "function_call",
    "custom_tool_call",
    "tool_search_call",
})


def _without_tool_calls(content: Any) -> Any:
    """Remove unexecuted tool-call blocks from assistant history.

    Providers use different history shapes: Anthropic/OpenAI-chat adapters
    normalize calls to ``tool_use``, while the Responses API keeps native
    ``function_call`` items. Interactive turns must not persist either shape
    without a matching tool result.
    """
    if not isinstance(content, list):
        return content
    return [
        block
        for block in content
        if not (
            isinstance(block, dict)
            and block.get("type") in _TOOL_CALL_BLOCK_TYPES
        )
    ]


def _looks_like_premature_no_tool_stop(text: str) -> bool:
    """Detect assistant text that promises work but has not actually done it.

    Strategy: positive-signal-only. If the text contains a future-intent
    marker ("I'll", "next I", "下一步", "我会", …), the model is describing
    work it has not actually executed — we nudge it to follow through.

    We deliberately do NOT short-circuit on "completion" words ("score:",
    "completed", "implemented", …) because the coordinator uses that
    vocabulary every turn while reporting executor results. Treating those
    as completion signals lets a status report masquerade as a final
    answer and the persistent loop exits mid-run.
    """
    normalized = " ".join((text or "").strip().lower().split())
    if not normalized:
        return False

    future_markers = (
        "now let me",
        "let me ",
        "i'll ",
        "i will ",
        "i’m going to",
        "i'm going to",
        "i am going to",
        "i need to",
        "i should ",
        "next i",
        "next, i",
        "start with",
        "set up",
        "write the",
        "create the",
        "run the",
        "train ",
        "evaluate ",
        # Chinese future / intent
        "下一步",
        "接下来",
        "我会",
        "我将",
        "我要",
        "我打算",
        "我准备",
        "让我",
        "我先",
        "稍后",
        "马上",
        "再开始",
        "重新跑",
        "重新运行",
        "再跑",
        "应该",
    )
    if any(marker in normalized for marker in future_markers):
        return True

    # Chinese sentences often end with 。 instead of : when announcing next steps
    return normalized.endswith(":") or normalized.endswith("：")


# ---------------------------------------------------------------------------
# Console output helpers
# ---------------------------------------------------------------------------
#
# DISPLAY_HOOK lets an outer layer (e.g. the intake REPL) take over rendering.
# When set, every _print_* helper forwards (event_type, payload) to the hook
# instead of writing to stderr. Set to None to restore the default behavior.
# Event types: "status", "user", "assistant", "tool_call", "tool_result".
DISPLAY_HOOK: Any = None  # Callable[[str, dict[str, Any]], None] | None


def _print_status(msg: str) -> None:
    if DISPLAY_HOOK is not None:
        DISPLAY_HOOK("status", {"message": msg})
        return
    print(f"\033[90m--- {msg}\033[0m", file=sys.stderr)


def _print_user(msg: str) -> None:
    if DISPLAY_HOOK is not None:
        DISPLAY_HOOK("user", {"message": msg})
        return
    preview = msg[:200] + "..." if len(msg) > 200 else msg
    print(f"\033[36m[User]\033[0m {preview}", file=sys.stderr)


def _print_assistant(msg: str) -> None:
    if DISPLAY_HOOK is not None:
        DISPLAY_HOOK("assistant", {"message": msg})
        return
    print(f"\033[33m[Assistant]\033[0m {msg}", file=sys.stderr)


def _print_tool(name: str, inputs: dict) -> None:
    if DISPLAY_HOOK is not None:
        DISPLAY_HOOK("tool_call", {"name": name, "inputs": inputs})
        return
    # Compact representation of tool inputs
    parts: list[str] = []
    for k, v in inputs.items():
        s = str(v)
        if len(s) > 80:
            s = s[:80] + "..."
        parts.append(f"{k}={s}")
    args_str = ", ".join(parts)
    print(f"\033[35m[Tool: {name}]\033[0m {args_str}", file=sys.stderr)


def _print_tool_result(name: str, output: str, *, is_error: bool = False) -> None:
    if DISPLAY_HOOK is not None:
        DISPLAY_HOOK("tool_result", {"name": name, "output": output, "is_error": is_error})
        return
    color = "\033[31m" if is_error else "\033[32m"
    preview = output[:300].replace("\n", "\\n")
    if len(output) > 300:
        preview += "..."
    print(f"{color}[Result: {name}]\033[0m {preview}", file=sys.stderr)
