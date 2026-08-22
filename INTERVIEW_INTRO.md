# Arbor 项目介绍与我的优化工作 —— 面试准备材料

> 用途：面试前通读一遍即可上场。结构 = **开场自我介绍稿 → 项目介绍 → 我的工作 → 问答准备**。
> 所有事实均有出处：README、源码（`src/` 共约 4 万行 Python）、三份优化文档（`OPTIMIZATION_MAP/FINDINGS/INNOVATIVE.md`）。
> 日期：2026-08-11。

---

## 一、开场自我介绍

### 30 秒稿（电梯版）

> 我最近深入研究了 **Arbor——一个自主科研智能体框架**（人大高瓴 + 微软研究院开源，arXiv 论文）。
> 它让一个"研究主管"Agent（Coordinator）反复执行 **观察→提想法→派活→收结果→总结→决策** 六步循环，
> 每步派活都让一个"研究工程师"Agent（Executor）在**隔离的 git worktree** 里真正改代码、跑实验，
> 只把在**留出测试集**上验证有效的改进合回主干——整个过程长成一棵**假设树**。
> 我通读了 `src/` 下约 4 万行源码，并用并行探索的方法找出了 **55 个优化点**
> （28 个增量 + 27 个结构性），按成本/风险排好优先级。在这个过程中，
> 我对 LLM Agent 系统的成本结构、并发模型、上下文管理和持久化有了很完整的理解。

### 1 分钟稿（正式版）

> 我的工作是**对一个工业级 LLM 多智能体框架做系统性的架构分析和优化设计**。
>
> 先说项目本身：Arbor 的目标是让 AI 自主做研究——给定一个基准和一个优化目标，
> 它自己提假设、改代码、跑实验、保留有效的改进。它的核心是一棵**假设树**：
> 每个节点是一个想法，实验结果和失败教训都存进树里向上传播，让后续想法越来越聪明，
> 而不是像普通 Agent 那样淹没在上下文滚动里。
>
> 技术上，它本质上是**一条 ReAct 工具调用循环**（Coordinator 的 LLM 每轮决定下一步调哪个工具）
> 加**一套围绕它的基础设施**：隔离 worktree 的实验纪律、事件总线 + 实时仪表盘、checkpoint 断点恢复、
> 四层上下文压缩、多 LLM provider 适配。
>
> 我的工作分两轮：第一轮通读全代码，产出 **28 个增量优化点**（成本、执行流、持久化、工具层四类），
> 每个都精确到 `文件:行号`；第二轮是**结构性创新优化**，产出 **27 个机会、归为 7 个方向**，
> 核心主张是"把 Arbor 从单一 LLM 文本智能，升级为**语义记忆 + 价值调度 + 流水线并行**的研究系统"。
> 其中 **3 个 P0 项是零风险接线、当天可落地**，因为我找到了项目里"基建已造好但没接线"的证据。

---

## 二、项目整体介绍

### 2.1 一句话定位

> **Arbor 是一个自主研究智能体：给它一个基准和一个目标，它自己提假设、改代码、跑实验，
> 只保留经得起留出数据验证的改进——长成一棵"假设树"。**

官方卖点：在相同算力预算下，效果超越 Claude Code 与 Codex **2.5×**。

### 2.2 背景与动机

- 现有 Agent（Claude Code / Codex）做任务时，思路是**一次性、逐轮滚动**的——实验结果、失败原因随上下文丢失，无法累积。
- Arbor 想解决的核心问题：**让 Agent 的探索可累积、有纪律、可追溯**。
  - 可累积：所有实验结果写成持久化"假设树"，洞察向上传播，想法越来越聪明。
  - 有纪律：executor 在**开发集**上迭代、**留出测试集**上验证，防止对指标过拟合；每次实验在**独立 git worktree** 跑，合并前主干永不污染。
  - 可追溯：事件总线记录全部运行过程，实时仪表盘 + 断点恢复。

