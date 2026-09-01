<h1 align="center">🌳 AutoAS</h1>

<h3 align="center">AutoAS · 自主科研实验系统 · 任务规划 → 实验运行 → 数据分析 → 反馈迭代 的完整闭环</h3>

<p align="center">
  <a href="https://github.com/your-team/AutoAS"><img src="https://img.shields.io/badge/Code-GitHub-181717?style=for-the-badge&logo=github&logoColor=white" alt="GitHub"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-Apache_2.0-D22128?style=for-the-badge&logo=apache&logoColor=white" alt="License: Apache 2.0"></a>
  <a href="https://arxiv.org/abs/2606.11926"><img src="https://img.shields.io/badge/Base_Paper-arXiv-B31B1B?style=for-the-badge&logo=arxiv&logoColor=white" alt="Base paper"></a>
  <a href="https://dashscope.aliyun.com/"><img src="https://img.shields.io/badge/Base_Model-Qwen%40百炼-7A3EFF?style=for-the-badge&logo=alibabacloud&logoColor=white" alt="Qwen via 百炼"></a>
</p>

<p align="center">
  <i>把「一个科研目标 + 一个可量化指标」交给 AutoAS，它会自主完成
  <b>任务规划与实验设计 → 实验运行与数据获取 → 数据分析与反馈迭代</b> 的三段式闭环，
  用实验结果改变下一轮的计划，逐步提升实验成效。</i>
</p>

---

## 🏆 核心成果：V2 相对 V1 在同一任务上的提升

> 任务：**APTOS 2019 糖尿病视网膜病变分级**（5 类严重度 0–4）
> 指标：quadratic weighted kappa（保留测试集）｜基座模型：**Qwen（qwen3.8-max，阿里云百炼）**
> 完整实验记录见 [`result/V2_APTOS_RESULTS.md`](result/V2_APTOS_RESULTS.md)

| 维度 | V1 (Arbor-main) | **V2** | 差异 |
|---|---|---|---|
| **测试集 kappa** | 0.89643 | **0.92264** | **+0.0262（跨过 SILVER 档线 0.9197）** |
| 档位 | 未达银牌 | **SILVER** | 跨档 |
| **运行时长** | 8h16m41s | **4h08m49s** | **V2 快约 50%** |
| **LLM 错误** | 10 | **0** | **完全消除** |
| 未缓存 token | 1.25M | **815K** | **−35%** |
| 想法数 | 22 | 28 | 探索更深 |
| 合并（merged） | 3 | 4 | 更高效的收敛 |

**一句话结论**：V2 用 **一半的时间、零 LLM 错误、更深的假设树**，把测试集 kappa 从 0.89643 提升到 **0.92264（SILVER，接近 gold 档 0.9305）**——同样的基座模型、同样的计算预算，成绩跨了一个档位。

---

## 🔁 三段式闭环：实验结果如何改变下一轮计划

Arbor V2 的核心不是"一次生成方案"，而是把整个科研流程做成一个**可自动运行、可量化验证的闭环**：

```
┌─────────────────────────────────────────────────────────────────────────┐
│  ① 任务规划与实验设计                                                       │
│     协调器（Coordinator）解析目标，在「想法树」上生成并筛选假设，               │
│     规划下一轮要实验的任务清单                                                 │
└─────────────────────────────────────────────────────────────────────────┘
                                  │ 派发
                                  ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  ② 实验运行与数据获取                                                       │
│     执行器（Executor）在隔离的 git worktree 中实现代码、运行真实实验，          │
│     在 dev 集迭代、在保留测试集（B_test）验证，产出分数与提交物                  │
└─────────────────────────────────────────────────────────────────────────┘
                                  │ 返回分数 / 失败 / 洞察
                                  ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  ③ 数据分析与反馈迭代                                                       │
│     收敛检测器（ConvergenceDetector）判定分数平台期；反向传播把失败教训            │
│     和成功路径写回想法树；协调器据此决定：继续 / 合并 / 剪枝 / 换方向 / 停止      │
└─────────────────────────────────────────────────────────────────────────┘
                                  │ 带着「下一轮该改什么」回到 ①
                                  └───────────────↺
```

