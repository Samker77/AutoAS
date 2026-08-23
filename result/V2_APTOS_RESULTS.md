# V2 APTOS 实验结果分析（arborV2 vs Arbor-main）

> 任务：APTOS 2019 Blindness Detection（5 类糖尿病视网膜病变严重度 0–4）
> 指标：quadratic weighted kappa（保留测试集，is_lower_better=false）
> 模型：qwen3.8-max（provider=openai-chat，dashscope 兼容端点）
> V2 session：`/data/chl/work/aptos2019-blindness-detection/.arbor/sessions/v2-aptos-qwen3-max`
> 生成时间：2026-08-20

---

## 一、最终结果

| 指标 | V2 结果 |
|---|---|
| **测试集 kappa（B_test，主指标）** | **0.92264** |
| 档位 | **SILVER**（silver ≥ 0.9197；gold = 0.9305） |
| Dev 集 kappa（B_dev, fold0_val） | 0.927427 |
| Baseline（B_test） | 0.0000 |
| 最佳节点 dev 分 | 3.2.1.1 = 0.927885（ConvNeXt-Base 2-way blend） |
| 运行时长 | 4h08m49s |
| 退出状态 | `ok`（非超时） |

`submission.csv` 已 commit 到 trunk，367 行，格式 `id_code,diagnosis`（整数 0–4），merge 时由 harness 独立复核得 0.92264，working tree 干净。

---

## 二、研究轨迹（4 个 merged idea）

想法树深度 4，29 个节点，7 个打分，4 个合并，3 个 done，1 个 needs_retry。

| 节点 | Dev κ | 状态 | 做法 |
|---|---|---|---|
| 1.1.1.1 | 0.9083 | merged | EfficientNet-B3@384 + Ben Graham circle-crop + WeightedRandomSampler + AdamW cosine，建立 baseline pipeline |
| 2.1.1.1 | 0.9098 | merged | B3 5-fold stratified CV + OOF 阈值拟合（超 single-fold argmax） |
| 3.2.1.1 | 0.9279 | merged | +ConvNeXt-Base 5-fold CV，2-way score blend（OOF 0.9256 > B3 CV 0.9160） |
| 3.3.1.1 | 0.9274 | merged | +Swin-Base@384，3-way blend（权重 0.21/0.39/0.40，OOF 0.9324） |

**最终配方**：
Ben Graham circle-crop@384（disk-cached）→ 5-fold stratified CV (seed 42) → 三个 ImageNet-22k 预训练家族（EfficientNet-B3 / ConvNeXt-Base / Swin-Base）→ CE + MSE(expected-class score) ordinal loss + balanced sampling + heavy aug + 4-view TTA → 加权 expected-class score blend + 在 pooled 3295-row OOF 上 coordinate-descent 拟合 4 个阈值。

### 负向发现（写入 ROOT insight，供后续复用）

- B5@456 underperformed B3@384（0.9017 < 0.9083）—— 放大 backbone 反而更差
- 8-view TTA 没超过 4-view（OOF 0.9319 < 0.9325）
- single-fold 阈值搜索会过拟合，必须在 full OOF 上拟合
- Nelder-Mead 权重/阈值抛光提升 OOF/B_dev 但 hurt B_test（0.92216 < 0.92264）—— 简单 grid 更稳
- ConvNeXt-Large 训练未完成（hard cap，needs_retry）
- 残差误差：class 3 过预测、class 4 欠预测

---

## 三、V1 vs V2 对比

> V1 = `/data/chl/Arbor-main`（原始版本）。V1 的 session 目录在清理 trunk 时被删除，下表 V1 数据来自先前对话总结中记录的 V1 REPORT（已确认是 qwen3.8-max 真实运行结果）。

| 维度 | V1 (Arbor-main) | V2 (arborV2) | 差异 |
|---|---|---|---|
| 模型 / Provider | qwen3.8-max / OpenAICompatProvider | 同左 | ✅ 一致 |
| **运行时长** | 8h16m41s | 4h08m49s | **V2 快约 50%** |
| Cycles | 9 | 11 | V2 +2 |
| 想法总数 | 22 | 28 | V2 +6 |
| Merged | 3 | 4 | V2 +1 |
| **LLM 错误** | 10 | **0** | **V2 完全消除** |
| Eval 失败 | 2 | 3 | 基本持平 |
| LLM 调用 | 369 | 388 | 基本持平 |
| 输入 token | 17.4M | 13.9M | V2 −3.5M |
| **未缓存 token** | 1.25M | 815K | **V2 −35%** |
| 输出 token | 335K | 266K | V2 −69K |
| 协调器轮次 | 101 | 117 | V2 +16 |
| **测试集 κ（B_test）** | 0.89643 | **0.92264** | **V2 +0.0262** |
| Dev 集 κ | 0.9263 | 0.9274 | V2 略高 |
| 档位 | 未达 silver | **SILVER** | V2 跨线 |

### 关键结论

1. **V2 显著更强**：B_test 0.92264 vs 0.89643（+0.0262，约 +3 个百分点），跨过 silver 档线（0.9197），逼近 gold（0.9305）。

