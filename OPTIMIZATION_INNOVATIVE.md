# Arbor 创新性优化清单（第二份：结构性/架构级）

> 基于对 `src/` 的深入阅读 + 4 个并行探索代理（LLM 调用层 / Idea Tree 选择策略 / 编排执行流水线 / 上下文记忆层）。
> 这份文档记录的是**创新性/结构性优化**——不是修性能小毛刺，而是改变**算法、架构、调度策略、记忆形态**的改进。共 **27 个机会**，归为 **7 个创新方向**。
> 文件路径相对 `src/` 根目录；行号为当前快照（2026-08-11）。
>
> 姊妹文档：`OPTIMIZATION_FINDINGS.md`（第一份：28 个增量优化）、`OPTIMIZATION_MAP.md`（架构图 + 优化点位置讲解）。

---

## 0. 一句话总览

> **把 Arbor 从"单一 LLM 文本智能"升级为"语义记忆 + 价值调度 + 流水线并行"的研究系统。**

- **语义记忆**：现在 idea 去重、经验召回全靠 LLM 读文本/关键词匹配，升级为 embedding 向量检索。
- **价值调度**：现在预算只按墙钟时间，升级为按"期望收益/成本"分配实验次数。
- **流水线并行**：现在 coordinator 在 executor 跑实验时完全空转，升级为重叠工作。
- **成本分层**：现在所有小 LLM 调用都用跟协调器一样的贵模型，升级为分层路由 + 约束解码。
- **质量护栏**：现在坏实验跑到 timeout 才停，升级为早停 + 阶段门。
- **上下文架构**：现在压缩一律"位置截断 + 一次性总结"，升级为语义分级 + 分形摘要。
- **范式变革**：把单线程 ReAct 循环换成阶段状态机 / 多协调器。

---

## 1. 七个方向在项目中的位置（总览图）

```
┌─────────────── 六步 arbor cycle（当前是单线程 ReAct 循环）───────────────┐
│                                                                       │
│  OBSERVE → IDEATE → SELECT → DISPATCH → BACKPROPAGATE → DECIDE       │
│     │         │        │        │            │           │           │
│     │     [E9 投机] [E5-E8    [E10 解耦    [E11 渐进    [E26 状态机] │
│     │     预生成]  价值排序]  任务队列]     回传]        [E27 多协调] │
│     │                   │        │            │                      │
│     ▼                   ▼        ▼            ▼                      │
│  Idea Tree ──────────[E1/E4 embedding 去重与检索]                     │
│     │                                                               │
│     ▼                                                               │
│  Executor 实验 ──────[E20 早停] ──[E21 smoke 门] ──[E22 工具缓存]    │
│     │                                                               │
│     ▼                                                               │
│  LLM 调用 ──────────[E14/E15 模型路由] ──[E16 结构化输出]            │
│     │                [E17/E18 传播合并] ──[E19 小调用缓存]           │
│     │                                                               │
│     ▼                                                               │
│  上下文管理 ────────[E12 预测压缩] ──[E13 流式早启]                 │
│     │                [E23 分级] ──[E24 分形摘要] ──[E25 多断点]      │
│     ▼                                                               │
│  会话记忆 ──────────[E2 语义召回] ──[E3 跨 run 全局记忆]             │
└──────────────────────────────────────────────────────────────────────┘
```

每个方向的内在逻辑：

| 方向 | 从 → 到 | 改变的"形态" |
|------|--------|-------------|
| ① 语义化记忆 | 文本对比 → 向量检索 | 记忆形态 |
| ② 价值驱动调度 | 墙钟预算 → 期望价值预算 | 调度依据 |
| ③ 流水线化 | 串行等待 → 重叠并行 | 时间结构 |
| ④ 成本分层 | 一价全包 → 分级路由 | 成本结构 |
| ⑤ 预算质量护栏 | 跑满 timeout → 早停/阶段门 | 资源纪律 |
| ⑥ 上下文架构 | 位置截断 → 语义归档 | 上下文管理 |
| ⑦ 范式变革 | 单线程 ReAct → 状态机/多协调器 | 系统形态 |

