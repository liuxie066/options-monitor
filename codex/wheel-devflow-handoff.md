# Wheel 需求 → devflow 交接

- 状态：Deepreview Gate 第 4 轮已通过；实现与验证完成，用户已确认完成，Devflow Closeout 已关闭。
- 根任务：创建 prdflow skill。Wheel 为真实试跑案例，不把其实现误当成本轮 skill 任务。
- prd_doc：[om-wheel-prd.md](om-wheel-prd.md)，批准原话集中在 §8。
- design_doc：`../docs/WHEEL_STRATEGY_PRD.md` 第 13 节；需求 PRD 仍是产品唯一真源。
- 源码目标：本仓库根目录。
- 证据基线：本地跟踪 `origin/main@343a5d1f720e15fc05b168cd8d363788cbd137b3`，本轮未联网刷新，也不是生产核实。
- 已获准：保存需求、准备交接和 prdflow 源码草稿；按用户要求将 Wheel 需求及交接迁入 options-monitor 的 `codex/`；2026-09-08 用户以“让它启动devflow”明确授权启动 Wheel devflow；2026-09-09 用户以“提交并推送，不发布”授权提交并推送本次已验证源码。
- 未获准：merge、发布、部署、生产配置修改或生产写入。

## Devflow 启动约束（已执行到 Brainstorm）

1. 读取目标项目 AGENTS.md 和当前可用 devflow skill，刷新源码/配置/测试与工作区状态。
2. 把 PRD 的目标、非目标、S1～S5、A01～A16 和 §8 批准记录作为产品输入。
3. 从 devflow Brainstorm 的实现可行性/方案讨论开始；沿当前 workflow 获得实现设计确认。
   不无故重问已确认业务问题，但事实冲突和影响行为的新选择必须回到用户。
4. 不能把本文或 PRD 的保存映射成 devflow Save Design 完成，不能提前触发自动 Panel。
5. 后续实现设计由接收方按项目 owner-first 约定落盘，再遵循其实际 gates。

重点源码入口：`domain/domain/wheel.py`、`src/application/wheel/`、
`src/application/ledger/wheel_trade_companions.py`、`src/application/ledger/api.py`、
`domain/domain/engine/candidate_engine.py`。
测试入口：`tests/test_wheel_strategy.py`、`tests/test_wheel_scanning.py`、
`tests/test_wheel_workflows.py`、`tests/test_wheel_tick_integration.py`。

当前 canonical 产品 owner 是 OM 的 `docs/WHEEL_STRATEGY_PRD.md`；第 1～12 节保留既有单向 Call 合同，第 13 节记录本隔离工作区已实现的双向合同。
需求唯一真源仍为本项目 `codex/om-wheel-prd.md`；当前事实仅是未提交的本地实现，不代表已合并、发布、部署或生产启用。
接收方按项目规则维护该 owner；不强制提交被项目忽略的 plans/reviews 文档。

## 校验与限制

已做：需求 walkthrough、Brainstorm、Save Design、Parallel Design Panel、Improve Design、4 轮 Planreview、Workspace Isolation Check、Wheel 实现、完整本地 validation 和 4 轮 Deepreview。
未做：生产配置核验、收益回测、commit/push/merge、发布、部署或生产启用；这些仍是独立授权边界。
本地实现和验证通过不表示生产配置、发布、部署或真实交易链已获验证或授权。
实际工作区/base 由接收方读时再确认；不预建 worktree，不 stash 或回退无关改动。

## Devflow Progress