### 2.3 核心机制：六步 Arbor 循环 + 双 Agent

两个协作 Agent，共同重复六步循环：

| 步骤 | 内容 | 谁做 |
|---|---|---|
| ① 观察 Observe | 重读假设树：活跃前沿、约束、祖先洞察、近期证据、当前最优 | Coordinator |
| ② 构思 Ideate | 选父节点，提出 1-3 个子假设（精化/修正/扩展） | Coordinator |
| ③ 择优 Select | 在当前最优方向与未解备选之间平衡，挑最值得测的叶节点 | Coordinator |
| ④ 派发 Dispatch | 把假设交给独立 Executor，在全新 worktree 实现并在开发信号上评估 | Coordinator → Executor |
| ⑤ 反向传播 Backpropagate | 记录结果/分数/洞察/分支，归纳经验传给祖先节点 | Coordinator |
| ⑥ 决策 Decide | 合并 / 剪枝 / 继续 / 待定 / 终止；合并以留出集验证为依据 | Coordinator |

**本质**：这是一个 ReAct 循环——Coordinator 是一个**单一持久的 ReAct Agent**（`src/core/agent.py`），
六步循环并不是代码里的 for 循环，而是靠 system prompt 约定 + LLM 每轮决定调哪个工具来驱动的。

### 2.4 技术架构（分层）

```
① CLI 层      src/cli/        启动、intake 规划对话、实时仪表盘(2773行,全项目最大)、run_state
② 编排层      src/coordinator  CoordinatorOrchestrator(1287行)、IdeaTree 假设树、收敛检测、executor 派发
③ Executor    src/executor+工具 隔离 git worktree、Bash/Read/Edit/Grep/Glob 等工具
④ 核心        src/core/       Agent ReAct 循环、四层上下文压缩、4 个 LLM provider
⑤ 事件/监控    src/events+webui+report  类型化 EventBus → events.jsonl / WebUI / REPORT.md
```

关键模块：

- **Idea Tree（假设树，持久记忆）**：`src/coordinator/idea_tree.py`。上下文压缩、崩溃都不丢它，因为每次改动都落盘（JSON 规范存储 + Markdown 派生渲染）。
- **Agent（ReAct 引擎）**：`src/core/agent.py`，Coordinator 和 Executor 共用同一个类。
- **上下文管理**：`src/core/context.py`，4 层压缩策略（截断旧工具结果 → 摘要 → …），镜像 Claude Code。
- **LLM providers**：`src/core/llm/`——`claude.py`（Anthropic 原生）、`openai_compat.py`（DeepSeek/vLLM/Ollama）、`openai_responses.py`、`litellm_provider.py`。
- **事件驱动**：`events/` 类型化 EventBus → 持久化到 events.jsonl，dashboard / WebUI / 日志都是订阅者。

### 2.5 关键设计亮点（面试加分点）

1. **实验纪律**：executor 在独立 worktree + 独立分支跑，dev 上迭代、test 上验证，合并阈值可配。main 永不污染。
2. **假设树 = 持久记忆**：把"教训"固化成结构，这是它区别于普通 Agent 的根本。
3. **免密钥集成**：Arbor 自身不调 LLM 时，可以以 MCP / Agent Skill Suite 形式嵌入 Claude Code / Codex，由宿主模型驱动，Arbor 只提供确定性工具。
4. **文献把关**：内置 alphaXiv 检索后端，想法投入算力**之前**先做新颖性审查。
5. **跨 run 学习**：每次运行留下可复用发现，下次 intake 时召回，从经验出发而不是从零开始。

### 2.6 成果数据（记住 3-4 个数字）

| 任务 | 指标 | Claude Code | Codex | **Arbor** |
|---|---|---|---|---|
| BrowseComp | 准确率 ↑ | 53.33 | 50.00 | **67.67** |
| Terminal-Bench 2.0 | 通过率 ↑ | 71.70 | 73.59 | **77.36** |
| Math-Reasoning | gap ↑ | 8.33 | 6.25 | **20.83** |