---

## 2. 方向一：语义化记忆（把"比对文本"换成"比对语义"）

**现在的问题**：idea 是否重复、经验是否相关，全靠关键词/LLM 读文本判断。`recall.py:9` 自己都写着 "an embedding/LLM judge can replace `_score` later"——项目预留了这条路但一直没走。

**这个方向对项目意味着什么**：研究系统最容易犯的错是**重复探索死路**。现在防重复靠 LLM 读 `get_constraints_block()` 的纯文本，靠不住；换成向量相似度后变成确定性护栏。

| # | 机会 | 位置 | 创新点 | 收益 | 风险 |
|---|------|------|--------|------|------|
| **E1** | Embedding 驱动 idea 去重 + SELECT 多样性打分 | `tree_ops.py:162`（add 前）；新 `embedding_store.py` | 新 idea 提交前算 embedding，与 done/merged/pruned 节点比余弦相似度，超阈值给警告；SELECT 时偏好离已执行节点最远的 pending 节点 | 省 10-30% 重复 executor 运行；让选择变成可计算 | 每次 add 多 ~200ms；需批量回填旧树；误报需调阈值 |
| **E2** | 语义召回替代 Jaccard 关键词匹配 | `recall.py:47-63` | 把 `_score()` 的 `len(topic∩exp)/len(topic)` 换成 embedding 余弦；`all-MiniLM-L6-v2` 本地模型或 provider 的 embedding API | "gradient clipping" 能匹配到 "exploding gradient mitigation"（现在相似度=0） | 引入 embedding 依赖；EXPERIENCE.md 变更需重算 |
| **E3** | 跨 run 全局记忆索引 | 新 `global_memory.py`；接入 `distill.py:132` | 每次 run 蒸馏完把 findings 追加进持久向量索引（带 run_id/domain/kind/score 元数据），`recall.py` 启动时先查索引 | 第 10 次跑同一 benchmark 拥有前 9 次的全部经验；可 `--domain` 过滤防污染 | 索引持久化需原子写 + 可重建；需去重 |
| **E4** | 运行中可调的 ResearchRecall 工具 | 新工具注册进 `tools/__init__.py` | coordinator 在 IDEATE 时主动查全局记忆："有人试过类似的吗？结果如何？" | 避免跨 run 重走死路；IDEATE 从纯思考变成有记忆依据 | 每次查询一次 embedding 调用；存储增长需清理 |

**一句话**：E2 是"启动时的召回升级"，E3/E4 是"把记忆变成运行中随时可查的索引"，E1 是"把去重变成确定性护栏"。它们共用一套 embedding 基建，建议一起做。

---

## 3. 方向二：价值驱动调度（预算不该按墙钟，该按期望价值）

**现在的问题**：`max_depth` 固定、无分支预算，一个坏方向能吃掉 10+ 次 executor 运行；SELECT 靠 LLM 的"promising"主观判断；`RunExecutorParallel` 按 LLM 提交顺序跑，无成本收益分析。收敛检测器（`convergence.py`）算出 velocity 但从不用它预测"下一个哪个节点最可能赢"。

**这个方向对项目意味着什么**：从"探索"变成"探索-利用权衡"。同样 40 次实验预算，能集中在高期望分支上，省下 15-40% 的浪费。

