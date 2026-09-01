<h1 align="center">🧪 AutoAS</h1>

<h3 align="center">基于 Qwen 的科学实验任务规划与反馈迭代系统</h3>
<h3 align="center">任务规划 → 实验运行 → 数据分析 → 反馈迭代 的完整闭环</h3>

<p align="center">
  <a href="https://github.com/Samker77/AutoAS"><img src="https://img.shields.io/badge/Code-GitHub-181717?style=for-the-badge&logo=github&logoColor=white" alt="GitHub"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-Apache_2.0-D22128?style=for-the-badge&logo=apache&logoColor=white" alt="License: Apache 2.0"></a>
  <a href="https://arxiv.org/abs/2606.11926"><img src="https://img.shields.io/badge/Base_Paper-arXiv-B31B1B?style=for-the-badge&logo=arxiv&logoColor=white" alt="Base paper"></a>
  <a href="https://dashscope.aliyun.com/"><img src="https://img.shields.io/badge/Base_Model-Qwen%40百炼-7A3EFF?style=for-the-badge&logo=alibabacloud&logoColor=white" alt="Qwen via 百炼"></a>
</p>

---

## 🏆 核心成果

> 2026 年度中国青年科技创新"揭榜挂帅"擂台赛 · 阿里云榜题  
> 赛道一 · 科学发现 · 方向 1B | 题目编号 XH-202619

### MLE-Bench Lite 整体战绩

| 指标 | 数值 |
|---|---|
| **任务总数** | 20 个（图像 8 / 文本 6 / 音频 2 / 表格 4） |
| **有效提交率** | **20/20 = 100%** |
| **奖牌率（分母 18）** | **14/18 = 77.8%** |
| **🥇 Gold** | 10 个（55.6%） |
| **🥈 Silver** | 4 个（22.2%） |
| **🥉 Bronze** | 0 个 |
| **❌ 无牌** | 4 个 |
| **⚠️ 仅 dev 分未核验** | 2 个（dog-breed / dogs-vs-cats，不计入奖牌率分母） |

> 注：分母取 18 = 20 − 2 个仅本地 dev 分而无官方 test 复评的任务；奖牌阈值依据 MLE-Bench 官方 grader 输出或 leaderboard 明确记录。其中 2 个奖项（Russian/Whale）当前为 dev 分，需 `grade.py` 官方复评。

### 代表任务亮点

| 任务 | 模态 | 官方指标 | 最终分数 | 档位 | 备注 |
|---|---|---|---|---|---|
| **APTOS 2019** | 图像 | quadratic weighted kappa | 0.92264 | 🥈 Silver | 跨银线 0.9197 |
| **aerial-cactus-identification** | 图像 | ROC AUC | **1.0000** | 🥇 Gold | AUC 满分 |
| **denoising-dirty-documents** | 图像 | pixel RMSE ↓ | 0.007333 | 🥇 Gold | ≤金线 0.01794 |
| **detecting-insults-in-social-commentary** | 文本 | ROC AUC | 0.960734 | 🥇 Gold | ≥金线 0.83321 |
| **Histopathologic Cancer Detection** | 图像 | ROC AUC | 0.99639 | 🥇 Gold | V2 提升 0.00167 |
| **Jigsaw Toxic Comment** | 文本 | ROC AUC | 0.98762 | 🥇 Gold | ≥金线 0.98740 |
| **Transparent Conductors** | 表格 | mean RMSLE ↓ | 0.05215 | 🥇 Gold | V1 Silver→V2 Gold |
| **mlsp-2013-birds** | 音频 | ROC AUC | 0.93541 | 🥇 Gold | 完整三段式闭环案例 |
| **leaf-classification** | 图像 | multi-class log loss ↓ | 0.00622 | 🥈 Silver | V2 0.00725→0.00622 |
| **random-acts-of-pizza** | 文本 | ROC AUC | 0.7944 | 🥈 Silver | ≥银线 0.76482 |
| **Right Whale Redux** | 音频 | ROC AUC | 0.9665 | 🥈 Silver\* | dev 分待官方复评 |
| **TPS Dec 2021** | 表格 | accuracy | 0.96334 | 🥇 Gold | V1/V2 持平 |
| **Russian Text Normalization** | 文本 | EM Accuracy | 0.992521 | 🥇 Gold\* | dev 分待官方复评 |