- MLE-Bench Lite（GPT-5.5）：**86.36% Any-Medal**，77.27% 金奖。
- 单一控制器统一跑六项任务，全部在留出 test 上超过强单智能体基线。
- 论文：arXiv 2606.11926《Toward Generalist Autonomous Research via Hypothesis-Tree Refinement》，人大高瓴 + 微软研究院，构建在 claw-code（Claude Code 的开源 Rust 复现）之上。

---

## 三、我在项目中的工作

### 3.1 工作目标与产出

目标：**对一个工业级 LLM 多智能体框架做系统性的性能与成本优化分析**。

产出三份文档（都在仓库根目录）：

| 文档 | 内容 |
|---|---|
| `OPTIMIZATION_MAP.md` | 架构地图：整个项目画成流程图，把每个优化点"钉"在它所在的位置，讲清楚"它在哪、为什么、改它影响什么" |
| `OPTIMIZATION_FINDINGS.md` | 第一轮：**28 个增量优化点**，分 A 成本 / B 执行流 / C 持久化监控 / D 工具质量四类 |
| `OPTIMIZATION_INNOVATIVE.md` | 第二轮：**27 个结构性创新优化**，归为 **7 大方向**，含优先级路线图 |

### 3.2 方法论（面试重点讲这段）

1. **源码通读建立全局观**：先读 README/论文，再按"CLI → 编排层 → Executor → 核心循环 → 事件监控"的顺序分层读 4 万行代码，画出架构图。
2. **多代理并行探索**：用 4 个并行探索代理分工（LLM 调用层 / Idea Tree 选择策略 / 编排执行流水线 / 上下文记忆层），每个代理独立读代码，我逐一验证其结论——避免单视角遗漏。
3. **证据优先**：每个优化点都精确到 `文件:行号`，不是拍脑袋。比如"上下文压缩破坏缓存"锚定在 `agent.py:317` + `context.py`。
4. **找"先例锚点"证明创新可行**：第二轮的创新不是空想，而是找到代码里"基建已就绪但没用上"的证据（见 3.4）。
5. **按成本×风险排优先级**：分 P0 接线型（零风险）/ P1 低成本 / P2 结构性 / P3 架构型。

### 3.3 发现一：28 个增量优化点（摘要）

**A. LLM 调用层（成本影响最大）**

- **A1 逐祖先串行传播**（`tree_ops.py:564-643`）：每个实验完成后，`propagate_insights` 从父节点到 ROOT 逐个调 LLM。深度 3 的树 = **1 次解析 + 3 次传播 = 4 次 LLM 往返/实验**。→ 可合并成一次调用返回 `{ancestor_id: insight}` 映射。
- **A2 每实验一次 LLM 解析报告**（`executor_io.py:221`）：score 其实正则 `/(?:score|accuracy)[:\s]*([\d.]+)/` 大多能提取，只有 insight/result 才真需要 LLM。→ 正则先试、LLM fallback，干净报告省一次调用。
- **A3 压缩破坏 Anthropic 缓存断点**（`agent.py:317` + `context.py`）：`maybe_compact()` 就地改旧消息，而缓存断点标在最后一条消息上，一改就整段前缀重新按原价上传，**丢约 5 倍缓存读取成本**。→ 压缩前失效/移动断点，或只压缩断点之前的消息。
- **A4 token 估算每轮全量扫描**（`context.py:58-76`）：每次调用前线性扫描全部消息并逐块 `tiktoken.encode`，O(N) 且无缓存。→ 消息 dict 缓存估算值，追加增量、压缩失效。
- **A5 流式路径 usage 为空**（`openai_compat.py:236`）：`create_streaming` 结束 `Usage()` 留空 → token 统计/成本/缓存命中率全丢。→ 加 `stream_options={"include_usage": True}`。
- **A6 Claude token 高估 10-15%**（`claude.py`）：用 tiktoken `cl100k_base` 代理 Claude 真实分词器，高估导致压缩过早触发。→ 按模型族校准或缓存估算。
- **A7 max_tokens 截断恢复双倍计费**（`agent.py:384-393`）：截断后追加 nudge 再调一次 LLM，同一轮成本翻倍（最多 3 次）。

