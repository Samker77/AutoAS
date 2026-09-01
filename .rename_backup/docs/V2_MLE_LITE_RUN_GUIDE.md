# AutoAS V2 · MLE-Bench Lite 任务运行指南

> 给跑任务的组员：本仓库是 **V2 快照**。你们已经跑过原始 autoas，这里只讲 **V2 改了哪里**、**如何快速起一个新 MLE-Bench Lite 任务**、**有哪些坑**。

---

## 0. 先搞清楚：V2 和你们之前跑的原版有什么区别

| 改动 | 位置 | 作用 | 你们能观察到的差别 |
|---|---|---|---|
| **收敛引擎 v2**（score-gated idle clock） | `src/core/agent.py`（`_EVAL_SCORE_RE`） | 执行器提交了可工作的代码后，不再允许它无限在旁支 A/B 实验上空转；**只有伴随评测分数提升的提交才重置空闲时钟**，否则触发收敛提示 | 无提升的分支**更早收敛/停手** → 更省 token、更省时间；同一任务跑得更快（APTOS 实测 8h16m → 4h08m，LLM 错误 10 → 0） |
| **A4 增量 token 缓存** | `src/core/context.py`（`_est_tokens`） | 每条消息的 token 预估按内容引用缓存，内容没变就不重算 | 长上下文多轮迭代时**更便宜、更低延迟**（未缓存 token −35%） |

> ⚠️ 本仓库**不包含** L1/L2/i18n（并行批派发、后台执行器门禁、WebUI 中文化）——那些在开发分支，等合入后再单独通知。**对比实验请基于本快照**，不要混用。

---

## 1. 环境准备（一次性）

```bash
# 需要：Python ≥ 3.10、git、GPU 可选
git clone https://github.com/Samker77/AutoAS-Scientist.git
cd AutoAS-Scientist
python -m venv .venv && source .venv/bin/activate   # 或复用你们已有的 autoas venv
pip install -e .
autoas doctor        # 验证 PATH / git / API keys 就绪
```

> 如果你之前已经装过原版 autoas：**先 `pip install -e .` 重装到本快照**，再 `autoas doctor` 确认。

---

## 2. 配置基座模型：Qwen（阿里云百炼 DashScope）

在一个任务的 `research_config.yaml` 里写：

```yaml
# research_config.yaml（放在你的任务目录下）
llm:
  provider: openai-chat        # DashScope 的兼容端点走 openai-chat
  model: qwen3.8-max           # ← 换成百炼上你可用的 qwen 系列模型 id
  base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
  api_key: sk-你的百炼Key     # 或省略，用环境变量 DASHSCOPE_API_KEY
```

> 模型 id 以阿里云百炼控制台实际可用的为准。`qwen3.8-max` 是此前 APTOS 实验用的 id，如果账号里不可用就换成当前可用的 qwen 系列（如 `qwen-max` / `qwen-plus`）。

---

## 3. 准备一个 MLE-Bench Lite 任务目录

每个任务 = **一个独立的、干净的 git 仓库目录**，里面必须有：

```
<task_dir>/
├── eval.sh                  # 评分脚本：打印 "score: <数字>"（必须！插件靠这个行判断分数）
├── description.md           # 任务说明：指标、提交格式、数据描述（intake 会读它）
├── data/                    # 只读数据（受保护，agent 不许改）
│   └── sample_submission.csv
├── research_config.yaml     # 上面的 llm 配置 + 下面的 plugin 配置
└── (干净的 git：git init 后 baseline commit)
```

参考模板：`autoas-zoo/_template/`（有 `eval.sh` / `eval.py` / `README.md` / `task.py` 的最小骨架）。

> 插件通过 `eval.sh` 拿分数，**不要**让 agent 直接跑 `run_eval.py`；`data/` 被插件列为只读保护路径。MLE-Bench Lite 的标准 harness 会提供 `eval.sh` + 数据，直接解压放进来即可。

---

## 4. 启动任务的 `research_config.yaml`（完整版）

```yaml
# research_config.yaml
plugin: mle_kaggle            # 一行切换到 Kaggle/MLE 竞赛模式
plugin_profile: mle_bench_lite  # 预设：max_cycles 20 / 树深 4 / 单执行器 4h / 总预算 24h

llm:
  provider: openai-chat
  model: qwen3.8-max
  base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
  api_key: sk-你的百炼Key

ui:
  interaction_mode: auto       # auto 全自动；想盯着可 review

# 可选：覆盖预设预算
# max_cycles: 12
```