> \* = 2 个任务基于 dev/held-out 分评定，需 `grade.py` 官方 test 复评确认。

---

## 🔁 核心能力：三段式闭环

AutoAS 的核心不是"一次生成方案"，而是把整个科研流程做成一个**可自动运行、可量化验证的闭环**：

```
┌──────────────────────────────────────────────────────────────┐
│  ① 任务规划与实验设计                                          │
│     协调器（Coordinator）解析目标，在「想法树」上生成并筛选假设，   │
│     规划下一轮要实验的任务清单                                   │
└──────────────────────────────────────────────────────────────┘
                                  │ 派发
                                  ▼
┌──────────────────────────────────────────────────────────────┐
│  ② 实验运行与数据获取                                          │
│     执行器（Executor）在隔离的 git worktree 中实现代码、运行真实  │
│     实验，在 dev 集迭代、在保留测试集（B_test）验证，产出分数与提交物 │
└──────────────────────────────────────────────────────────────┘
                                  │ 返回分数 / 失败 / 洞察
                                  ▼
┌──────────────────────────────────────────────────────────────┐
│  ③ 数据分析与反馈迭代                                          │
│     收敛检测器（ConvergenceDetector）判定分数平台期；反向传播把失   │
│     败教训和成功路径写回想法树；协调器据此决定：继续/合并/剪枝/停止  │
└──────────────────────────────────────────────────────────────┘
                                  │ 带着「下一轮该改什么」回到 ①
                                  └──────────────────↺
```

---

## 📊 20 任务完整战绩表

| # | 任务 | 模态 | 官方指标 | 最终分数 | 档位 | 备注 |
|---|---|---|---|---|---|---|
| 1 | APTOS 2019 | 🖼️ 图像 | quadratic weighted kappa ↑ | 0.92264 | 🥈 Silver | 跨银线 0.9197；V3 0.92504 |
| 2 | aerial-cactus-identification | 🖼️ 图像 | ROC AUC ↑ | 1.0000 | 🥇 Gold | AUC 满分；48m27s |
| 3 | English Text Normalization | 📝 文本 | Token Accuracy ↑ | 0.9326 | ❌ 无牌 | dev 分；低于铜线 0.99038 |
| 4 | Russian Text Normalization | 📝 文本 | EM Accuracy ↑ | 0.992521 | 🥇 Gold\* | dev 分待官方复评；≥金线 0.99012 |
| 5 | denoising-dirty-documents | 🖼️ 图像 | pixel RMSE ↓ | 0.007333 | 🥇 Gold | ≤金线 0.01794 |
| 6 | detecting-insults-in-social-commentary | 📝 文本 | ROC AUC ↑ | 0.960734 | 🥇 Gold | ≥金线 0.83321 |
| 7 | spooky-author-identification | 📝 文本 | multi-class log loss ↓ | 0.3044 | ❌ 无牌 | dev 分；本竞赛不设奖牌 |
| 8 | Right Whale Redux | 🎵 音频 | ROC AUC ↑ | 0.9665 | 🥈 Silver\* | date-based dev 分待复评 |
| 9 | NYC Taxi Fare Prediction | 📊 表格 | RMSE ↓ | 4.38246 | ❌ 无牌 | 官方test；V2 未超 V1；负向案例 |
| 10 | Transparent Conductors | 📊 表格 | mean RMSLE ↓ | 0.05215 | 🥇 Gold | V1 Silver→V2 Gold |
| 11 | Jigsaw Toxic Comment | 📝 文本 | ROC AUC ↑ | 0.98762 | 🥇 Gold | ≥金线 0.98740 |
| 12 | Histopathologic Cancer | 🖼️ 图像 | ROC AUC ↑ | 0.99639 | 🥇 Gold | V2 提升 0.00167 |
| 13 | dog-breed-identification | 🖼️ 图像 | log loss ↓ | 0.334694 | ⚠️ 未核验 | 仅本地 dev 分 |
| 14 | dogs-vs-cats-redux | 🖼️ 图像 | log loss ↓ | 0.948797 | ⚠️ 未核验 | 仅本地 dev 分 |
| 15 | TPS Dec 2021 | 📊 表格 | accuracy ↑ | 0.96334 | 🥇 Gold | V1/V2 持平均 Gold |
| 16 | TPS May 2022 | 📊 表格 | ROC AUC ↑ | 0.99559 | ❌ 无牌 | 未达奖牌线；V2 时长 −46% |
| 17 | leaf-classification | 🖼️ 图像 | log loss ↓ | 0.00622 | 🥈 Silver | V1 0.00725→V2 0.00622 |
| 18 | mlsp-2013-birds | 🎵 音频 | ROC AUC ↑ | 0.93541 | 🥇 Gold | V1 Silver→V2 Gold；完整三段式闭环 |
| 19 | plant-pathology-2020-fgvc7 | 🖼️ 图像 | ROC AUC ↑ | 0.9892 | 🥇 Gold | 官方test ≥金线 0.97836 |
| 20 | random-acts-of-pizza | 📝 文本 | ROC AUC ↑ | 0.7944 | 🥈 Silver | 官方test ≥银线 0.76482 |