**B. 执行流程层**

- **B1 `_completed_cycles()` 全表扫描**（`executor_run.py:88-98`）：每次派发前线性扫所有节点。→ 增量计数器。
- **B2 收敛检测 O(n log n) 全树扫描**（`convergence.py:145-180`）：每次实验完成重建状态、排序全部 done/merged 节点，N 个实验累计 O(N² log N)。→ 增量维护连续未改善计数。
- **B3 并行收尾用 `gather`**（`executor_run.py:1113`）：等全部完成才返回，1 个跑 60 分钟拖死 3 个 5 分钟的结果。→ `asyncio.as_completed`。
- **B4 Coordinator 在 executor 运行期间完全阻塞**：executor 跑 10-60 分钟，coordinator 干等，无法 IDEATE / 派搜索 / 处理输入。
- **B5 并行实验完成时突发 LLM 调用**：最多 4 parse + 4×depth propagate 同时触发，可能撞 provider 限流。→ semaphore。
- **B6 eval_info 在每个 executor prompt 重复注入**：静态字段重复 20 次。→ 挪进 system prompt。

**C. 持久化 / 事件 / 监控层**

- **C1 IdeaTree 每次 mutation 同步双写 JSON+MD 无防抖**（`idea_tree.py:348`）；且同步版 `add_node/update_node` 不拿锁，与 `async_update_node` 有并发竞态。
- **C2 checkpoint 每轮都写**（`orchestrator.py:639`）：300 轮 = 300 次写循环。→ 防抖。
- **C3 事件管道同步阻塞事件循环**（`file_logger.py:42` + `webui/server.py`）：每次 emit 做 json.dumps + **每事件 flush()** + webui 再序列化一次，全在 orchestrator 的事件循环上。
- **C4 `run_state._recount()` 每事件全表扫描**（`run_state.py:679`）、**C5 WebUI 每 1.5s 全量序列化**、**C6 dashboard 每次重绘重建整棵树**。

**D. 工具层 / 代码质量**

- D1 glob 每文件 stat()、D2 grep 每次 `shutil.which("rg")`、D3 file_edit 非原子写入、D4 PDF/notebook 硬截断、D5 重复导入（`coordinator/main.py:133-140`）、D6 `_ensure_gitignore` 三处重复、D7 `create_subprocess_shell` 注入风险、D9 recall 纯关键词匹配、D10 export 全量读进内存。

### 3.4 发现二：27 个创新优化点（7 大方向）

> 一句话总览：**把 Arbor 从"单一 LLM 文本智能"升级为"语义记忆 + 价值调度 + 流水线并行"的研究系统。**