- 更新时间：2026-09-09。
- 当前节点：`Closeout / completed`。
- 状态：`complete`；Deepreview attempt 4 verdict `pass`，当前无 blocking finding，用户已明确回复“确认完成”。
- `next_action`：无。未经独立授权不 commit/push/merge、不发布、不部署、不修改生产配置或生产状态。
- 原始目标、非目标、范围和成功信号：唯一记录见 [om-wheel-prd.md](om-wheel-prd.md) §1、§7；S1～S5、A01～A16 及 §8 批准依据保持不变。
- 授权差异：用户原话“让它启动devflow”取代此前“尚未获准启动 devflow”的限制；在 Planreview 通过和 Workspace Isolation Check 后，用户于 2026-09-08 回复“确认实现”，明确授权进入 Implementation。该授权不包含 commit/push/merge、发布、部署、生产配置修改或生产写入。
- 节点确认：用户在看到 Brainstorm 推荐方案与取舍后回复“确认”，授权 Save Design；按 devflow 规则随后自动进入 Parallel Design Panel。Panel 汇总后用户再次回复“确认”，授权 Improve Design；Planreview 仍按 devflow 自动门执行，不代表授权 Implementation。
- Capability Check：`ponytail_status=used`、`planreview_status=used`、`deepreview_status=used`、`om-doc-hygiene_status=used`。
- `design_doc`：`docs/WHEEL_STRATEGY_PRD.md`，当前 SHA-256 `44c550eafed69a0710eda5fbdbd51737e164612589e11bb428ec22b877bbfe5e`；第 1～12 节保留单向 Call 合同和兼容语义，第 13 节已按实现事实更新为双向合同。
- workspace isolation：已创建 `implementation_workspace=/private/tmp/om-wheel-bidirectional`、branch `codex/wheel-bidirectional`，基线为本地 `origin/main@343a5d1f720e15fc05b168cd8d363788cbd137b3`；只迁移已核验的 `docs/WHEEL_STRATEGY_PRD.md`、`codex/om-wheel-prd.md`、`codex/wheel-devflow-handoff.md`。原受保护 `main` 及其无关用户 dirty 文档未被移动、stash、reset 或清理。
- `review_base`：本地跟踪 `origin/main@343a5d1f720e15fc05b168cd8d363788cbd137b3`，本轮未联网刷新；隔离工作树 `HEAD@343a5d1f720e15fc05b168cd8d363788cbd137b3`。原受保护主工作树及其用户改动全部保留。
- 内容版本：`om-wheel-prd.md` SHA-256 为 `a09b3b557bc93e5dccb4d43c4eba561ad3b3955ffac44a5863f677e6e253d218`；Brainstorm 依据当前 `origin/main` 的 Wheel、ledger、配置、扫描、CLI/Agent 和测试 owner。
- Panel 结果：Architecture `/root/wheel_panel_architecture=improve`；Failure & Safety `/root/wheel_panel_safety=improve`；Simplicity `/root/wheel_panel_simplicity=improve`；DSH adversarial critic `hub-38-mtso3v47=improve`。四者均校验并只读审查同一 design hash，无文件写入。
- 评审轮次：Planreview `1/5` completed (`fail`, 4 blockers)，`2/5` completed (`fail`, 5 blockers)，`3/5` completed (`fail`, 2 blockers)，`4/5` completed (`pass-with-risks`, 0 blockers)；Deepreview `1/5` completed (`fail`, 10 findings)，artifact `docs/reviews/code-review-20260909-013557.md`；`2/5` completed (`fail`, 3 findings)，artifact `docs/reviews/code-review-20260909-041413.md`；`3/5` completed (`fail`, 3 findings)，artifact `docs/reviews/code-review-20260909-041414.md`；`4/5` completed (`pass`, 0 findings)，artifact `docs/reviews/code-review-20260909-041908.md`。Panel 不占用 Planreview 轮次。
- Deepreview attempt 1 findings：DR-01 snapshot v1/v2 direct-loader strict matrix；DR-02 intent durable activation；DR-03 Put branch ID/Brief identity；DR-04 candidate alert Wheel selection；DR-05 market-bound facade/transaction；DR-06 Agent output contracts；DR-07 legacy Call diff compatibility；DR-08 Put realized net PnL；DR-09 multiplier provenance；DR-10 batched legacy assignment rolling state。均为已批准合同内修复，不扩大产品范围。
- Deepreview attempt 1 repair：direct loader 现要求 v1/v2 恰好一个；新 branch/intent 在同一事务校验 durable activation、policy 和 symbol market；Put ID 改为无冒号 deterministic identity；candidate alert 按 exact branch 过滤；Agent contract、legacy Call digest、Put canonical realized net PnL、multiplier provenance 和 batched legacy assignment rolling state 均在共享 owner 修复。
- Deepreview attempt 2 findings/repair：关闭 multiplier evidence 自签且无 source receipt、历史 as-of 混用未来 trade/current lots、以及 end/cancel/linkage 的 market 隔离缺口；cache/OpenD/bootstrap evidence 绑定真实 receipt，read model 从同一 as-of trade subset 重投影 lots，所有 mutation owner/事件/idempotency/receipt 绑定 market。
- Deepreview attempt 3 findings/repair：关闭 prepared tick context 漏传 market、CLI/pipeline OpenD refresh 丢失 receipt、以及 Call already-inactive receipt/Agent schema 漏 market；均复用现有 read-model 或 `store_multiplier` owner，无新增层或配置。
- Deepreview attempt 4 conclusion：ledger/economics、facades/compatibility、pipeline/artifacts 三路均 `pass`，无 material finding；保留的 mixed-market integration fixture 缺口为非阻断 residual risk。
- final validation：修复后定向 `109 passed`；全部改动测试文件 `722 passed, 1 warning`；完整 suite 在仅允许本机 loopback 后 `6119 passed, 1 warning`。Ruff、`git diff --check`、dependency graph `production_modules=602 cycles=0`、guardrails 与 projector fingerprint 均通过；warning 仍为既有 Legacy Tick renderer deprecation。临时 `agent-runtime/node_modules` symlink 已清理。
- final scope：tracked changes `69`、untracked files `7`，status inventory SHA-256 `9d9b3a019fdfa986440b8bd9c869985cce694d41446046270b6c405dc72612fe`；继续以 `origin/main@343a5d1f720e15fc05b168cd8d363788cbd137b3` 为 review base。
- Improve Design 已接受并关闭：v1/v2 event schema 与 hash 分派、v2 写入后的前向回退、Put 与普通 CSP 的唯一现金 allocator、child 真实交割本金锚与最终舍入守恒、启用 policy 进入两个 trade writer、无股票 lot 的公开读取面、legacy open Call lot adapter、snapshot v2 身份、普通 CC 冲突判定及 Slice 1 facade 完整性；初始 design SHA-256 为 `149724772af6b824288d50fe1b854483f9145206bbe395a0bd298a41afa751d9`。
- Planreview attempt 1：artifact `docs/reviews/plan-review-20260908-212546.md`；review handle `/root/wheel_planreview_1`；目标 hash `32ee393c83e65500150ef4dbb0659b9b316789fd085a768c6039a89284677b82`。
- Planreview attempt 1 findings：PR-01 durable activation interval/generation；PR-02 allocation 必须与 ingestion order 无关；PR-03 `strategy_scan_status` 需纳入 direction identity；PR-04 普通 CC 必须复用 canonical strategy membership。评审明确这些均不需要新的产品选择。
- Planreview attempt 2：artifact `docs/reviews/plan-review-20260908-215223.md`；review handle `/root/wheel_planreview_1` follow-up；目标 hash `27828cd0a6dc3353bb900c5fffa9a192b18b844048d4bda1204a07b9debeee3e`。
- Planreview attempt 2 findings：PR2-01 activation 必须按 market/account 隔离；PR2-02 static config validation 与 runtime readiness/state 分离并固定 fail-closed rollout；PR2-03 冻结 direction-neutral phase contract；PR2-04 multiplier 必须绑定 provenance；PR2-05 sealed status/index/snapshot/manifest 必须显式升级版本并 dual-read legacy。评审确认仍不需要新的产品选择。
- Planreview attempt 3：artifact `docs/reviews/plan-review-20260908-221716.md`；review handle `/root/wheel_planreview_1` follow-up；目标 hash `bdd3092c26f09a8576dcfc3b8f58390e50aaf801909091d1440f75666b77b2a3`。
- Planreview attempt 3 findings：PR3-01 activation 只能 gate 普通入口和新 action，不能丢弃既有 Wheel 内部 assignment lineage；PR3-02 ordinary entry 必须只按 immutable historical window 的 event time 判定，不能要求命中 window 当前仍 open。评审确认两项均可由已批准产品合同直接收敛。
- Planreview attempt 4：artifact `docs/reviews/plan-review-20260908-223753.md`；review handle `/root/wheel_planreview_1` follow-up；目标 hash `f802005b2fb8a3fc8a40d2880c97c70e4853eed36f4a29ed72fcc2a37db06dcd`。
- Planreview attempt 4 conclusion：PR3-01/PR3-02 closed；verdict `pass-with-risks`，未修复 blocker `0`。Residual risks 归属后续 Slice 1 migration/writer evidence、Slice 2 allocator/artifact compatibility、独立 release/activation runbook；不改变当前产品范围，也不授权生产操作。

