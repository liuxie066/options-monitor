# Gateflow Goal Confirmation — Candidate Brief Evidence Integrity

- Work unit: `candidate-brief-evidence-integrity`
- Gate: `goal confirmation`
- Date: 2026-08-12
- Status: confirmed by user
- Branch: `fix/candidate-brief-evidence-integrity`
- Base: `origin/main@ded8f882`
- Artifact path: `docs/gateflow/candidate-brief-evidence-integrity/goal-confirmation.md`

## User problem

HK 14:00 批次把已完整计算且净权利金不大于零的合约当成输入或行情证据不可用，使合法的经济性拒绝被投影为
`partial_data` / `data_unavailable`，进而产生“部分行情证据不可用”和不准确的 AI 文案。同一份回执还把
成功的 `fetched` 预取、未启用的 CC+LP 变体快照记为缺口，并且会用策略级通用原因遮蔽已封存合约中的
具体 RV 缺失原因。

## Confirmed goal

修复 HK/US 共用的候选证据分类和定时 Daily Brief 投影，使三类事实严格分开：

1. 证据完整后得出的确定性经济拒绝；
2. RV、报价或输入绑定等真实硬证据缺失；
3. 完整扫描后的合法零候选。

Daily Brief 只消费现有 sealed snapshot、策略状态和预取回执，不重算策略事实。

## Motivation and direct evidence

- 真实 HK run `20260811T060021Z-c516fc` 的 460 条决策中，29 条 `input_invalid` 全部是
  `net_premium_non_positive`；460/460 的 `term_matched_rv_status` 均为 `ok`，但这 29 条仍让 7 个策略范围被投影为证据不完整。
- 真实 US run `20260811T174005Z-a34b02` 中 607/607 的 RV 状态为 `ok`，10 条
  `net_premium_non_positive` 同样被错误计入证据缺口，证明错误在 HK/US 共用链路。
- `candidate_scanning._calculation_decision_record()` 将 opening-ready 合约的所有计算失败统一标为
  `input_invalid`；`evidence_summary_from_decisions()` 又把 `input_invalid` 列为 unavailable，是错误分类的直接链路。
- `candidate_universe_summary()` 先写入策略级通用原因，再对合约原因使用 `setdefault()`；合约
  `rejects[].metric_value.reason_code` 里已有 `term_matched_rv_unavailable`，但被通用原因遮蔽。
- `_append_prefetch_gaps()` 的成功状态集合没有 `fetched`；`assemble_daily_decision_brief()` 无条件读取
  CC+LP snapshot；AI unavailable 分支无论是否存在候选行都声称“以下展示策略原始排序”。
- 基线 `1.13.12` 已包含确认的 6 个自然日财报窗口规则；该规则不属于本 work unit。

## Success signals

1. `net_premium_non_positive` 在 opening-ready 合约上投影为确定性策略/经济拒绝，不再计入
   `input_invalid`、`eligibility_unresolved_count`、`partial_data` 或 `data_unavailable`。
2. RV、报价、multiplier 绑定或其他真实不可评估输入仍然 fail closed，不得因本修复降级为普通策略拒绝。
3. sealed snapshot 内已有的具体证据原因（例如 `term_matched_rv_unavailable`）进入 Daily Brief 缺口事实，
   且回执用可理解的中文原因表达，不再只显示通用告警。
4. 预取条目 `status=fetched` 且 `reason/message=ok` 时不生成 data gap；真实失败状态仍生成符号级缺口。
5. CC+LP snapshot 只在当前市场配置实际启用 `cc_lp` 变体时是必需证据；`sp_lc` 或未启用时不生成假缺口。
6. AI 状态不可用且该策略没有可展示候选行时，文案不得声称“以下展示原始排序”；真实有候选行时保留现有降级说明。
7. HK/US、Sell Put/Covered Call、合法零候选、真实证据缺失和正常候选的 deterministic regressions 通过；
   focused tests、相关广泛回归、compile/analyze 与 `git diff --check` 通过。

## Non-goals and scope boundary

- 不再修改财报 6 日窗口、候选阈值、排序、资金/持仓容量或策略开关。
- 不处理 DeepSeek `DeepSeekResponsesError`、凭据、网络或 provider 可用性；这是独立 work unit。
- 不新增 public schema、状态层、第二套候选分类器或旧 CSV/trace 事实源。
- 不修改 `config.yaml`、`config.us.json`、`config.hk.json`、secret 或生产 runtime artifacts。
- 不远程重跑、不发通知、不发布、不部署、不升级、不合并 main。

## Overdesign deliberately excluded

本轮复用 `CandidateCalculationError` 的稳定 reason、现有 evidence summary、sealed
`opening_candidate_snapshot.v1` 的 `rejects` 和当前 Daily Brief `data_gaps`。不引入新分类 schema、新快照、
新数据库表或 AI 事实判断。

## Blocking open questions

无。用户已于 2026-08-12 确认上述目标、成功信号、非目标和边界。