| 方向 | 从 → 到 | 代表优化 | 核心收益 |
|---|---|---|---|
| ① 语义化记忆 | 文本对比 → 向量检索 | E1 embedding 去重 / E2 语义召回 / E3 跨 run 全局记忆 / E4 ResearchRecall 工具 | 省 10-30% 重复实验；"gradient clipping"能匹配到"exploding gradient mitigation"（现在相似度=0） |
| ② 价值驱动调度 | 墙钟预算 → 期望价值预算 | E5 EI 引导 / E6 分支预算 / E7 价值调度器 / E8 方法家族经济 | 预算集中在高期望分支，省 15-40% 浪费 |
| ③ 流水线化 | 串行等待 → 重叠并行 | E9 投机 IDEATE / E10 持久任务队列 / E11 as_completed / E12 预测压缩 / E13 流式早启 | 40 轮循环省 20-40 分钟墙钟 |
| ④ 成本分层 | 一价全包 → 分级路由 | E14 extraction_model / E15 SearchAgent 模型分离 / E16 结构化输出 / E17-18 传播合并 / E19 小调用缓存 | 50 实验 run 小调用成本从 ~$7.6 降到 ~$0.5（**-93%**）；SearchAgent 每 run 省 ~$10.5 |
| ⑤ 预算质量护栏 | 跑满 timeout → 早停/阶段门 | E20 Yield 早停 / E21 smoke 门 / E22 工具缓存 | 坏实验 5 分钟被掐而非 30 分钟 |
| ⑥ 上下文架构 | 位置截断 → 语义归档 | E23 重要性分级 / E24 分形摘要 / E25 多断点缓存 | 高价值证据跨轮保留，噪音早丢弃 |
| ⑦ 范式变革 | 单线程 ReAct → 状态机/多协调器 | E26 阶段状态机 / E27 多协调器 | 循环纪律可执行；独立方法族并行 |

**关键证据——创新不是空想（"基建已就绪但没用上"，面试最加分）：**

| 创新 | 复用的现成基建（代码里的证据） |
|---|---|
| E9/E10 后台执行 | `search_ctx.py` 的 `_BG_TASKS` + 信号量 + async-safe 写回——**SearchAgent 已实现，executor 没沿用** |
| E10 异步结果注入 | `agent.py:308` 的 `drain_notifications()` 机制就是为此设计 |
| E15 模型分离 | `SearchConfig.agent_model` + `_maybe_override_provider` 已实现，只是默认值 `None` |
| E21 分段预算 | `config.py:28` 的 `BudgetStage`（walltime/data_fraction/promotion_gate）**字段全定义了没接线** |
| E2 语义召回 | `recall.py:9` 官方注释预留 "embedding/LLM judge can replace `_score` later" |
| E13 流式 | 4 个 provider 的 `create_streaming` **全部实现但主循环从不用** |
| E27 自进化 | `trajectory.py` 已导出 RL/SFT 格式轨迹 |

**与第一轮的演进关系**（增量优化是"把现有机制跑快"，创新优化是"换一种机制"）：
- A1（逐祖先串行传播）→ 演进为 **E17/E18**（结构化 + 单遍）
- A3（压缩破坏缓存断点）→ 演进为 **E25**（给摘要块稳定断点）
- B3（gather 阻塞）→ **E11**（渐进回传）+ **E10**（彻底解耦）
- B4（coordinator 阻塞）→ **E9**（投机 IDEATE）+ **E10**（任务队列）

### 3.5 优化优先级与落地路线

| 阶段 | 机会 | 理由 |
|---|---|---|
| **P0 接线型（零风险，当天可做）** | **E21** smoke 门 | `BudgetStage` 字段已定义未接线，纯接线，每次实验省钱省时 |
| | **E15** SearchAgent 模型分离 | 只改默认值，每 run 省 ~$10.5 |
| | **E11** gather→as_completed | 纯收益，不破坏协议 |
| **P1 低成本** | E14 / E16 / E22 / E19 | 复制现成模式、结构化输出、工具缓存 |
| **P2 结构性** | E9/E12/E13 流水线；E1-E3 语义记忆；E5-E8 价值调度；E17/18/25/23/24/20 | 收益大、需引入依赖或行为变化 |
| **P3 架构型（最后）** | E10 → E26 → E27 | 范式切换，高风险高收益，且依赖前面的成果 |

**一句话落地主张**：P0 三件套是零风险接线、当天可做；P2 语义记忆方向是项目预留已久、收益最大的结构性升级；P3 是未来形态。

---

## 四、面试问答准备（Q&A）

### 4.1 项目理解类