### Implementation Completion Evidence

- 进入 Deepreview 前已逐项读取 staged、unstaged 和 untracked inventory；staged 为空，tracked changes `62`，untracked files `7`。写入本 checkpoint 前的内容清单 SHA-256 为 `a766f546b6f830faaad4f8fa0ccf6b0dee149c20d79a11590b6d7fcc49d8527b`；全部映射到 S1～S5/A01～A16、canonical living doc 或 generated dependency graph，无无关文件。
- planned 实现覆盖 versioned Wheel branch lifecycle、durable activation、Call/Put policy/scan/capacity、intent/linkage facade、两个 trade writer companion、direction-aware status/snapshot/manifest、required-data、Daily Brief、CLI/Agent 和兼容读取。
- required-correctness/safety 收口：非 Wheel action 不持久化空 `wheel_branch_id`，保留 legacy Daily Brief digest；runtime readiness 移至中立 Wheel owner，消除 production import cycle；legacy/v2 Call event 根据 projection generation 选择 schema，避免旧分支操作写入后不生效。这三项均由全量测试中的直接失败路径发现并修复。
- validation：最终修复后定向 focused `109 passed`，全部改动测试文件 `722 passed, 1 warning`；在临时复用现有本机 Node 依赖并允许仅回环测试端口后，完整 suite `6119 passed, 1 warning`；Ruff、`git diff --check`、dependency graph `cycles=0`、文档/runtime-config/sensitive-artifact guardrails 全部通过。唯一 warning 为既有 Legacy Tick renderer deprecation。
- projector implementation fingerprint 已核对 expected/actual 均为 `0e7b1bdaa502d3ec774df69e9701f9b127fe2870c1d4c5b6b987418338a237b5`。临时 `agent-runtime/node_modules` symlink 已移除；未安装依赖、未写生产状态。