| # | 机会 | 位置 | 创新点 | 收益 | 风险 |
|---|------|------|--------|------|------|
| **E5** | Expected Improvement（EI）引导节点选择 | 新 `expected_improvement.py`；接入 `convergence.py:88`、`prompts.py:401` | 对每个 pending 叶子算 `EI = (最好情况估计 − trunk) × P(赢 trunk \| 父子树)`，用兄弟分数分布估计概率（Laplace 平滑） | 用"量化依据"取代"promising"模糊判断；自然降权全失败的子树 | 样本少时噪声大，需全局改善率作先验；建议只作推荐不硬拦 |
| **E6** | 分支预算 + 动态深度 | `idea_tree.py:28`（Node 加 `branch_budget`）；`executor_run.py:88` | 每个子树固定预算（默认 3 次），耗尽且无改善→整子树自动剪枝；有改善→预算+1；有多个改善孩子的 family 允许深度+1 | 防"一个坏方向吃 10 次实验"；预算动态流向有希望的方向 | 默认 3 可能太保守，需配置逃生门；"第 4 次才突破"的遗憾 |
| **E7** | 价值感知调度器 | 新 `scheduler.py`；`executor_run.py:1002` | 派发前算 `priority = EI / 预估成本`（成本从历史 executor token 用量估计），高优先先跑、低优先建议重排或裁剪 | 同样预算完成更多实验；推迟"又贵又可能失败"的大实验 | token 成本预测噪声大（2-10x）；应排序而非硬过滤 |
| **E8** | 方法家族经济（家族级淘汰） | `idea_tree.py:396`（constraints block 加家族状态段）；`convergence.py:240` | 把 depth-1 节点形式化为"方法家族"，聚合 family 最优分/改善率/剩余预算/状态；家族 stalled→提示重新分配预算，exhausted→整族一次剪枝 | 比逐节点剪枝更早发现死方向；上下文里 1 行"Family A exhausted"替代 10 行 pruned 节点 | 依赖 depth-1 节点真的代表独立方法；可能误杀深层的黑马 |

**一句话**：E5 是决策依据，E6/E8 是预算边界，E7 是执行时的排队策略。E5 的 EI 是 E7 的输入，建议 E5→E6→E7 阶梯式落地。

---

## 4. 方向三：流水线化（把"等待"变成"重叠"）

**现在的问题**：executor 跑 10-60 分钟期间，coordinator 完全空转（`agent.run()` 阻塞）；上下文压缩是调用前被动触发；LLM 流式输出时什么都不干。项目其实**已经有后台并发的先例**——`search_ctx.py` 的 SearchAgent 用 `_BG_TASKS` 注册表 + `_MAX_PARALLEL=4` 信号量实现了 fire-and-forget，executor 派发却没沿用这个模式。

**这个方向对项目意味着什么**：把研究从"爆发式"变成"持续满载"。40 轮循环能省 20-40 分钟墙钟。

| # | 机会 | 位置 | 创新点 | 收益 | 风险 |
|---|------|------|--------|------|------|
| **E9** | 投机式 IDEATE（executor 跑时预生成 idea） | `orchestrator.py:380`；新 `speculative_ideate.py` | executor 开始跑时后台协程预生成候选 idea 存缓冲（带树状态 hash）；executor 完成时若相关分支没变就直接注入，跳过 IDEATE 轮 | 每轮省 60-120s；40 轮省 20-40 分钟 | 树变了要失效重来；空窗期 token 成本翻倍 |
| **E10** | 解耦执行：持久任务队列 | 新 `execution_queue.py`；改 `executor_run.py:816` | 派发改成 `asyncio.Queue`，返回"已接受"，worker 池后台跑，完成后注入合成 tool_result 消息。**先例**：`agent.py:308` 的 `drain_notifications()` 就是为此设计的 | coordinator 永不阻塞；结果异步到达自动消化；好结果可取消后续 | **ReAct 协议变更**：Anthropic 要求 tool_use/tool_result 严格配对，需占位符+延迟注入代理模式；prompt 要改成 fire-and-forget |
| **E11** | 渐进式回传（`gather` → `as_completed`） | `executor_run.py:1113` | 每个 executor 一完成就立即 parse→更新树→传播→收敛检测→注入消息，不等全部 | 4 个各 10 分钟的实验，首个结果 t=10min 而非 t=40min；可中途裁剪冗余实验 | 需要 generator/回调模式；取消 inflight 要处理 worktree 清理 |
| **E12** | 预测性压缩（空闲期预压缩） | `context.py:301`；`agent.py:516` | 工具执行开始时后台跑 Layer-4 LLM 总结（目标是冻结的旧消息前缀），等工具返回时总结已就绪 | 把阻塞的 LLM 压缩调用移出关键路径，每轮省 2-10s | 摘要略过时但仍是有效起点；任务需在 `_aclose_tools` 取消 |
| **E13** | 流式 + 只读工具提前启动 | `agent.py:331`（改用 `create_streaming`） | LLM 还在流式输出时，一收到完整工具参数就立刻执行只读工具（`tool.is_read_only` 分区分好），与输出尾部重叠 | 每轮省 2-4s；重工具轮次省 10-15% 墙钟 | 部分 JSON 累积、流中断取消、两种流式事件模式都要处理 |