**Q：这个项目解决了什么问题？**
A：普通 Agent（Claude Code/Codex）做研究是"一次性滚动"的——实验失败的原因、中途的洞察都随上下文丢失，且没有实验纪律，容易在留出集上过拟合。Arbor 把研究固化成"假设树"：结果和教训持久化并向上传播，想法越来越聪明；executor 在隔离 worktree + dev/test 分离纪律下跑实验，只有留出集验证通过的改进才合并。一句话：**让 AI 的探索可累积、有纪律、可追溯**。

**Q：为什么它有 2.5× 的优势？你怎么看这个数字？**
A：核心来源我认为有三点：① 结构化记忆（假设树）让经验不丢，后续想法有据可依；② 实验纪律（worktree 隔离 + 留出集验证）保证有效改进真的有效、不会过拟合；③ 跨 run 学习让同一 benchmark 越跑越强。当然我理解这个数字依赖具体 benchmark，且是"相同算力预算"下的对比，这也是我做成本优化的原因——**算力预算本身就是变量，优化它的利用率是实打实的收益**。

**Q：它和 Claude Code 什么关系？**
A：**框架上同源、定位不同**。两者都基于 ReAct 工具调用循环，Arbor 甚至构建在 claw-code（Claude Code 的开源 Rust 复现）之上，上下文压缩策略也是镜像 Claude Code 的。但 Arbor 是**研究范式**——用双 Agent + 假设树做累积式探索，而 Claude Code 是通用编码工具。有意思的是 Arbor 也提供免密钥的 Agent Skill Suite，能嵌入 Claude Code 由宿主模型驱动。

### 4.2 技术深度类

**Q：你发现的最高价值优化是什么？为什么？**
A：从**美元成本**看，是 **A1/A2 + E14/E16 这组"小调用合并 + 分层模型"**——每个实验做完要 1 次解析 + N 次祖先传播共 4 次 LLM 调用，全用跟协调器一样的贵模型。我核算过：50 个实验的 run，把这些小抽取调用从贵模型路由到便宜模型，**成本从 ~$7.6 降到 ~$0.5，-93%**；SearchAgent 每 run 再省 ~$10.5。从**墙钟**看，是 B4 的 coordinator 阻塞——executor 跑 10-60 分钟时主管完全空转，改后台并发能省 20-40 分钟/40 轮。
（注：会讲"成本×频率"这个框架——`优化收益 = 调用次数 × 单次成本 × 是否在热路径`。）

**Q：上下文缓存是怎么回事？为什么压缩会破坏它？**
A：LLM 对话按**前缀缓存**计费——Anthropic/OpenAI 缓存你发过的一段前缀，相同前缀下次只收约 1/5。Arbor 把缓存断点标在最后一条消息，靠"每次只新增一点点"持续命中。但 `maybe_compact` 压缩时会**就地截断/摘除/去重旧消息**，一改前缀缓存就失效，下一轮整段按原价重传——我估的损失是约 5 倍缓存读取成本。解法 E25 是把第 4 个缓存断点放在"压缩摘要块"上做稳定锚，只有易变尾部重算。

**Q：ReAct 循环你怎么理解？Arbor 的循环有什么特殊？**
A：ReAct 就是"推理 + 行动交替"：LLM 每轮输出决策，是调工具就执行再回到推理，否则结束回合。Arbor 的特殊之处：① Coordinator 是**单一持久**的 ReAct Agent，六步循环不是代码循环，而是 prompt 约定 + 工具门控；② 唯一的循环级护栏是 `_completed_cycles` 计数；③ 这意味着循环纪律只存在于文本里、代码没强制——这正是我 E26 状态机的动机。

**Q：你会先实施哪个优化？怎么验证收益？**
A：P0 里先做 **E21 smoke 门**——它把项目里已定义未接线的 `BudgetStage` 接上，坏实验 5 分钟被掐、省 85% 预算还池，零新依赖。验证方式：同一 benchmark 跑 A/B，对比 ① 每实验墙钟中位数 ② 每 run 总 token/成本 ③ 成功实验占比，smoke 门应该提高三者的利用率而**不损失最终分数**。任何优化我都要求"不影响合并质量"作为硬约束。