**示例：实验结果确实改变了下一轮计划**（APTOS 任务中 4 个 merged 想法的真实轨迹）：

| 轮次 | 想法（节点） | Dev κ | 做了什么 |
|---|---|---|---|
| R1 | 1.1.1.1 | 0.9083 | EfficientNet-B3@384 + circle-crop 建立 baseline 管线 |
| R2 | 2.1.1.1 | 0.9098 | 上一轮 B3 达标 → 加 5-fold CV + OOF 阈值拟合 |
| R3 | 3.2.1.1 | 0.9279 | 上一轮 CV 有效 → 引入 ConvNeXt-Base 做 2-way blend |
| R4 | 3.3.1.1 | 0.9274 | 上一轮 blend 有效 → 加 Swin-Base 做 3-way blend，OOF 0.9324 |

**负向发现同样被保留并写入 ROOT insight 复用**（`B5@456 反而不如 B3@384`、`8-view TTA 没有超过 4-view`、`single-fold 阈值搜索会过拟合`、`Nelder-Mead 抛光提升 OOF 但 hurt B_test`）——实验得出的"此路不通"也成为下一轮规划的依据，避免重复踩坑。

---

## ⚙️ V2 的关键技术改进（本仓库源码内）

### 1. 收敛引擎 v2：score-gated idle clock（`src/core/agent.py`）

- **问题**：V1 中执行器在提交了可工作的代码后，会继续在旁支 A/B 实验上空转，既不收敛也不产生价值。
- **V2 方案**：只有当一次提交**伴随评测分数提升**（`_EVAL_SCORE_RE` 从 `bash eval.sh` 输出中抽取 `score:/accuracy:`）时，才重置空闲时钟；否则空闲累计，触发收敛提示。协调器自身永不提前收敛。
- **效果**：在 APTOS 任务上 **LLM 错误从 10 → 0，运行时长减半**（8h16m → 4h08m），探索更深（28 vs 22 想法，4 vs 3 merged）。

### 2. A4 增量 token 缓存（`src/core/context.py`）

- 每条消息的 token 预估（`_est_tokens`）按内容引用缓存，只有内容真正变化才重算——在长上下文、多轮迭代的科研场景下显著降低重复计费与延迟。
- **效果**：未缓存 token **−35%**（1.25M → 815K），输入 token −3.5M。

> 注：本仓库为当前上传的 V2 快照（含收敛引擎 v2 与 A4 缓存）。L1/L2/i18n 等后续优化位于开发分支，随迭代合入。

---

## 🧩 框架原理

Arbor 由两个协同的智能体组成，重复执行六步 **arbor cycle**：

- **协调器（Coordinator）** — 研究总监：维护想法树、驱动循环、派发实验、依据证据决策。
- **执行器（Executor）** — 研究工程师：实现一个想法、在隔离 worktree 跑实验、汇报证据。

```
① OBSERVE  观察当前结果与失败模式
② IDEATE   基于分析和树内洞察提出 1–3 个新假设
③ SELECT   平衡「当前最优方向」与「未探索备选」，选出最值得测的
④ DISPATCH 派发独立执行器在隔离 worktree 中实现并评估（dev 集）
⑤ BACKPROP 记录分数、洞察、失败；把教训向上抽象给祖先与未来想法
⑥ DECIDE   依据保留测试集验证决定：继续 / 合并到 trunk / 剪枝 / 停止
```

**想法树（Idea Tree）** 是记忆的核心：每一轮的结果、失败模式、提炼的洞察都保存在树中并向根节点传播，让后续想法"从上次的经验出发"而不是从零开始。**Git 纪律**：每个执行器在独立 worktree/分支工作，`main` 在满意前始终不被污染，验证过的改进才合并进 `trunk`。

---

## 👥 给组员：跑 MLE-Bench Lite 其他任务

跑 V2 跑其他 MLE-Bench Lite 任务（环境、Qwen 百炼配置、任务目录结构、启动命令、注意事项）请看：

👉 **[`docs/V2_MLE_LITE_RUN_GUIDE.md`](docs/V2_MLE_LITE_RUN_GUIDE.md)** — V2 快照说明 + 快速启动 + 避坑清单