**一句话**：E11 最易落地（纯收益），E9 次之（纯重叠），E10 收益最大但改协议。E13 是"复用已实现却没被主循环用的 `create_streaming`"。E12 把压缩从"被动救火"变"主动预备"。

---

## 5. 方向四：成本分层与约束（同样研究花更少的钱）

**现在的问题**：所有小 LLM 调用（解析报告、解析 eval 分、传播 insight、SearchAgent 检索循环）都用跟 coordinator 主循环**同一个贵模型**。`SearchConfig.agent_model`（`config.py:308`）默认 `None`——模型分离的基建**已经造好**（`_maybe_override_provider`），就是没设默认值。全项目**没有任何结构化输出**：`_parse_executor_report` / `_parse_eval_score` / `propagate` 都是自由格式 JSON + 手剥 markdown fence + 静默失败退化。

**这个方向对项目意味着什么**：最直接的美元节省。50 实验的 run，小调用从 ~$7.6 降到 ~$0.5（-93%）；SearchAgent 每 run 省 ~$10.5。

| # | 机会 | 位置 | 创新点 | 收益 | 风险 |
|---|------|------|--------|------|------|
| **E14** | 分层模型路由（`extraction_model`） | 新 `extraction_model` 配置；接入 `executor_io.py:238`、`tree_ops.py:600`、`git_ops.py:91` | coordinator 主循环用贵模型，小抽取调用用便宜模型（参考 `SearchConfig.agent_model` 的模式） | 小调用成本省 ~90%（~$7/run） | 便宜模型解析分可能错→"便宜先试，贵重试"双保险 |
| **E15** | SearchAgent 模型分离强制化 | `search_agent/agent.py:97`（`_maybe_override_provider`）；`config.py:308` | 把 `agent_model` 默认值从 `None` 改成便宜模型的合理默认，**始终**为 SearchAgent 建独立 provider | 每 run 省 ~$10.5，检索是信息字段不驱动调度，质量影响小 | 相关论文召回略降（advisory 字段，可接受） |
| **E16** | 结构化输出 / 约束解码 | `executor_io.py:238-265`、`git_ops.py:91-119`、`tree_ops.py:600` | 三处抽取改 provider 原生约束：Claude 用 `tool_choice`+内联 `input_schema`，OpenAI 用 `response_format: json_schema` | 输出 token 省 30-60%；消除静默失败退化（现在约 2-5% 提取退化造成级联浪费） | provider 参数模式差异；单轮工具调用形状要测试 |
| **E17** | 融合解析+传播为单次结构化调用 | `executor_run.py:568-669` | 原始报告+树上下文一次性发，返回 `{"parsed": {...}, "ancestor_insights": {...}}`，一次更新节点+祖先 | 1+N 次调用→1 次（深度 4 省 80%）；祖先综合能看到原始报告，质量更高 | 报告超长要截断回退；错误粒度变粗 |
| **E18** | 单遍祖先传播 | `tree_ops.py:564-643` | 不逐祖先串行，一次调用返回 `{ancestor_id: insight}` 映射 | 深度 4：4 次→1 次，延迟省 60-75%，子 insight 只发一次 | 深树输入超限要回退串行；"跳过"语义要写进 prompt |
| **E19** | 小调用语义缓存 / 同级去重 | `tree_ops.py:581` | 父节点被 K 个孩子各传播一次，第 K 次只比 K-1 次多一个新孩子——LRU 缓存 `md5(provider+model+prompt)` 或启发式跳过 | 省 20-40% 传播调用 | 哈希碰撞极低但关键数据不可赌；只用于无副作用的小调用 |