### Implementation Baseline

- 冻结设计：`docs/WHEEL_STRATEGY_PRD.md`，SHA-256 `f802005b2fb8a3fc8a40d2880c97c70e4853eed36f4a29ed72fcc2a37db06dcd`。
- `implementation_workspace`：`/private/tmp/om-wheel-bidirectional`；branch `codex/wheel-bidirectional`。
- `review_base` / `HEAD`：本地 `origin/main@343a5d1f720e15fc05b168cd8d363788cbd137b3`。
- 捕获时 `git status --short`：` M docs/WHEEL_STRATEGY_PRD.md`、`?? codex/`；staged 内容为空。
- unstaged：`docs/WHEEL_STRATEGY_PRD.md`，64297 bytes，SHA-256 `f802005b2fb8a3fc8a40d2880c97c70e4853eed36f4a29ed72fcc2a37db06dcd`。
- untracked：`codex/om-wheel-prd.md`，15382 bytes，SHA-256 `a09b3b557bc93e5dccb4d43c4eba561ad3b3955ffac44a5863f677e6e253d218`。
- untracked：`codex/wheel-devflow-handoff.md`，8638 bytes，pre-baseline-annotation SHA-256 `84a88aa21bd5cb84d6cbe6519ab7a5c5d5980bf0fdd907a07ae93890347d9799`；本段写入是 Implementation gate 的控制材料更新，不属于产品实现 diff。

## Source Delivery Authorization

- 用户授权原话：`提交并推送，不发布`。
- 交付范围：本次 Wheel S1～S5/A01～A16 实现、测试、唯一需求真源、当前 living doc、generated dependency graph 与 `CHANGELOG.md / Unreleased`。
- 交付基线：已刷新 `origin/main@82d32849`；提交在推送前必须集成该基线并复验。
- 明确排除：merge、VERSION、tag、GitHub Release、发布产物、部署、生产配置、通知、交易和任何生产写入。