跑完任务后如何写报告（8 个必填板块 + 汇总表 + 两条红线）见：

👉 **[`docs/组员报告要求.md`](docs/组员报告要求.md)** — 报告模板与提交要求

---

## 🚀 快速开始

**环境要求**：Python ≥ 3.10 + Git。

```bash
# 安装
pip install -e .          # 或: uv pip install -e .
arbor doctor              # 检查 PATH / git / API keys

# 配置基座模型（Qwen 走阿里云百炼 DashScope，openai-chat 兼容端点）
arbor setup

# 在基准目录上启动一次研究
arbor --cwd ./benchmark --config research_config.yaml
```

最小配置示例（`research_config.yaml`）：

```yaml
task: >
  优化智能体在该基准上的指标（quadratic weighted kappa / accuracy）。
  不得修改评估框架或数据文件。

coordinator:
  max_cycles: 10          # arbor cycle 轮数
  max_depth: 3            # 想法树深度
  merge_threshold: 0.5    # 合并到主干所需的留出集最低提升
  ui:
    interaction_mode: auto   # auto | direction | review | collaborative

executor:
  max_turns: 100
```

运行中可用 `/status`、`/tree`、`/evidence`、`/branches`、`/cost`、`/report`、`/abort` 控制与查看。

---

## 🧰 常用 CLI

| 命令 | 功能 |
|---|---|
| `arbor` | 启动交互式研究会话 |
| `arbor --continue` | 继续上一段未完成的规划对话 |
| `arbor replay --demo` | 回放内置示例运行，无需 API key |
| `arbor report <session>` | 重新渲染某次会话的 REPORT |
| `arbor idea-check "<想法>"` | 对照 alphaXiv 做新颖性 / 先行工作审查 |
| `arbor web <session>` | 打开只读浏览器监控 |
| `arbor --resume --run-name <name>` | 断点恢复一次运行 |

---

## 🗂️ 项目结构

```
src/                 # `arbor` 包
├── core/            共享基础设施：ReAct 循环、工具、LLM 提供方、上下文管理
│   ├── agent.py     执行器 ReAct 循环 + V2 收敛引擎（score-gated idle clock）
│   └── context.py   上下文管理 + A4 增量 token 缓存
├── executor/        Executor 智能体 + executor CLI
├── coordinator/     Coordinator、Idea Tree、收敛检测器、orchestrator
├── cli/             arbor CLI：intake、实时仪表盘、setup、doctor、config
├── events/          类型化事件总线与载荷
├── report/          报告生成
├── webui/           只读运行监控 Web 服务器
├── plugins/         领域插件（mle_kaggle 等）
└── skills/          按需加载的 Markdown 手册
result/              实验结果（V2 vs V1 对照，含完整 APTOS 记录）
```

---

## 📚 致谢与引用

本仓库构建于开源项目 **Arbor**（[RUC-NLPIR/Arbor](https://github.com/RUC-NLPIR/Arbor)）之上，并针对"任务规划 → 实验运行 → 数据分析 → 反馈迭代"的科研闭环做了 V2 优化（收敛引擎 + 增量 token 缓存）。Arbor 的 CLI 框架建立在开源项目 [claw-code](https://github.com/ultraworkers/claw-code) 之上。

基座模型使用 **Qwen 系列**，通过 **阿里云百炼（DashScope）** 调用。

```bibtex
@misc{jin2026arbor,
  title  = {Toward Generalist Autonomous Research via Hypothesis-Tree Refinement},
  author = {Jiajie Jin and Yuyang Hu and Kai Qiu and Qi Dai and Chong Luo and
            Guanting Dong and Xiaoxi Li and Tong Zhao and Xiaolong Ma and
            Gongrui Zhang and Zhirong Wu and Bei Liu and Zhengyuan Yang and
            Linjie Li and Lijuan Wang and Hongjin Qian and Yutao Zhu and Zhicheng Dou},
  year   = {2026},
  eprint = {2606.11926},
  archivePrefix = {arXiv}
}
```

---

## 📄 License

[Apache License 2.0](LICENSE)