**Q：这个系统的并发模型有什么问题？**
A：Coordinator 和 Executor 在**同一个 asyncio 事件循环**里，`wait_for(agent.run())` 让 coordinator 干等 10-60 分钟；并行收尾用 `gather` 等全部完成；而且事件管道的 I/O（日志 flush、webui 序列化）也同步阻塞在这个循环上。更严重的是并行实验完成时 4 parse + 4×depth propagate 会**瞬间突发 LLM 调用**，可能撞 provider 限流。项目里其实已有解——SearchAgent 的 `_BG_TASKS` 后台模式就是先例，executor 没沿用。

### 4.3 方法论类

**Q：4 万行代码你怎么保证分析全面、结论可靠？**
A：三层保障：① **分层读**——先论文/README 建立全局观，再按 CLI→编排→Executor→核心→监控的顺序读，画架构图时每个模块都落到实际文件；② **多代理并行**——4 个探索代理各自分工独立读代码，我再逐个核对，防止单视角盲区；③ **证据锚定**——每个结论都带 `文件:行号`，比如 A3 锚在 `agent.py:317`。结论里我还区分了"确认的"（如 D5 重复导入）与"建议的"（如 B4 结构性改动需确认副作用）。

**Q：你怎么判断哪些优化"值得做"？**
A：用三个维度打分：**收益**（省多少美元/墙钟/每次实验都走吗=热路径吗）、**风险**（会不会破坏协议、崩溃恢复、并发语义）、**基建就绪度**（代码里有没有现成但没接线的零件）。所以我排出来的 P0 是"零风险接线"而非"收益最大"——E21/E15/E11 都是基建已备好，只是没人接上。

### 4.4 开放题

**Q：如果让你现在动手改这个项目，一天内你会做哪三件事？**
A：① 接上 E21 smoke 门（BudgetStage 已定义，5 分钟坏的实验不浪费 85% 预算）；② 把 SearchConfig.agent_model 默认值填上，让 SearchAgent 用便宜模型（省 ~$10.5/run）；③ 把并行收尾的 `gather` 换成 `as_completed`，让 coordinator 第一时间看到已完成实验的结果。这三件都零协议改动、当天可验证。

**Q：你从这项目里学到什么？**
A：三点：① **成本优化优先看"频率 × 单次成本"**——解析报告这种小调用，每次实验都发生，攒起来比一次大调用更值钱；② **"基建就绪但没接线"是代码库里的金矿**——项目作者常常预留了接口却来不及用，发现它们意味着你对架构的理解到位了；③ **多智能体系统的瓶颈往往在并发与状态一致性**，而不是单次 LLM 能力——Arbor 的阻塞、突发限流、双写竞态都印证了这点。

---

## 五、怎么用这份材料

1. **背熟开场稿（30 秒版）**——面试自我介绍用它。
2. **项目介绍部分当"地图"**——被问到任何架构/机制问题时，先答"它属于六步循环/哪一层"，再展开细节。
3. **我的工作部分是你的"能力证明"**——重点讲 3.2 方法论和 3.4 的"先例锚点"，这体现你不只是读了代码，而是会系统分析。
4. **Q&A 部分挑高频题练两遍**——尤其"最高价值优化"和"你先做哪个"这两题，几乎必问。
5. **数字是弹药**：40,000 行、55 个优化点、4 次 LLM 调用/实验、-93% 小调用成本、$10.5/run、20-40 分钟/40 轮、2.5×。

---

*若某段被追问细节，回仓库翻对应文档：`OPTIMIZATION_MAP.md`（架构图）、`OPTIMIZATION_FINDINGS.md`（28 个增量点）、`OPTIMIZATION_INNOVATIVE.md`（27 个创新点）。*
