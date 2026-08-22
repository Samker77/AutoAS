# Arbor 基线运行指令

## 一、这是什么

在 `baseline_algotune`（k-NN AlgoTune 任务）上跑一次完整实验，作为 A/B 验证的**对照组**。

**对照组定义**：框架 = `214c5f5`（Windows worktree 修复）+ **不含 A4 优化**。优化组 = 同一 commit + 重新应用 `D:\Agent\bench\a4.patch`。

> ⚠️ 背景：本机是 Windows，首次启动基线死于 worktree 创建 bug（`shlex.quote` 产生的单引号经 cmd.exe 传给 git 变成字面引号）。框架已提交修复（`214c5f5`，`_run_git`/`_run_cmd` 改用 `shlex.split` + `create_subprocess_exec`），经验证 worktree 创建成功。因此"未修改框架"在这台机器上不可行——worktree 修复是两侧共用的前置条件，A/B 对比的变量只有 A4。

## 二、前置条件

| 项 | 基线组（已跑完 ✅） | 优化组（当前，待跑） |
|---|---|---|
| 全局配置 `C:\Users\admin\.arbor\config.yaml` | ✅ | ✅ |
| 任务仓库 `D:\Agent\baseline_algotune` | ✅ 起点 = `master` 参考版 | ✅ **已重置回 `master` 参考版**（零删除；6 个 coordinator/* 分支保留留档，trunk 已指回 master） |
| 框架 | `214c5f5` + 无优化 | `214c5f5` + `6fca246`（A4）+ `7e4bfa4`（executor 收敛） |
| 虚拟环境 `.venv`（含 arbor.exe） | ✅ | ✅ |

基线结果见 `BASELINE_REPORT.md`（dev 16.4x / test 18.23x，4656s，7.72M input tokens，95.3% 缓存命中）。基线 trunk 哈希 `1c83895` 已存档。

## 三、运行命令（优化组，与基线完全相同的 prompt）

打开一个 **Git Bash 窗口**，整行粘贴：

```bash
cd /d/Agent/baseline_algotune && /d/Agent/AI-Scientist/Arbor-main/.venv/Scripts/arbor.exe run "maximize the speedup printed by 'bash eval.sh dev'; iterate on dev, gate merges on 'bash eval.sh test'; only edit solution.py; never touch task.py, eval.py, or eval.sh; output must keep passing is_solution" --yes --yes-cwd /d/Agent/baseline_algotune
```

> ⚠️ 请用**独立的终端窗口**运行，不要用对话里的 `!` 前缀（会被当前会话的分类器拦截）。

## 四、运行中会看到什么

终端会滚动类似输出（Observe → Ideate → Select → Dispatch → Backpropagate → Decide 六步循环），executor 会在 `%TEMP%\coordinator-worktrees-admin\` 下创建 worktree：

```
Observe → Ideate → Select → Dispatch → Backpropagate → Decide ...
[executor 1.1] running in worktree ...
```

配置参数（来自 `research_config.yaml`）：

- `max_cycles: 6`
- `executor_max_turns: 30`
- `coordinator.max_depth: 2`
- `merge_threshold: 5.0`（dev 提升 ≥5% 才合并）
- 预计全程 **10–60 分钟**

## 五、运行完成后

1. 在对话里告诉我"基线已跑完"。
2. 我会用 `D:\Agent\bench\metrics.py <session_dir>` 提取指标（wall-clock、token 用量、cache 命中、best/trunk/test 分数、LLM 调用次数）。
3. 然后应用 `D:\Agent\bench\a4.patch`（增量 token 估算缓存），在相同任务上再跑一轮（优化组），做 A/B 对比。

## 六、A/B 对比对照表

| 组 | 框架 | 任务仓库 | 命令 |
|---|---|---|---|
| 基线 | `214c5f5` + 无 A4 | baseline_algotune | 上文命令 |
| 优化 | `214c5f5` + a4.patch | 同一仓库 | 同上（重跑） |

## 七、第一次启动失败记录（2026-08-11）

首次启动死于 worktree 创建（`could not create leading directories of ''C:/.../.git': Invalid argument`）。根因：`shlex.quote` 把含反斜杠的 Windows 路径包上 POSIX 单引号，`create_subprocess_shell`（cmd.exe）不去引号 → git 收到字面 `'C:/...` → Invalid argument。运行中的 coordinator（基线 LLM）自主诊断并修复了 `git_ops._run_git` + `experiment._run_cmd`；我已验证 `_create_worktree` 在该修复下成功，并提交为 `214c5f5`。修复对基线/优化组同等生效，不影响 A/B 变量。
