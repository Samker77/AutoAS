# 基线运行分析报告（对照组）

> 会话：`run_20260812_092700` ｜ 任务：baseline_algotune（k-NN AlgoTune speedup）
> 框架：`214c5f5`（Windows worktree 修复）+ **不含 A4** ｜ Provider：qwen3.8-max / DashScope
> 运行时间：2026-08-12 09:27 → 10:44，正常退出（`exit_reason: ok`）

---

## 一、运行概况

| 指标 | 数值 |
|---|---|
| 总时长 | **4656s（77.6 分钟）** |
| LLM 调用 | 253 次（agent 229 + 解析/洞察 24） |
| 输入 tokens | **7,718,728** |
| 输出 tokens | **181,902** |
| 其中缓存读取 | 7,352,320（**缓存命中率 95.3%**） |
| 非缓存输入 | 366,408（4.7%） |
| 事件数 | 1,570 |

### Token 成本分布（按工作目录）

| 角色 | 调用 | 输入 | 其中缓存命中 | 输出 |
|---|---|---|---|---|
| **Coordinator（MAIN）** | 77 | **4,413,934（57%）** | 4,267,776（96.7%） | 62,972 |
| Executors（6 个 worktree，合计） | 152 | 3,295,742 | 3,084,544 | 110,478 |
| 解析/洞察辅助 | 24 | 14,398 | 0 | 12,963 |

> 注意：**coordinator 自己消耗了 57% 的输入 tokens**（4.4M/7.7M）。它要反复把整棵 idea tree、全部 findings、长上下文喂给 LLM。这是 A4（增量 token 估算缓存）主要的落点之一。

### 各 executor 成本

| 节点 | 状态 | 分数 | 轮次 | tokens | 时长 |
|---|---|---|---|---|---|
| 1.1 f64 GEMM + argpartition | done | 10.71x | 21 | 359K | 262s |
| 2.1 two-stage f32 级联 | needs_retry | — | 30 | 627K | 390s |
| 1.2 q_sq 跳过（partial assembly） | needs_retry | 14.9x* | 30 | 676K | 534s |
| 2.2 f32 级联重提议 | needs_retry | 12.9x* | 30 | 939K | 1405s |
| 4.1 augmented GEMM | done | 16.8x | 13 | 184K | 231s |
| 3.1 ctypes dgemm 融合 | needs_retry | ~16x* | 30 | 612K | 460s |

\* = executor 自测分数；coordinator 用 3 轮交错 A/B 复核后决定合并/裁剪。

---

## 二、最终成果

**1.0x → 18.23x（test 验证），全部约束遵守。**

| 里程碑 | dev | test（验证） |
|---|---|---|
| 基线（参考实现的朴素副本） | ~1.0x | 0.94x |
| Merge 1.1：f64 GEMM 展开 + `argpartition` | 10.7–12.2x | **15.29x** |
| Merge 3.1：ctypes `cblas_dgemm` α/β 融合 + buffer 复用 | ~16x | **18.23x** |
| 最终 trunk（独立样本） | 最高 23.6x | 15.83 / 18.23 / 18.57 |

### 合并了什么（coordinator 自主发现）

1. **1.1**：`|x−y|² = |x|² − 2x·y + |y|²` 代数重构，用单个 BLAS GEMM 替代 51MB 的 broadcast diff 张量；`argpartition` 替代全量 `argsort`。
2. **3.1**：绕过 numpy，直接 ctypes 调用 numpy 自带的 OpenBLAS（`scipy_cblas_dgemm64_`，ILP64），用 `alpha=-2, beta=0` 把缩放融进 GEMM 结尾；shape-keyed 可复用 buffer 消除每次调用的 3.2MB 分配；同时丢弃行常量（排序无关）的 `|q|²` 项。DLL 缺失时优雅回退到 numpy matmul。

### 被否定的方向（有实测证据）