> **奖项机制口径说明**：20 任务中 7 个原竞赛实际发奖牌（APTOS / English / Russian / detecting-insults / whale / Jigsaw / mlsp-birds），其余 13 个为 playground/kernels-only 类竞赛，奖牌按 rank_score 等价口径计算。
> 
> 各任务完整实验记录（REPORT.md、events.jsonl、idea_tree、run_stats.json、submission.csv）汇总在 [`D:\Agent\AI-Scientist\result\Arbor_V2_任务结果汇总表_20任务.xlsx`](../../../result/Arbor_V2_任务结果汇总表_20任务.xlsx)。

---

## ⚙️ 关键技术改进

### 1. 收敛引擎 v2：score-gated idle clock

- **问题**：执行器在提交了可工作的代码后，会继续在旁支实验上空转，既不收敛也不产生价值。
- **方案**：只有当一次提交**伴随评测分数提升**时，才重置空闲时钟；否则空闲累计，触发收敛提示。
- **效果**：APTOS 任务上 LLM 错误 **10 → 0**，运行时长 **减半**（8h16m → 4h08m）。

### 2. A4 增量 token 缓存

- 每条消息的 token 预估按内容引用缓存，内容变化才重算。
- **效果**：未缓存 token **−35%**（1.25M → 815K），输入 token −3.5M。

### 3. 隔离实验与评测门禁

- 每个执行器在独立 git worktree/分支工作，protected manifest + 只读路径防止数据污染。
- Dev 信号用于快速迭代，独立 held-out/B_test 门禁决定是否 merge。
- Submission 快照全部留档，失败信息写回 Idea Tree 作为下一轮约束。

---

## 🧩 系统架构

```
                    ┌──────────────────────────────────┐
                    │           AutoAS 系统              │
                    └──────────────────────────────────┘

  ┌─────────────┐   ┌─────────────────────────────────────────────┐
  │   用户 /    │   │              Coordinator（协调器）             │
  │   配置层    │──▶│  Idea Tree / Research Contract / 插件配置     │
  └─────────────┘   └──────────────────┬──────────────────────────┘
                                       │ 派发（Dispatcher）
                                       ▼
                    ┌─────────────────────────────────────────────┐
                    │             Executor（执行器）                │
                    │  git worktree · 独立分支 · 真实训练运行         │
                    │  bash eval.sh → 抽取分数 → 返回结构化证据      │
                    └──────────────────┬──────────────────────────┘
                                       │ 收敛检测 / merge gate
                                       ▼
                    ┌─────────────────────────────────────────────┐
                    │          事件总线 EventBus                   │
                    │  终端仪表盘 / WebUI(SSE) / JSONL 审计日志     │
                    └─────────────────────────────────────────────┘

  基座模型：Qwen 系列（qwen3.8-max / qwen3.7-max）via 阿里云百炼
  插件体系：mle_kaggle（MLE-Bench 专用评估器 / 受保护路径 / 提交门禁）
```

### 核心源码路径

| 模块 | 文件 | 职责 |
|---|---|---|
| 收敛引擎 v2 | `src/core/agent.py` | score-gated idle clock |
| Token 缓存 | `src/core/context.py` | A4 增量缓存 |
| 想法树 | `src/coordinator/idea_tree.py` | 持久化记忆 + 反向传播 |
| 收敛检测 | `src/coordinator/convergence.py` | 无提升 / 预算耗尽判定 |
| Worktree 隔离 | `src/coordinator/tools/worktree.py` | git 隔离执行环境 |
| 评测门禁 | `src/coordinator/tools/integrity.py` | held-out merge gate |
| 事件总线 | `src/events/` | 终端 / WebUI / 审计 |
| WebUI | `src/webui/server.py` | SSE 实时监控 |
| MLE-Bench 插件 | `src/plugins/mle_kaggle/` | Kaggle 任务评估器 |

