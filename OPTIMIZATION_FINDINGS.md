# Arbor 项目优化点清单

> 基于对 `src/` 下约 4 万行代码的全面阅读 + 4 个并行探索代理的分析。
> 文件路径相对 `src/` 根目录，行号为当前快照（2026-08-11）。

---

## A. LLM 调用层（成本影响最大）

### A1. `propagate_insights` 逐祖先串行 LLM 调用
- **位置**: `coordinator/tools/tree_ops.py:564-643`
- **问题**: 每个 executor 实验完成后，`propagate_insights` 从父节点到 ROOT 逐个调用 `provider.create()`。深度 3 的树 = 1 次解析 + 3 次传播 = 4 次 LLM 往返/实验。
- **建议**: 把全部祖先的上下文合并进一次调用，让 LLM 返回 `{ancestor_id: synthesized_insight}` 映射。

### A2. `_parse_executor_report` 每个实验都触发 LLM 解析
- **位置**: `coordinator/tools/executor_io.py:221-293`
- **问题**: score 提取其实用正则 `/(?:score|accuracy)[:\s]*([\d.]+)/i` 大多能搞定，insight/result 才真正需要 LLM。
- **建议**: regex 先试，失败再 fallback 到 LLM。干净报告可省一次调用。

### A3. 上下文压缩使 Anthropic 缓存断点失效
- **位置**: `core/agent.py:317` + `core/context.py`
- **问题**: `maybe_compact()` 就地修改 messages（截断/摘除旧 tool result），而 Claude 的缓存断点标在最后一条消息上。任何压缩触碰缓存前缀后，下一轮整个前缀重新上传，丢失约 5 倍的缓存读取成本。
- **建议**: 压缩前失效旧断点；或只压缩缓存断点之前的消息。

### A4. token 估算每轮全量扫描
- **位置**: `core/context.py:58-76` (`_estimate_message_tokens`)
- **问题**: 每次 LLM 调用前 `maybe_compact` 多次调用它，每次都线性扫描全部消息并逐块 `tiktoken.encode`。200+ 条消息时每次调用 O(N) 且无缓存。
- **建议**: 在消息 dict 里缓存估算值（如 `_est_tokens`），追加时增量更新、压缩时失效。

### A5. 流式路径 usage 为空
- **位置**: `core/llm/openai_compat.py:236`
- **问题**: `create_streaming` 结束时 `Usage()` 留空，token 统计和缓存统计丢失。
- **建议**: 加 `stream_options={"include_usage": True}` 并从最后 chunk 提取。

### A6. Claude token 计数高估 10-15%
- **位置**: `core/llm/claude.py:70-74, 179-180`
- **问题**: 用 tiktoken 的 `cl100k_base` 代理 Claude 真实分词器，高估导致上下文压缩过早触发。
- **建议**: 考虑缓存估算或按模型族校准系数。

### A7. `max_tokens` 截断恢复会双倍计费
- **位置**: `core/agent.py:384-393`
- **问题**: 模型输出截断时追加 nudge 消息并立即再调一次 LLM，同一轮成本翻倍（最多 3 次）。
- **建议**: 这属于合理容错，但可把 `max_tokens` 上限调高或仅在确实需要时恢复。

---

## B. 执行流程层（影响吞吐与响应性）

### B1. `_completed_cycles()` 每次全表扫描
- **位置**: `coordinator/tools/executor_run.py:88-98`
- **问题**: 每次 RunExecutor/RunExecutorParallel/ResumeExecutor 的循环上限检查都线性扫描所有节点。还应缓存计数器，在状态迁移时增量更新。

### B2. `ConvergenceDetector._rebuild_state()` O(n log n) 全树扫描
- **位置**: `coordinator/convergence.py:145-180`
- **问题**: 每次实验完成都重建状态、排序全部 done/merged 节点。N 个实验累计 O(N² log N)。
- **建议**: 维护增量 `_consecutive_non_improving` 计数器，新实验完成时 O(1) 更新。