- **f32 粗筛 + f64 精修级联**：中位慢 ~11%（转换/gather/精修 > f32 GEMM 收益）→ 2.1、2.2 均裁剪。
- **augmented K=dim+2 单 GEMM 距离构造**：慢 6–10% → 4.1 裁剪。
- **单独砍 `|q|²` pass**：中性 → 1.2 裁剪（executor 自测 14.9x，coordinator 3 轮交错实测为中性，未合并）。

---

## 三、约束验证

- **只改 solution.py**：`task.py`/`eval.py`/`eval.sh` 零 diff（已验证）。
- **无 metric gaming**：无线程 env 覆盖、无结果缓存；buffer 复用仅限 scratch。
- 每次 eval 都通过 `is_solution`。
- 测试门槛：合并时必须通过独立 `eval.sh test` 验证（test_trunk_score 18.23）。

---

## 四、过程中的问题

### 1. `Commit failed: ... modified: solution.py`（auto_commit 偶发失败）

已在对话中定位：这是 `experiment.py:199` 的 `git add` **返回码被丢弃**导致的偶发问题（大概率并行 executor + coordinator 合并时的 index.lock 竞争）。复现证明 git 序列本身完好。

**影响评估：良性。**
- 不阻塞运行（进程持续到 10:44 正常退出）。
- 不丢工作：`_finalize_worktree` 在 worktree 移除前重新 stage+commit；6 个 executor 的已提交工作全部保留（1.2/2.2 树内标记 "Implementation fully committed"）。
- 不影响 A/B：基线指标来自 eval 分数 + 事件日志，不依赖 auto_commit 成败。

### 2. 3/6 executor 撞 30-turn 上限

1.1、4.1 正常完成；2.1、1.2、2.2、3.1 都在实现已提交后陷入 A/B 微基准侧实验，没交最终报告。coordinator 的恢复手段：直接 inspect 分支 diff → coordinator 用交错 eval 复核 → 手工收尾。这是框架的一个真实待优化点（executor 收敛性）。

### 3. 主机噪声

eval 输出的"加速比"被参考腿的噪声污染（同一代码基线 0.88–1.01x 波动）。coordinator 学会了用**交错运行内的绝对 solution 时间**做对比（T,A,B,T,A,B… 3-5 轮），这对后续优化组同样成立。

---

## 五、对 A/B 对比的意义（对照组基准线）

以下是优化组（A4：增量 token 估算缓存）要对比的基线数字：

| 指标 | 基线（本次） |
|---|---|
| 总 wall-clock | **4656s** |
| 输入 tokens | 7,718,728 |
| 输出 tokens | 181,902 |
| LLM 调用 | 253 |
| 缓存命中率 | 95.3% |
| trunk dev / test | 16.4x / 18.23x |

### A4 预期影响（诚实评估）

`_estimate_message_tokens` 每次 `maybe_compact` 都会对**整个消息列表**重新 tokenize。本次运行按 API 输入量 ~7.7M tokens 估算，tiktoken（cl100k）速度 ~2.6M tokens/s，tokenizer 总耗时约 **3–5 秒**（占 4656s 的 ~0.1%）。因此：

- **A4 在本任务规模下的 wall-clock 节省很小**（秒级），A/B 会如实测出并报告。
- A4 的价值是**算法性的**：把每次调用从 O(全量上下文) 降到 O(新增消息)。在上下文更大（>100K）、tokenizer 更慢（非 cl100k）、消息更长的场景下收益线性放大（见 `bench_a4.py` 微基准）。
- 正确性验证是 A/B 的首要目标：**cached == fresh 位一致、token 估算不漂移、checkpoint/序列化无泄漏**。

---

## 六、会话产物

- `D:\Agent\AI-Scientist\Arbor-main\src\core\experiment.py` `_run_cmd` 的 git 修复（commit `214c5f5`）
- 分析脚本：`D:\Agent\bench\analyze_run.py`、`repro_autocommit.py`、`repro_worktree.py`