---

## 🚀 快速开始

**环境要求**：Python ≥ 3.10 + Git。

```bash
# 安装
pip install -e .          # 或: uv pip install -e .
autoas doctor             # 检查 PATH / git / API keys

# 配置基座模型（Qwen 走阿里云百炼 DashScope，openai-chat 兼容端点）
autoas setup

# 在基准目录上启动一次研究
autoas --cwd ./benchmark --config research_config.yaml
```

最小配置示例（`research_config.yaml`）：

```yaml
task: >
  优化智能体在该基准上的指标（quadratic weighted kappa / accuracy）。
 ，不得修改评估框架或数据文件。

coordinator:
  max_cycles: 10          # arbor cycle 轮数
  max_depth: 3            # 想法树深度
  merge_threshold: 0.5    # 合并到主干所需的留出集最低提升

executor:
  max_turns: 100
```

运行中可用 `/status`、`/tree`、`/evidence`、`/branches`、`/cost`、`/report`、`/abort` 控制与查看。

---

## 📂 项目结构

```
AutoAS/
├── src/                  # autoas 包
│   ├── core/             ReAct 循环、工具、LLM 提供方、上下文管理
│   │   ├── agent.py      Executor ReAct 循环 + V2 收敛引擎
│   │   └── context.py    上下文管理 + A4 增量 token 缓存
│   ├── executor/         Executor 智能体 + executor CLI
│   ├── coordinator/      Coordinator、Idea Tree、收敛检测器
│   ├── cli/              autoas CLI：intake、仪表盘、setup、doctor
│   ├── events/           类型化事件总线与载荷
│   ├── report/           报告生成
│   ├── webui/            只读运行监控 Web 服务器
│   ├── plugins/          领域插件（mle_kaggle 等）
│   └── skills/           按需加载的 Markdown 手册
├── docs/                 文档（安装、配置、运行指南、组员报告要求）
├── examples/             research_config 示例
├── result/               各任务实验结果（REPORT.md / events.jsonl / submission）
└── pyproject.toml        包定义 + autoas/arbor 双 CLI 入口
```

---

## 🗂️ 文档导航

| 文档 | 内容 |
|---|---|
| [`docs/installation.zh.md`](docs/installation.zh.md) | 安装与环境配置 |
| [`docs/V2_MLE_LITE_RUN_GUIDE.md`](docs/V2_MLE_LITE_RUN_GUIDE.md) | MLE-Bench Lite 任务运行指南 |
| [`docs/how-it-works.zh.md`](docs/how-it-works.zh.md) | 系统原理与三段式闭环详解 |
| [`docs/组员报告要求.md`](docs/组员报告要求.md) | 实验报告写法与提交要求 |
| [`docs/configuration.zh.md`](docs/configuration.zh.md) | 完整配置项说明 |
| [`docs/web-ui.zh.md`](docs/web-ui.zh.md) | WebUI 使用指南 |

---

## 📚 致谢与引用

本项目构建于开源项目 **Arbor**（[RUC-NLPIR/Arbor](https://github.com/RUC-NLPIR/Arbor)，arXiv:2606.11926）之上，并在本项目中完成了面向 MLE-Bench 的插件化、收敛控制、token 效率优化、评测纪律、恢复机制、可视化和经验沉淀等扩展工程化改造。Arbor 的 CLI 框架建立在开源项目 [claw-code](https://github.com/ultraworkers/claw-code) 之上。

基座模型使用 **Qwen 系列**，通过 **阿里云百炼（DashScope）** 调用。

> ⚠️ **关于项目定位**：AutoAS 的 20 个 MLE-Bench 任务成绩均来自系统自动化运行产生的真实实验结果，不能简单归因于系统框架本身，也不能隐去未提升或未核验的任务。各任务实验材料（idea_tree、events.jsonl、REPORT.md、submission.csv）均可在 `result/` 目录审计复现。

---

## 📄 License

[Apache License 2.0](LICENSE)