### B3. `RunExecutorParallel` 用 `gather` 阻塞到全部完成
- **位置**: `coordinator/tools/executor_run.py:1113`
- **问题**: 一个 executor 跑 60 分钟、另一个 5 分钟完成，coordinator 要等 60 分钟才看到任何结果。
- **建议**: 改 `asyncio.as_completed`，逐个 yield 结果，允许中途裁剪剩余 executor 或启动后续实验。

### B4. coordinator 在 executor 运行期间完全阻塞
- **位置**: `coordinator/tools/executor_run.py:437-439`
- **问题**: `wait_for(agent.run())` 期间 coordinator 不能 IDEATE、不能派发 SearchAgent、不能处理用户输入。10-60 分钟的浪费。
- **建议**: 结构性改动——executor 像 SearchAgent 一样后台运行 + 完成回调，coordinator 空窗期做其他事。

### B5. 并行 executor 完成时突发 LLM 调用
- **位置**: `coordinator/tools/executor_run.py:1113` + `executor_io.py:238` + `tree_ops.py:600`
- **问题**: 4 个 executor 同时完成后，最多 4 个 parse + 4×depth 个 propagate 同时触发，可能撞 provider 限流。
- **建议**: 加 semaphore（如最多 2 并发）限制 parse/propagate 并发。

### B6. eval_info 在每个 executor prompt 重复注入
- **位置**: `coordinator/tools/executor_io.py:54-98`
- **问题**: 静态字段（baseline/trunk/dataset）在每个 executor 的用户消息里重复出现，20 个 executor = 20 次重复。
- **建议**: 静态字段放进 executor 的 system prompt，用户消息只留模板替换后的命令。

---

## C. 持久化 / 事件 / 监控层

### C1. `IdeaTree.save()` 每次 mutation 同步双写 JSON + MD
- **位置**: `coordinator/idea_tree.py:348-356`
- **问题**: 每个 `add_node`/`update_node`/`prune_node` 都全量重写两个文件，无防抖。IDEATE 一轮 3-5 次 add 就是 6-10 次写。
- **建议**: Markdown 是派生渲染，可防抖（每 N 次或定时写）；JSON 保持即时写保证崩溃安全。另外 `add_node`/`update_node`（同步）没拿 `_save_lock`，与 `async_update_node` 不一致，有并发竞态风险。

### C2. checkpoint 每轮都写
- **位置**: `coordinator/orchestrator.py:639`
- **问题**: `checkpoint_hook` 每 turn 写 messages.jsonl + checkpoint.json。300-turn 的 run = 300 次写循环。
- **建议**: 防抖到每 N turn 或 executor 结果到达时写。树独立持久化，turn 级崩溃最多丢几轮上下文。

### C3. 事件管道在事件循环上同步做 I/O + 序列化
- **位置**: `events/subscribers/file_logger.py:42-43` + `webui/server.py:195-204` + `events/bus.py:68`
- **问题**: 每次 emit，file_logger 做 `json.dumps` + 每事件 `flush()`（成千上万次 syscall），webui 再 `json.dumps` 一次，全部同步阻塞在 orchestrator 的事件循环上。高频率事件（如 THINKING_DELTA）尤其明显。
- **建议**: 专用 writer 线程 + 队列；file_logger 改周期 flush（如每 200ms 或每 N 事件）。

### C4. `run_state._recount()` 每次状态变化全表扫描
- **位置**: `cli/run_state.py:679-691`
- **问题**: 每次 idea 状态更新都遍历所有 ideas 重算计数器，O(n)/事件。
- **建议**: 增量维护 delta（旧状态 -1，新状态 +1）。

### C5. WebUI snapshot 每 1.5s 全量序列化
- **位置**: `webui/server.py:206-212` + `webui/snapshot.py:96-178`
- **问题**: 心跳每 1.5s 深拷贝整个 RunState（idea ledger、thinking_feed、activity）再 json.dumps。
- **建议**: 缓存快照，仅 state dirty 时重算；或 SSE 增量推送。

### C6. dashboard 每次重绘重建整个树
- **位置**: `cli/run_dashboard.py:1829-1896`
- **问题**: 每次 paint（最多 2.5/sec）都重建 children map 并递归整棵树。
- **建议**: 以 (idea_order 长度, 最后修改 id) 为 key 缓存渲染结果。

---

## D. 工具层 / 代码质量