**一句话**：E15 是零改动接线（改默认值），E14 是推广同一模式，E16 解决"解析不坏"，E17/E18 是 A1 的演进版（把"合并调用"升级为"结构化输出保证正确"），E19 堵"重复传播"。

---

## 6. 方向五：预算质量护栏（坏实验别跑满 timeout）

**现在的问题**：executor 死循环在坏实现上时，**没有任何零进展检测**，跑到 `executor_timeout` 才停；所有实验一视同仁给满预算；只读工具重复执行浪费 I/O 和 context。

**这个方向对项目意味着什么**：把"跑满预算"变成"证明自己值得继续跑"。

| # | 机会 | 位置 | 创新点 | 收益 | 风险 |
|---|------|------|--------|------|------|
| **E20** | Yield monitor 早停 | `executor_run.py:529`；新 `yield_monitor.py` | 并行协程给每轮打分（成功 Bash/提交代码 +1、错误/重复调用 -1），连续 N 轮低于阈值→设 `stop_reason="low_yield"` 取消，剩余预算还回池 | 死路 executor 5 分钟被掐 vs 30 分钟浪费；省 15-25% 总算力 | 误伤长训练任务→豁免 RunTraining；需暴露 agent 观察 API；worktree 清理 |
| **E21** | 分段评估：smoke 门先过再跑完整 eval | `config.py:28`（**`BudgetStage` 已定义未接线**）；`executor_io.py:101` | 接线现成的 `BudgetStage`：先 ~15% 预算跑 2-3 样本，`smoke_passed` 为假→早停 `needs_retry`，省下 85% 预算还池 | 不编译/跑错路径的实现 5 分钟被抓出；`promotion_gate` 字段可表达"score > baseline×1.02 才升 test" | 提示词要传达两阶段；smoke 信号 schema 小改 |
| **E22** | 确定性工具结果缓存 | `tools/base.py`；`agent.py:699`；`file_read.py`/`grep.py`/`glob_tool.py` | 按 `sha256(tool+args+文件指纹(mtime,size))` 缓存只读工具结果，任何写工具调用后失效 | 重复 Read 128K 文件=3 万 token/次，20 轮×5 次=~3M token 变 0；减少压缩触发 | 失效保守处理（所有 Bash 都失效）；LRU 上限 |

**一句话**：E21 是**最低风险最高 ROI 的接线**（`BudgetStage` 字段全定义了就是没人用），E20 给 executor 装"止损器"，E22 顺手清掉重复 I/O。三者都在"每次实验"热路径上，建议最先做。

---

## 7. 方向六：上下文架构（从"位置截断"到"语义归档"）

**现在的问题**：压缩第 2 层 `_snip_old_tool_results`（`context.py:122`）纯按位置截断——15 轮前的关键失败证据和 3 轮前的噪音 glob 一视同仁；第 4 层总结一次性把旧消息压成一段扁平摘要，**细节永久丢失**；Anthropic 缓存 4 个断点只用 3 个。

**这个方向对项目意味着什么**：让 LLM 的上下文"花在刀刃上"——高价值证据跨轮保留，噪音早丢弃；已探索方向的细节可随时展开，而非永久蒸发。