2. **V2 代码变更的三大贡献**
   - **消除 LLM 错误**：10 → 0，避免因错误重试空耗周期。
   - **token 效率提升**：未缓存 token −35%（1.25M → 815K），说明 V2 的 prompt 结构更紧凑、缓存命中率更高。
   - **更深的想法树**：28 vs 22 想法、4 vs 3 merged，协调器在更短时间内探索了更多策略。

3. **策略更优**：V1 走 B5@448 单模型 + 离线混合扫描；V2 走 3 家族架构多样性 blend（B3/ConvNeXt/Swin），且验证了 B5 不如 B3、ConvNeXt-Large 受 hard cap 限制 —— 探索更深、决策更对路。

4. **时间效率翻倍**：4h vs 8h 跑出更高分，LLM 错误归零是主要贡献。

---

## 四、search 功能是否开启：已确认开启并实际调用

`research_config.yaml`（aptos 任务）：

```yaml
search:
  enabled: true
  builtin_backend: alphaxiv   # 零配置，无 API key
  auto_search_on_add: false
```

`builtin_backend: alphaxiv` 使 `SearchConfig.has_backend = True`，SearchAgent 工具链（`web_search` / `web_visit`）被注册，`SearchIdeaContext` 工具对协调器可用。

### 运行中实际调用证据（来自 events.jsonl）

V2 运行中 search 通道**真实触发并完成**，共 13 条相关事件：

- `SearchIdeaContext` 调用 3 次（dispatch 到后台 SearchAgent）
- `SearchStatus` 查询 2 次（确认后台搜索完成）
- `web_search` 4 次（每次批量 3 个 query，去重后取候选 URL）
- `web_visit` 4 次（抓取 alphaxiv 论文页全文）

**典型一次调用链**（节点 1.1.1.1，B3@384 baseline）：
1. 协调器在节点 done 且 score(0.9083) > trunk(0.0) 后，调用 `SearchIdeaContext(node_id="1.1.1.1", focus="APTOS 2019 ... EfficientNet + Ben Graham circle crop")`
2. 后台 SearchAgent 发 `web_search`（3 个 query：技术/方法/数据集角度），去重得 14 个候选 URL
3. `web_visit` 抓取 3 篇 alphaxiv 论文（`2604.17341v1`、`2607.25545v1`、`2507.17121v2`）
4. SearchAgent 推理后判定 **`prior-art-exists`**（这是标准 APTOS 2019 recipe，非研究新颖性），写入 `node.related_work`
5. 协调器随后 `SearchStatus` 确认无后台搜索在跑，继续 IDEATE/merge

> 注意：这次 novelty 审计判定为 prior-art-exists，但因为是工程竞赛、novelty 不影响 merge 决策，协调器仍正常合并该节点 —— 这符合 `_related_work_annotation_section` 里"prior-art-exists 不阻断 merge"的设计。

### 配置开关状态汇总

| 开关 | 值 | 含义 |
|---|---|---|
| `search.enabled` | true | 搜索基础设施开启 |
| `search.builtin_backend` | alphaxiv | 零配置 arXiv 论文后端，无 API key |
| `search.has_backend` | True（派生） | web_search/web_visit 工具已注册 |
| `search.auto_search_on_add` | false | 不在新建节点时自动搜索（按需触发） |
| `search.require_validated` | true（默认） | 只对 beat-trunk 的 validated 节点花搜索预算 |
| `search.grounded_ideation` | false（默认） | 主动研究通道（ResearchSearch）关闭，保持 benchmark 公平 |
| `search.background` | true（默认） | novelty 审计后台运行，不阻塞协调器 |

**结论**：novelty 审计通道（`SearchIdeaContext` + alphaxiv）在 V2 运行中**确实开启并工作**；主动研究通道（`ResearchSearch` / grounded_ideation）按 benchmark 公平性默认关闭，未触发。这与 V1 一致（公平对比成立）。

---

## 五、公平性说明

- V1/V2 均使用 qwen3.8-max + dashscope 端点 + mle_kaggle 插件，从同一 cleaned trunk（仅 framework：data/ private/ eval.sh research_config.yaml .gitignore）起步。
- search 配置两边一致（alphaxiv 后端、grounded_ideation=false），不引入外部知识不对称。
- ⚠️ V1 的 session 目录已被删除，上表 V1 数据来自先前总结中的 REPORT 记录。如需更严格复核，可从 `Arbor-main` 重新跑一次 V1（约 8h）。

---

## 六、参考文件

- V2 REPORT：`/data/chl/work/aptos2019-blindness-detection/.arbor/sessions/v2-aptos-qwen3-max/REPORT.md`
- V2 run_stats：同目录 `run_stats.json`
- V2 coordinator final report：同目录 `COORDINATOR_FINAL_REPORT.txt`
- V2 idea tree：同目录 `.coordinator/idea_tree.{json,md}`
- V2 事件日志：同目录 `events.jsonl`（search 调用证据见上文）
- 配置：`/data/chl/work/aptos2019-blindness-detection/research_config.yaml`
- 论文相关性机制说明：`/data/chl/arborV2/PAPER_RELEVANCE.md`