### D1. `glob_tool` 每个匹配文件 stat()
- **位置**: `core/tools/glob_tool.py:95`
- **问题**: `p.stat().st_mtime` 逐个调用，上万个文件时 1 万次 syscall。
- **建议**: 用 `os.scandir()` 获取缓存 stat，或降低 stat 需求。

### D2. `grep` 每次执行都 `shutil.which("rg")`
- **位置**: `core/tools/grep.py:122`
- **问题**: 应缓存到 `__init__`。
- **建议**: `self._rg_path` 缓存。

### D3. `file_edit` 非原子写入
- **位置**: `core/tools/file_edit.py:192-193`
- **问题**: 写入中途崩溃会留下截断/损坏文件。
- **建议**: 临时文件 + `os.replace()`。

### D4. `file_read` PDF/notebook 走 `_truncate` 而非 `process_result`
- **位置**: `core/tools/file_read.py:137, 176`
- **问题**: 大输出被硬截断而非持久化到磁盘。
- **建议**: 改调 `process_result()`。

### D5. `main.py:133-140` 重复导入（已确认）
- **位置**: `coordinator/main.py:133-140`
- **问题**: `yaml` 和 `redacted_snapshot` 被导入两次。
- **建议**: 删除重复块。

### D6. `_ensure_gitignore` 三处重复实现
- **位置**: `executor/main.py:24-48`、`coordinator/orchestrator.py:225-270`、`coordinator/main.py:60-92`
- **建议**: 合并到 `core/` 共享工具。

### D7. `_run_git` 用 `create_subprocess_shell`
- **位置**: `coordinator/tools/git_ops.py:47`
- **问题**: 字符串拼接进 shell，分支名等内部生成但配置值可能被注入。
- **建议**: 用 `create_subprocess_exec` 显式参数列表。

### D8. `_compute_branch_name` 用 SHA-1
- **位置**: `coordinator/tools/worktree.py:42`
- **建议**: 非加密场景用 `blake2b`/`md5` 更快。

### D9. `recall.py` 关键词重叠匹配 + EXPERIENCE.md 无清理
- **位置**: `recall.py:43-50`、`distill.py`
- **问题**: 纯 Jaccard 关键词匹配脆弱；会话无限累积导致扫描变慢。
- **建议**: 限制最近 N 个会话；`list_experiences` 按 mtime 排序（现在是字母序）。

### D10. export 一次性把整个会话读进内存
- **位置**: `export.py:143-238`
- **问题**: 多天 run 的 artifacts 全量读入再 base64 进 HTML，可达数百 MB。
- **建议**: 流式或限制嵌入文件大小/数量。

---

## 优先实施建议（High Impact / 中低成本）

| 优先级 | 优化点 | 预期收益 |
|--------|--------|---------|
| ⭐⭐⭐ | A1+A2 合并 parse+propagate 的 LLM 调用 | 每个实验省 3~N 次 LLM 调用 |
| ⭐⭐⭐ | B1+B2 增量维护循环计数与收敛状态 | 消除全树扫描热点 |
| ⭐⭐⭐ | C3 事件管道移到写线程 + 周期 flush | 消除事件循环上的磁盘 I/O 阻塞 |
| ⭐⭐ | B3 `gather` → `as_completed` | coordinator 更早拿到中间结果 |
| ⭐⭐ | A3 缓存感知的上下文压缩 | 保住 ~5 倍缓存读取收益 |
| ⭐⭐ | A4 增量 token 估算 | 每轮省 O(N) 扫描 |
| ⭐⭐ | C2 checkpoint 防抖 | 长 run 减少大量磁盘写 |
| ⭐⭐ | B5 parse/propagate 并发限流 | 避免撞 provider 限流 |
| ⭐ | C1 tree 保存防抖 + 锁统一 | 减少 I/O、修并发竞态 |
| ⭐ | D5/D6 去重与共享工具 | 代码质量 |

> **注意**: 本清单是探索阶段的初步发现，部分优化点（如 A1 合并调用、B4 后台 executor）涉及行为/协议变更，实施前应逐一确认其副作用（例如解析失败的状态分类、收敛检测语义、checkpoint 崩溃恢复粒度）。