| # | 机会 | 位置 | 创新点 | 收益 | 风险 |
|---|------|------|--------|------|------|
| **E23** | 重要性分级上下文 + 树归档 | 新 `context_grading.py`；改 `context.py:122` | 给每条消息按"对当前节点相关度"打分分三级：core 留超 `keep_recent_n`、reference 标准截断、archivable 移到节点 `archived_context`（`TreeView` 可取回） | 15 轮前的失败证据保留，3 轮前的噪音丢弃 | 误归档→需 `RecallContext(node_id)` 工具取回；压缩时批量打分非每轮 |
| **E24** | 分形摘要（实验级→分支级→run 级） | 新 `progressive_summary.py`；改 `context.py:355` | 总结时按 `node_id` 打标签，写出 `.coordinator/summaries/<node_id>.md`，消息留指针；回到某方向时 `ExpandContext(node_id)` 重新注入 | 已探索分支的细节永不丢失；多分辨率检索贴合树的层次结构 | 摘要关联错节点产生噪音；存储增长需清理策略 |
| **E25** | 多断点缓存布局（用上第 4 个断点） | `claude.py:230`（`_cache_messages`）；`context.py:404` | 把第 4 个断点放在压缩摘要块上做"稳定锚"：system+tools（断点1/2）+ 压缩摘要前缀（断点4）跨轮缓存，只有易变尾部重算 | 每次压缩不再全量重建前缀缓存，50 轮 run 省 5 次 × 5-10 万 token 的 cache-creation | 工具定义动态变化会破前缀；4 断点上限用满 |

**一句话**：E25 直接对冲"A3 压缩破坏缓存"的老问题（从"尽量别动旧消息"升级为"给摘要块一个稳定断点"）。E23/E24 一起构成"重要性感知的上下文"体系。

---

## 8. 方向七：范式变革（高风险的系统形态切换）

**现在的问题**：六步 arbor cycle **只存在于 system prompt 文本里**，代码层面没有任何约束——agent 可以任意顺序调任何工具，`_completed_cycles` 是唯一的循环级护栏。

**这个方向对项目意味着什么**：目前最大的架构惯性。做对了能让其他所有优化都有栖息地；做错了成本极高。

| # | 机会 | 位置 | 创新点 | 收益 | 风险 |
|---|------|------|--------|------|------|
| **E26** | 阶段状态机替代单线程 ReAct | `orchestrator.py:380`；新 `phase_machine.py` | 六个阶段建模为显式状态+工具门控（INIT 只给 Bash/Read、DISPATCH 才给 RunExecutor…），每阶段短 prompt 调 `agent.run()`，消息跨调用持久 | 循环纪律可执行；每阶段可独立分配时间预算；天然支持阶段重叠（呼应 E9）；新阶段易加 | 分 prompt 破坏 KV-cache 前缀稳定（`_system_hash` 要改）；需"任意工具"兜底模式；**最侵入** |
| **E27** | 多协调器并行探索独立子树 | `orchestrator.py:122` | depth-1 节点≥3 时给每个家族 fork 子协调器，各自跑分支级循环，共享 `IdeaTree`（`async_update_node` 锁安全），父协调器分配预算+汇总跨分支 insight | 3 个独立方法族并行，墙钟最多省 3x | **最高风险**：重叠 idea、4x 协调器成本、并发 merge 冲突、共享 worktree 从同一 trunk HEAD 起的假设 |

**一句话**：E26 是"把 prompt 里口头约定的循环变成代码强制执行"，E27 是"把单协调器扩展成可并行的研究组织"。两者都建议**最后做**，且最好在 E9/E10/E11 之后——因为它们就是流水线化和解耦的受益方。

---

## 9. 优先级与落地路线

按「基建就绪度 × 收益 × 风险」综合排序：