---

## 5. 启动运行

```bash
cd <task_dir>

# 方式 A：交互式（会先和 intake 聊目标，确认 Research Contract 后开始）
autoas

# 方式 B：无头直接跑（推荐在服务器/nohup 下用）
autoas --yes "maximize the score from eval.sh without touching data/ or eval.sh" \
      --yes-cwd . \
      --run-name v2-<task名> \
      --no-dashboard-input
```

跑起来后：

- **浏览器实时监控**：`autoas web <run_name>`（或启动时自动打开，端口约 8765）
- **终端状态**：运行中 `/status` `/tree` `/evidence` `/branches` `/cost` `/report`
- **中断恢复**：`autoas --resume --run-name <run_name>`（从想法树断点继续）

---

## 6. 结果去哪了

```
<task_dir>/.autoas/sessions/<run_name>/
├── REPORT.md                 # 最终报告（分数、合并轨迹、失败）
├── COORDINATOR_FINAL_REPORT.txt
├── events.jsonl              # 全量事件（可回放/审计）
├── run_stats.json
└── .coordinator/idea_tree.*  # 想法树快照
```

`submission.csv` 会由 agent 写回任务目录根（插件要求 `required_outputs: submission.csv`）。

**提交证据**：跑完后把 `REPORT.md` 发给组长，或在 `result/` 下按 `V2_<任务名>_RESULTS.md` 的格式补一份记录（参考现有的 `result/V2_APTOS_RESULTS.md`）。

---

## 7. ⚠️ 注意事项（务必看）

1. **公平性 / 口径**：和之前 A/B 对比保持一致——基座模型都用 Qwen（百炼）、都从同一 cleaned 任务目录起步。**别混用不同模型**，否则 20 任务对照失真。
2. **`eval.sh` 必须打印 `score: 数字`**（`score` 或 `accuracy` + `:` 或 `=`）。不打印分数 → 收敛引擎读不到分 → 收敛门失效。
3. **`data/` 只读**：插件把 `data/`、`private/`、`evaluation/` 列为受保护路径，agent 不该改；你也不要把测试集塞进 `data/` 之外可写的地方。
4. **每任务独立 git 仓库**：启动前必须是干净的 git（baseline commit），运行中 `main` 不被污染，验证过的改进才进 `trunk`。
5. **预算节奏**：`mle_bench_lite` 预设 24h 总预算 / 单执行器 4h。插件提示 agent 前 10-15% 做 baseline + 验证协议、最后 10% 做 finalize（保留最佳 submission）。**别在最后 30 分钟开新训练**。
6. **`scripts/mle/` 缺失不影响运行**：插件引用了 `scripts/mle/setup_workspace.sh` 等，本仓库没有这些脚本，代码会**静默跳过**（已确认），只少两个便利钩子，不报错。
7. **收敛引擎副作用**：它会让"无提升的分支"更早停手。如果某个分支其实还在缓慢提升但分数没到 `improvement_threshold`（0.001 相对提升），可能被过早收敛——这是**有意的**权衡（省预算），真遇到可以调 `coordinator.convergence.improvement_threshold` 或重启时用 `--resume` 继续盯。
8. **多任务并行**：不同任务目录之间**完全独立**（各自 git、各自 session）。一台机器同时跑 2-3 个没问题，但注意 GPU 争抢——插件建议并行的便宜消融别和重训练抢同一块卡。

---

## 8. 常见问题

| 现象 | 原因 / 处理 |
|---|---|
| `autoas doctor` 报 API key 缺失 | `autoas setup` 配好 Qwen 端点 + key，或写进任务 `research_config.yaml` |
| 跑起来一直卡在 intake | 检查 `base_url` / `model` 是否可用；先 `curl` 一下 DashScope 兼容端点确认连通 |
| 分数一直没提升但还在烧钱 | 收敛引擎正常表现；`/tree` 看是否有节点在 needs_retry，可 `--resume` 后人工给方向 |
| eval 崩了 / 格式错 | 插件会教 agent 读报错修 submission；**别手动改数据** |
| 想中途换方向 | `--interaction-mode review` 或 `direction` 启动，人为把关下一轮规划 |