| 阶段 | 机会 | 理由 |
|------|------|------|
| **P0 接线型（最优先，基建已就绪）** | **E21** smoke 门（`BudgetStage` 字段已定义未接线） | 零新依赖，纯接线，每次实验省钱省时 |
| | **E15** SearchAgent 模型分离（改默认值） | 零改动接线，每 run 省 ~$10.5 |
| | **E11** `gather`→`as_completed` 渐进回传 | 纯收益，不破坏协议 |
| **P1 低成本型** | **E14** 分层模型路由 | 复制 SearchAgent 模式，省 ~93% 小调用成本 |
| | **E16** 结构化输出 | 消除解析静默失败 + 省 30-60% 输出 token |
| | **E22** 工具结果缓存 | 重复 I/O + 重复 context 归零 |
| | **E19** 小调用语义缓存 | 堵重复传播 |
| **P2 结构性型** | **E9** 投机 IDEATE / **E12** 预测压缩 / **E13** 流式早启 | 时间重叠，中等收益中等风险 |
| | **E1** embedding 去重 + **E2** 语义召回 + **E3** 全局记忆 | 需要引入 embedding 依赖，但方向一有 `recall.py` 官方预留 |
| | **E5** EI 引导 / **E6** 分支预算 / **E7** 价值调度 / **E8** 家族经济 | 价值调度族，E5 先行 |
| | **E17/E18** 传播合并（A1 的结构化演进） | 深度 4 省 60-80% 传播调用 |
| | **E25** 多断点缓存 / **E23** 分级上下文 / **E24** 分形摘要 | 上下文体系升级 |
| | **E20** Yield monitor 早停 | 预算止损器 |
| **P3 架构型（最后）** | **E10** 解耦任务队列 → **E26** 状态机 → **E27** 多协调器 | 范式切换，高风险高收益，且依赖前面的流水线/调度成果 |

---

## 10. 与已有基建/上一份文档的关系

**已确认的"先例锚点"（证明这些创新不是空想，基建已备好）：**

| 创新 | 复用的现成基建 |
|------|---------------|
| E9/E10 后台执行 | `search_ctx.py` 的 `_BG_TASKS` + 信号量 + async-safe 写回（**SearchAgent 已实现，executor 没沿用**） |
| E10 异步结果注入 | `agent.py:308` 的 `drain_notifications()` 机制 |
| E15 模型分离 | `SearchConfig.agent_model` + `_maybe_override_provider`（`search_agent/agent.py:97`） |
| E21 分段预算 | `config.py:28` 的 `BudgetStage`（`walltime/data_fraction/promotion_gate` 全定义了没接线） |
| E3 跨 run 缓存 | `tree_ops.py:528` 的 `baseline_cache.json`（跨 run 缓存 eval 元数据的先例） |
| E2 语义召回 | `recall.py:9` 官方注释预留 "embedding/LLM judge can replace `_score` later" |
| E27 自进化 | `trajectory.py` 已导出 RL/SFT 格式轨迹（reward 回填 + Polar 风格 token 记录） |
| E13 流式 | 4 个 provider 的 `create_streaming` 全部实现但主循环从不用 |

**与 `OPTIMIZATION_FINDINGS.md`（第一份增量优化）的关系：**

- A1（逐祖先串行传播）→ 演进为 **E17/E18**（结构化 + 单遍）。
- A3（压缩破坏缓存断点）→ 演进为 **E25**（给摘要块稳定断点）。
- B3（gather 阻塞）→ 演进为 **E11**（渐进回传）+ **E10**（彻底解耦）。
- B4（coordinator 阻塞）→ 演进为 **E9**（投机 IDEATE）+ **E10**（任务队列）。
- D9（recall 关键词匹配）→ 演进为 **E2/E3**（语义召回 + 全局索引）。
- 增量优化是"把现有机制跑快"；这份的创新优化是"换一种机制"。

---

> **结论**：这份清单的 27 个机会里，**P0 三项（E21/E15/E11）是零风险接线、当天可做**；P2 的语义记忆方向（E1-E3）是项目预留已久、收益最大的结构性升级；P3 的 E26/E27 是整个系统的未来形态，但应排在前面所有成果之后。
